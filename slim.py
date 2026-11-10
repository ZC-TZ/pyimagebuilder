#!/usr/bin/env python3
"""无 Docker 瘦身镜像：默认扁平化，显式裁剪、保护、审计及 gzip 压缩。"""

import argparse
import hashlib
import json
import lzma
import os
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

from archive_compression import gzip_stream
from cache_identity import file_identity, stat_identity
from errors import BuildError
from file_publish import publication_transaction, publish_new_file
from file_resources import open_regular_file
from image_reader import sha256_file
from platforms import normalize_platform
from progress import make_reporter
from reproducible import parse_epoch
from rootfs import RootFSIndex
from rootfs_archive import (_config_digest, _derived_config, _output_path, _write_image,
                            image_tag, merge_rootfs, open_image)
from settings import cache_directory, load_settings
from slim_rules import DEFAULT_LIMIT, load_patterns, prune

BUFFER = 4 * 1024 * 1024


def _snapshot(source, target):
    """只从本次打开的输入复制；变化时拒绝混用不同归档的字节。"""
    source = Path(source).resolve()
    identity = file_identity(source, content=False)
    digest = hashlib.sha256()
    with open_regular_file(source) as stream, target.open("xb") as output:
        initial = stat_identity(os.fstat(stream.fileno()))
        if initial[:4] != identity[:4]:
            raise BuildError("Slim input changed before snapshot")
        remaining = initial[2]
        while remaining:
            block = stream.read(min(remaining, BUFFER))
            if not block:
                raise BuildError("Slim input was truncated")
            output.write(block)
            digest.update(block)
            remaining -= len(block)
        if stream.read(1) or stat_identity(os.fstat(stream.fileno())) != initial:
            raise BuildError("Slim input changed during snapshot")
    if file_identity(source, content=False) != identity:
        raise BuildError("Slim input changed during snapshot")
    return initial[2], "sha256:" + digest.hexdigest()


def _gzip(source, output, level, reporter=None):
    """固定 gzip 头，并从压缩结果重读校验 CRC、长度与未压缩摘要。"""
    with source.open("rb") as stream:
        return gzip_stream(stream, output, source.stat().st_size, level, reporter)


def _default_output(source, compressed):
    path = Path(source)
    if path.is_file():
        name = path.name
        for suffix in (".tar.gz", ".tgz", ".tar"):
            if name.lower().endswith(suffix):
                name = name[:-len(suffix)]
                break
        return path.parent / (name + "-slim.tar" + (".gz" if compressed else ""))
    name = str(source).rsplit("/", 1)[-1].replace(":", "-").replace("@", "-")
    return Path.cwd() / (name + "-slim.tar" + (".gz" if compressed else ""))


def slim_image(source, output=None, *, tag=None, reference=None, platform="linux/amd64",
               store=None, kind=None, deletes=(), keeps=(), delete_files=(), keep_files=(),
               auto_protect=True, analyze_only=False, report=None, gzip_output=False,
               compress_level=6, upload_limit=DEFAULT_LIMIT, largest=20, workspace=None,
               source_date_epoch=0, reporter=None, stored=False):
    """输出新 Docker 镜像及可选完整报告；原归档/引用不修改，也不自动入库。

    报告与镜像处于同一发布事务，临时目录清理或任一发布失败时回滚本次产物。
    预览模式只生成决策报告，不生成镜像，也不能精确判断重建后 tar 的大小。
    """
    if type(compress_level) is not int or not 1 <= compress_level <= 9:
        raise BuildError("gzip level must be between 1 and 9")
    if type(upload_limit) is not int or upload_limit <= 0:
        raise BuildError("Upload limit must be a positive byte count")
    if type(largest) is not int or not 1 <= largest <= 1000:
        raise BuildError("Largest file count must be between 1 and 1000")
    deletes, keeps = load_patterns(deletes, delete_files), load_patterns(keeps, keep_files)
    platform, epoch = normalize_platform(platform), parse_epoch(source_date_epoch)
    requested = output if output is not None else _default_output(source, gzip_output)
    compressed = gzip_output or str(requested).lower().endswith((".tar.gz", ".tgz"))
    if compressed and not str(requested).lower().endswith((".tar.gz", ".tgz")):
        raise BuildError("gzip output requires .tar.gz or .tgz suffix")
    if not compressed and Path(requested).suffix.lower() != ".tar":
        raise BuildError("Slim output requires .tar suffix")
    destination = _output_path(requested) if not analyze_only or output is not None else None
    report_path = _output_path(report) if report is not None else None
    if report_path is not None and (report_path.suffix.lower() != ".json" or report_path == destination):
        raise BuildError("Slim report requires a distinct new .json file")
    parent = Path(workspace).expanduser().resolve() if workspace is not None else (
        destination.parent if destination is not None else report_path.parent if report_path else None)
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    result = None
    with publication_transaction() as published, \
            tempfile.TemporaryDirectory(prefix="pyimagebuilder-slim-", dir=parent) as temporary:
        private = Path(temporary)
        input_source, before_bytes, source_digest = source, None, None
        if not stored and Path(source).is_file():
            if reporter is not None:
                reporter.phase_start("Snapshot slim input")
            input_source = private / "source.tar"
            before_bytes, source_digest = _snapshot(source, input_source)
            if reporter is not None:
                reporter.phase_success()
        with open_image(input_source, private / "read", tag, platform, store, kind, stored=stored) as (image, selected):
            output_tag = image_tag(reference or selected)
            if reporter is not None:
                reporter.phase_start("Plan image slimming")
            index = RootFSIndex()
            for number, layer in enumerate(image.layers, 1):
                index.apply_layer(layer)
                if reporter is not None:
                    reporter.progress(number, len(image.layers), "Indexing slim source", unit="layers")
            decision = prune(index, deletes, keeps, image.config, auto_protect, largest)
            result = {"schemaVersion": 1, "operation": "slim", "analyze_only": analyze_only,
                      "tag": output_tag, "platform": platform, "source_image_id": image.config_digest,
                      "source_sha256": source_digest, "before_bytes": before_bytes,
                      "before_layers": len(image.layers), "decision": decision,
                      "gzip": compressed, "upload_limit": upload_limit, "under_upload_limit": None,
                      "output": None, "report": str(report_path) if report_path else None}
            if reporter is not None:
                reporter.log("删除 {} 个路径，保护 {} 个候选路径；可回收载荷 {} 字节。".format(
                    len(decision["removed_paths"]), len(decision["protected_paths"]), decision["removed_unique_bytes"]))
                reporter.phase_success()
            if not analyze_only:
                if reporter is not None:
                    reporter.phase_start("Write slim rootfs")
                layer = private / "layer.tar"
                merge_rootfs(image.layers, layer, reporter, index=index)
                config = _derived_config(image.config, layer, epoch, "slim")
                if reporter is not None:
                    reporter.phase_success()
                raw_archive = private / "image.tar"
                _write_image(layer, config, raw_archive, output_tag, "docker", epoch, reporter)
                result.update(image_id=_config_digest(raw_archive, "docker"), layers=1,
                              uncompressed_archive_bytes=raw_archive.stat().st_size)
                result["under_upload_limit"] = result["uncompressed_archive_bytes"] < upload_limit
                artifact = raw_archive
                compressed_details = None
                if compressed:
                    if reporter is not None:
                        reporter.phase_start("Compress and verify slim archive")
                    artifact = private / "image.tar.gz"
                    compressed_details = _gzip(raw_archive, artifact, compress_level, reporter)
                    if reporter is not None:
                        reporter.phase_success()
                # gzip 写入时已计算压缩摘要，避免再完整读取同一压缩包。
                digest = compressed_details["sha256"] if compressed_details else "sha256:" + sha256_file(artifact)
                result.update(output=str(destination), sha256=digest,
                              after_bytes=artifact.stat().st_size)
                if before_bytes is not None:
                    result["saved_bytes"] = before_bytes - result["after_bytes"]
                if not result["under_upload_limit"] and reporter is not None:
                    reporter.log("警告：解压后的镜像归档不小于上传限制；gzip 不能绕过该限制。")
            pending_report = private / "report.json"
            if report_path is not None:
                pending_report.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            if not analyze_only:
                publish_new_file(artifact, destination)
                published.append(destination)
            if report_path is not None:
                publish_new_file(pending_report, report_path)
                published.append(report_path)
                if reporter is not None:
                    reporter.log("审计报告：" + str(report_path))
    return result


