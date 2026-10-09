#!/usr/bin/env python3
"""锁定声明的本地输入，并从快照执行不含 RUN 的封闭构建。"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from build_args import arguments
from dockerfile_parser import parse
from errors import BuildError
from file_publish import publish_new_file
from image_reader import sha256_file
from reproducible import parse_epoch
from compat import is_linked_directory, unlink_missing


VERSION = 1
BUFFER = 4 * 1024 * 1024
HOST_VARIABLE = re.compile(r"\$(?:BUILDPLATFORM|BUILDARCH|\{BUILDPLATFORM\}|\{BUILDARCH\})")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _inside(path, directory):
    path = Path(path).resolve()
    directory = Path(directory).resolve()
    return path == directory or directory in path.parents


def _toolchain():
    source = Path(__file__).resolve().parent
    files = {path.name: sha256_file(path) for path in sorted(source.glob("*.py"))}
    return {"python": platform.python_implementation() + " " + platform.python_version(),
            "os": sys.platform, "machine": platform.machine().lower(),
            "builder_source_sha256": _digest(files)}


def _validate_recipe(dockerfile):
    try:
        raw = Path(dockerfile).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BuildError("Hermetic Dockerfile must be UTF-8") from exc
    instructions = parse(raw)
    if any(item.op == "RUN" for item in instructions):
        raise BuildError("Strict hermetic mode rejects all RUN instructions")
    if any(item.op == "ADD" and any(source.startswith(("http://", "https://"))
                                     for source in item.value.sources) for item in instructions):
        raise BuildError("Strict hermetic mode rejects remote ADD sources")
    if HOST_VARIABLE.search(raw):
        raise BuildError("Strict hermetic mode rejects BUILDPLATFORM/BUILDARCH host variables")


def _context_manifest(context):
    context = Path(context).resolve()
    if not context.is_dir():
        raise BuildError("Build context not found: " + str(context))
    records = [{"path": ".", "type": "dir", "mode": stat.S_IMODE(context.lstat().st_mode)}]
    def visit(directory):
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            item = path.lstat()
            name = path.relative_to(context).as_posix()
            mode = stat.S_IMODE(item.st_mode)
            if stat.S_ISLNK(item.st_mode):
                raise BuildError("Strict hermetic mode rejects context symlinks: " + name)
            elif stat.S_ISDIR(item.st_mode):
                if is_linked_directory(path):
                    raise BuildError("Strict hermetic mode rejects context directory links: " + name)
                records.append({"path": name, "type": "dir", "mode": mode})
                visit(path)
            elif stat.S_ISREG(item.st_mode):
                if item.st_nlink > 1:
                    raise BuildError("Strict hermetic mode rejects context hardlinks: " + name)
                records.append({"path": name, "type": "file", "mode": mode,
                                "size": item.st_size, "sha256": sha256_file(path)})
            else:
                raise BuildError("Unsupported context entry in hermetic mode: " + name)
    visit(context)
    return records


def _map_digests(mapping_path):
    mapping_path = Path(mapping_path).resolve()
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("Invalid base-map JSON: " + str(exc)) from exc
    if not isinstance(mapping, dict) or not mapping:
        raise BuildError("Base-map must be a nonempty JSON object")
    resolved = {}
    records = {}
    for reference, value in sorted(mapping.items()):
        if not isinstance(reference, str) or not reference:
            raise BuildError("Base-map keys must be image references")
        def one(item):
            if not isinstance(item, str) or not item:
                raise BuildError("Base-map values must be paths")
            path = Path(item)
            if not path.is_absolute():
                path = mapping_path.parent / path
            path = path.resolve()
            if not path.is_file():
                raise BuildError("Base archive not found: " + str(path))
            return path, sha256_file(path)
        if isinstance(value, dict):
            if not value:
                raise BuildError("Empty platform mapping for " + reference)
            resolved[reference] = {}
            records[reference] = {}
            for target, item in sorted(value.items()):
                if target not in ("linux/amd64", "linux/arm64"):
                    raise BuildError("Unsupported base-map platform: " + str(target))
                path, digest = one(item)
                resolved[reference][target] = path
                records[reference][target] = digest
        else:
            path, digest = one(value)
            resolved[reference] = path
            records[reference] = digest
    return resolved, records


def make_lock(dockerfile, context, base_tar=None, base_map=None, *, tag,
              target_platform="linux/amd64", target_stage=None, image_format="docker",
              source_date_epoch=0, build_args=None):
    """记录 Dockerfile、上下文、基础镜像及工具链的精确输入，供后续封闭构建校验。"""
    if Path(dockerfile).is_symlink():
        raise BuildError("Strict hermetic mode rejects a symlink Dockerfile")
    dockerfile = Path(dockerfile).resolve()
    context = Path(context).resolve()
    if not dockerfile.is_file():
        raise BuildError("Dockerfile not found: " + str(dockerfile))
    if base_tar is not None and base_map is not None:
        raise BuildError("Choose --base-tar or --base-map")
    if target_platform not in ("linux/amd64", "linux/arm64") or image_format not in ("docker", "oci", "both"):
        raise BuildError("Unsupported hermetic platform or format")
    if os.environ.get("PYTHONHASHSEED") != "0" or sys.flags.hash_randomization:
        raise BuildError("Start Python with PYTHONHASHSEED=0 before locking or building")
    _validate_recipe(dockerfile)
    if base_tar is not None:
        path = Path(base_tar).resolve()
        if not path.is_file():
            raise BuildError("Base archive not found: " + str(path))
        base = {"kind": "tar", "sha256": sha256_file(path)}
    elif base_map is not None:
        _, records = _map_digests(base_map)
        base = {"kind": "map", "entries": records}
    else:
        base = {"kind": "none"}
    args = dict(build_args or {})
    return {"schemaVersion": VERSION, "policy": "no-run-local-snapshot-v1",
            "toolchain": _toolchain(),
            "parameters": {"tag": tag, "platform": target_platform,
                           "target": target_stage, "format": image_format,
                           "sourceDateEpoch": parse_epoch(source_date_epoch),
                           "buildArgNames": sorted(args),
                           "buildArgsSha256": _digest(args)},
            "inputs": {"dockerfileSha256": sha256_file(dockerfile),
                       "context": _context_manifest(context), "base": base}}


def _copy_context(source, destination):
    source = Path(source).resolve()
    destination.mkdir()
    directories = []
    def visit(directory, target):
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            output = target / path.name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode):
                raise BuildError("Context changed to a symlink: " + str(path))
            elif stat.S_ISDIR(item.st_mode):
                # 暂存阶段也检查 junction，避免锁定后目录被替换为链接再复制外部数据。
                if is_linked_directory(path):
                    raise BuildError("Context changed to a directory link: " + str(path))
                output.mkdir()
                directories.append((output, stat.S_IMODE(item.st_mode)))
                visit(path, output)
            elif stat.S_ISREG(item.st_mode):
                with open(path, "rb") as input_stream, open(output, "wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, BUFFER)
                os.chmod(output, stat.S_IMODE(item.st_mode))
            else:
                raise BuildError("Context changed to an unsupported file type: " + str(path))
    visit(source, destination)
    for path, mode in reversed(directories):
        os.chmod(path, mode)
    os.chmod(destination, stat.S_IMODE(source.lstat().st_mode))


def _copy_base(base_tar, base_map, root):
    if base_tar is not None:
        destination = root / "base.tar"
        shutil.copyfile(base_tar, destination)
        return destination, None
    if base_map is None:
        return None, None
    resolved, records = _map_digests(base_map)
    by_digest = {}
    translated = {}
    for reference, value in resolved.items():
        def copy_one(path, digest):
            if digest not in by_digest:
                destination = root / ("base-" + digest + ".tar")
                shutil.copyfile(path, destination)
                if sha256_file(destination) != digest:
                    raise BuildError("Base archive changed while staging")
                by_digest[digest] = destination
            return str(by_digest[digest])
        if isinstance(value, dict):
            translated[reference] = {key: copy_one(path, records[reference][key])
                                     for key, path in value.items()}
        else:
            translated[reference] = copy_one(value, records[reference])
    destination = root / "base-map.json"
    destination.write_bytes(_json(translated))
    return None, destination


def build_locked(lock_path, dockerfile, context, base_tar, base_map, tag, output, *,
                 target_platform="linux/amd64", target_stage=None, image_format="docker",
                 oci_output=None, source_date_epoch=0, build_args=None):
    """从通过锁文件校验的输入快照构建镜像。

    暂存前和发布前均核对原始输入；构建使用暂存副本，避免普通文件修改悄悄改变结果。
    锁定规则排除 RUN 和远端输入。
    """
    from builder import build

    lock_path = Path(lock_path).resolve()
    context = Path(context).resolve()
    output = Path(output).resolve()
    second = (Path(oci_output).resolve() if oci_output is not None else
              output.with_name(output.stem + ".oci.tar")) if image_format == "both" else None
    report_path = Path(str(output) + ".hermetic.json")
    for candidate in (lock_path, output, second, report_path):
        if candidate is not None and _inside(candidate, context):
            raise BuildError("Hermetic lock/output must be outside the build context")
    for candidate in (output, second, report_path):
        if candidate is not None and candidate.exists():
            raise BuildError("Hermetic output already exists: " + str(candidate))
    if not lock_path.is_file():
        raise BuildError("Input lock not found: " + str(lock_path))
    try:
        expected = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("Invalid input lock: " + str(exc)) from exc
    actual = make_lock(dockerfile, context, base_tar, base_map, tag=tag,
                       target_platform=target_platform, target_stage=target_stage,
                       image_format=image_format, source_date_epoch=source_date_epoch,
                       build_args=build_args)
    if actual != expected:
        raise BuildError("Hermetic input lock differs from current inputs or toolchain")
    output.parent.mkdir(parents=True, exist_ok=True)
    if second is not None:
        second.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-hermetic-", dir=output.parent) as temp:
        root = Path(temp)
        staged_context = root / "context"
        _copy_context(context, staged_context)
        source_dockerfile = Path(dockerfile).resolve()
        if _inside(source_dockerfile, context):
            staged_dockerfile = staged_context / source_dockerfile.relative_to(context)
        else:
            staged_dockerfile = root / "Dockerfile"
            shutil.copyfile(source_dockerfile, staged_dockerfile)
        staged_base, staged_map = _copy_base(base_tar, base_map, root)
        staged = make_lock(staged_dockerfile, staged_context, staged_base, staged_map,
                           tag=tag, target_platform=target_platform,
                           target_stage=target_stage, image_format=image_format,
                           source_date_epoch=source_date_epoch, build_args=build_args)
        if staged != expected:
            raise BuildError("Staged input snapshot does not match the lock")
        partial = root / "image.tar"
        partial_oci = root / "image.oci.tar" if second is not None else None
        build(staged_dockerfile, staged_context, staged_base, staged_map, tag, partial,
              enable_run=False, workspace_dir=root, cache_dir=None, target_stage=target_stage,
              image_format=image_format, oci_output=partial_oci,
              source_date_epoch=source_date_epoch, target_platform=target_platform,
              build_args=build_args)
        if make_lock(dockerfile, context, base_tar, base_map, tag=tag,
                     target_platform=target_platform, target_stage=target_stage,
                     image_format=image_format, source_date_epoch=source_date_epoch,
                     build_args=build_args) != expected:
            raise BuildError("Original inputs changed during the hermetic build")
        report = {"schemaVersion": VERSION, "policy": expected["policy"],
                  "inputLockSha256": _digest(expected),
                  "outputs": {"primarySha256": sha256_file(partial),
                              "ociSha256": sha256_file(partial_oci) if partial_oci else None}}
        report_temp = root / "report.json"
        report_temp.write_bytes(_json(report) + b"\n")
        published = []
        try:
            for source, destination in ((partial, output), (partial_oci, second),
                                        (report_temp, report_path)):
                if source is not None:
                    publish_new_file(source, destination)
                    published.append(destination)
        except OSError:
            for destination in published:
                unlink_missing(destination)
            raise
    return output, report_path


def main(argv=None):
    """独立脚本入口：生成封闭构建的输入锁文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dockerfile", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base-tar", type=Path)
    source.add_argument("--base-map", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                        default="linux/amd64")
    parser.add_argument("--target")
    parser.add_argument("--format", choices=("docker", "oci", "both"), default="docker")
    parser.add_argument("--source-date-epoch", required=True)
    parser.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--output", type=Path, required=True, help="New input-lock JSON path")
    args = parser.parse_args(argv)
    try:
        if _inside(args.output, args.context) or args.output.exists():
            raise BuildError("Input lock must be a new file outside context")
        lock = make_lock(args.dockerfile, args.context, args.base_tar, args.base_map,
                         tag=args.tag, target_platform=args.platform,
                         target_stage=args.target, image_format=args.format,
                         source_date_epoch=args.source_date_epoch,
                         build_args=arguments(args.build_arg))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "xb") as stream:
            stream.write(_json(lock) + b"\n")
    except (BuildError, OSError) as exc:
        parser.exit(1, "Lock failed: {}\n".format(exc))
    print("Created {}".format(args.output.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
