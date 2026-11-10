#!/usr/bin/env python3
"""仅将原始 tar 包装成 gzip：保留全部 tar 字节、标签、平台、层和镜像身份。"""

import argparse
import json
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
from image_reader import verify_layer_tar
from progress import make_reporter
from rootfs_archive import _output_path
from slim_rules import DEFAULT_LIMIT


def compress_archive(source, output=None, *, compress_level=6, report=None,
                     upload_limit=DEFAULT_LIMIT, workspace=None, reporter=None, source_label=None):
    """只接受本地未压缩 tar，输出新 gzip 和可选报告，不联网或自动入库。

    tar 结构预检不等于镜像语义验证。通过同一只读句柄压缩，并验证 gzip 解码
    结果与实际读取的输入完全一致；无需创建另一份未压缩大 tar 或解包 rootfs。
    发布事务覆盖源句柄关闭、临时清理和报告发布，失败时只回收本次产物。
    """
    if type(compress_level) is not int or not 1 <= compress_level <= 9:
        raise BuildError("gzip level must be between 1 and 9")
    if type(upload_limit) is not int or upload_limit <= 0:
        raise BuildError("Upload limit must be a positive byte count")
    source = Path(os.path.abspath(Path(source).expanduser()))
    requested = output if output is not None else source.with_name(
        source.name + (".gz" if source.suffix.lower() == ".tar" else ".tar.gz"))
    if not str(requested).lower().endswith((".tar.gz", ".tgz")):
        raise BuildError("Compression output requires .tar.gz or .tgz suffix")
    destination = _output_path(requested)
    report_path = _output_path(report) if report is not None else None
    if report_path is not None and report_path.suffix.lower() != ".json":
        raise BuildError("Compression report requires a new .json file")
    parent = Path(workspace).expanduser().resolve() if workspace is not None else destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    with publication_transaction() as published, \
            tempfile.TemporaryDirectory(prefix="pyimagebuilder-compress-", dir=parent) as temporary, \
            open_regular_file(source) as stream:
        initial = stat_identity(os.fstat(stream.fileno()))
        identity = file_identity(source, content=False)
        if initial[:4] != identity[:4]:
            raise BuildError("Compression input changed before read")
        if reporter is not None:
            reporter.phase_start("Check compression input")
        verify_layer_tar(stream, str(source))
        if reporter is not None:
            reporter.phase_success()
            reporter.phase_start("Compress and verify original archive")
        stream.seek(0)
        artifact = Path(temporary) / "image.tar.gz"
        details = gzip_stream(stream, artifact, initial[2], compress_level, reporter)
        if (stat_identity(os.fstat(stream.fileno())) != initial or
                file_identity(source, content=False) != identity):
            raise BuildError("Compression input changed during read")
        if reporter is not None:
            reporter.phase_success()
        result = dict(details, schemaVersion=1, operation="compress",
                      source=str(source) if source_label is None else str(source_label),
                      output=str(destination), report=str(report_path) if report_path else None,
                      compression="gzip", compress_level=compress_level, archive_bytes_preserved=True,
                      before_bytes=initial[2], saved_bytes=initial[2] - details["after_bytes"],
                      upload_limit=upload_limit, under_upload_limit=initial[2] < upload_limit)
        if not result["under_upload_limit"] and reporter is not None:
            reporter.log("警告：未压缩 tar 不小于上传限制，gzip 不改变解压后的大小。")
        pending_report = Path(temporary) / "report.json"
        if report_path is not None:
            pending_report.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        publish_new_file(artifact, destination)
        published.append(destination)
        if report_path is not None:
            publish_new_file(pending_report, report_path)
            published.append(report_path)
    return result


def main(argv=None):
    """独立入口和 main.py compress 共用参数；stdout 为摘要，进度写 stderr。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Local uncompressed Docker/OCI/rootfs tar")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--compress-level", type=int, choices=range(1, 10), default=6)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--upload-limit", type=int, default=DEFAULT_LIMIT, metavar="BYTES")
    parser.add_argument("--workspace", "--temp-dir", dest="workspace", type=Path)
    parser.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    reporter = make_reporter(args.progress, args.quiet, debug=args.debug,
                             no_color=args.no_color, stream=sys.stderr)
    try:
        with publication_transaction() as published:
            reporter.start(str(args.input), "original", mode="compress")
            result = compress_archive(args.input, args.output, compress_level=args.compress_level,
                                      report=args.report, upload_limit=args.upload_limit,
                                      workspace=args.workspace, reporter=reporter)
            published.append(Path(result["output"]))
            if result["report"]:
                published.append(Path(result["report"]))
            reporter.success(output=result["output"], size=result["after_bytes"])
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except KeyboardInterrupt as exc:
        reporter.fail(exc)
        return 130
    except (BuildError, OSError, ValueError, tarfile.TarError, EOFError, zlib.error) as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        reporter.close()


if __name__ == "__main__":
    sys.exit(main())