def main(argv=None):
    """统一入口与两个独立脚本共用参数；进度走 stderr，stdout 为 JSON 报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Local Docker/OCI archive or stored image reference")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--image", "--input-tag", dest="input_tag", help="Select input archive RepoTag")
    parser.add_argument("-t", "--tag", help="Output tag; defaults to selected input tag")
    parser.add_argument("--delete", action="append", default=[])
    parser.add_argument("--delete-from", action="append", type=Path, default=[])
    parser.add_argument("--keep", action="append", default=[])
    parser.add_argument("--keep-from", action="append", type=Path, default=[])
    parser.add_argument("--no-auto-protect", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--analyze-only", "--dry-run", dest="analyze_only", action="store_true")
    parser.add_argument("--largest", type=int, default=20)
    parser.add_argument("--gzip", action="store_true")
    parser.add_argument("--compress-level", type=int, default=6, choices=range(1, 10))
    parser.add_argument("--upload-limit", type=int, default=DEFAULT_LIMIT, metavar="BYTES")
    parser.add_argument("--workspace", "--temp-dir", dest="workspace", type=Path)
    parser.add_argument("--source-date-epoch", default=os.environ.get("SOURCE_DATE_EPOCH", "0"))
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    parser.add_argument("--source", choices=("local", "registry", "artifactory"))
    parser.add_argument("--image-store", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    reporter = make_reporter(args.progress, args.quiet, debug=args.debug, no_color=args.no_color, stream=sys.stderr)
    try:
        reporter.start(args.input, args.platform, mode="slim")
        settings, _ = load_settings(args.config)
        with publication_transaction() as published:
            result = slim_image(args.input, args.output, tag=args.input_tag, reference=args.tag,
                                platform=args.platform, store=args.image_store or cache_directory(settings, "imageStore", None),
                                kind=args.source, deletes=args.delete, keeps=args.keep, delete_files=args.delete_from,
                                keep_files=args.keep_from, auto_protect=not args.no_auto_protect,
                                analyze_only=args.analyze_only, report=args.report, gzip_output=args.gzip,
                                compress_level=args.compress_level, upload_limit=args.upload_limit, largest=args.largest,
                                workspace=args.workspace, source_date_epoch=args.source_date_epoch, reporter=reporter)
            for key in ("output", "report"):
                if result.get(key):
                    published.append(Path(result[key]))
            reporter.success(image=result["tag"], output=result["output"],
                             size=result.get("after_bytes"), layers=result.get("layers"))
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except KeyboardInterrupt as exc:
        reporter.fail(exc)
        return 130
    except (BuildError, OSError, ValueError, tarfile.TarError, EOFError, lzma.LZMAError, zlib.error) as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        reporter.close()


if __name__ == "__main__":
    sys.exit(main())
