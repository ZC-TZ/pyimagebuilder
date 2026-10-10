#!/usr/bin/env python3
"""无 Docker 导出 rootfs、导入单层镜像并保留运行配置扁平化镜像。"""

import argparse
import copy
import json
import lzma
import os
import shutil
import sys
import tarfile
import tempfile
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from cas_store import CASStore, docker_archive_tag
from compat import remove_suffix
from errors import ArchiveError, BuildError
from file_publish import publish_new_file
from image_changes import apply_changes, parse_changes
from image_reader import ImageArchiveReader, archive_members, sha256_file
from image_store import _archive_path, default_store
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from platforms import architecture, normalize_platform
from progress import make_reporter
from registry import parse_reference
from reproducible import parse_epoch, timestamp
from rootfs import RootFSIndex, clean_path
from settings import cache_directory, load_settings


BUFFER = 4 * 1024 * 1024


def image_tag(value):
    """校验本地输出标签；允许短名称，缺省 tag 使用 latest，不接受 digest 标签。"""
    if not isinstance(value, str) or "@" in value:
        raise BuildError("Output image requires a repository[:tag], not a digest reference")
    host = value.split("/", 1)[0]
    explicit_host = "/" in value and ("." in host or ":" in host or host == "localhost")
    parse_reference(value if explicit_host else "local.invalid/" + value)
    return value if ":" in value.rsplit("/", 1)[-1] else value + ":latest"


def _member_name(name, allow_root=True):
    """严格检查 tar 路径，不允许反斜杠被悄悄改写成 Linux 路径。"""
    if "\\" in name or "\x00" in name:
        raise ArchiveError("Invalid rootfs archive path: " + name)
    result = clean_path(name)
    if not result and not allow_root:
        raise ArchiveError("Empty rootfs archive link target")
    return result


def _supported(member):
    """只接受可作为 Linux rootfs 表示的类型，不在宿主创建设备或 FIFO。"""
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk() or
            member.isfifo() or member.ischr() or member.isblk()):
        raise ArchiveError("Unsupported rootfs tar member type: " + member.name)


def _header(member, name):
    """复制镜像元数据；改名时移除旧路径/大小覆盖，稀疏文件展开为普通文件。"""
    result = copy.copy(member)
    result.name = name
    result.pax_headers = {key: value for key, value in member.pax_headers.items()
                          if key not in ("path", "linkpath", "size") and
                          not key.startswith("GNU.sparse.")}
    if member.isfile():
        result.type = tarfile.REGTYPE
        result.sparse = None
    else:
        result.size = 0
    return result


class _LayerFiles:
    """限量复用 tar 句柄；通过预读 TarInfo 的偏移取内容，不再逐文件扫描层。"""
    def __init__(self, limit=8):
        self.limit = limit
        self.opened = OrderedDict()

    def get(self, path):
        if path in self.opened:
            archive = self.opened.pop(path)
        else:
            archive = tarfile.open(path, "r:")
            if len(self.opened) >= self.limit:
                _, old = self.opened.popitem(last=False)
                old.close()
        self.opened[path] = archive
        return archive

    def close(self):
        for archive in self.opened.values():
            archive.close()
        self.opened.clear()


def merge_rootfs(layers, output, reporter=None):
    """按层应用 whiteout，输出最终文件树；保留 inode 分组与文件元数据。

    内容直接由原始层流式复制。硬链接先前目标被覆盖/删除时，仍保留原 inode
    的内容；选一个可见名字先写成普通文件，其余名字链接到它。
    """
    index = RootFSIndex()
    root_member = None
    for number, layer in enumerate(layers, 1):
        index.apply_layer(layer)
        if reporter is not None:
            reporter.progress(number, len(layers), "Indexing source layers", unit="layers")
    needed = {(entry.layer, entry.member) for entry in index.entries.values() if entry.layer}
    metadata = {}
    # 只保存最终可见 inode 的头；即使 LRU 句柄被淘汰，再打开也可直接按偏移读数据。
    # 覆盖掉的下层文件若仍有硬链接，其 backing member 也在 needed 中。
    for layer in layers:
        with tarfile.open(layer, "r:") as archive:
            for member in archive:
                name = _member_name(member.name)
                _supported(member)
                if not name:
                    if not member.isdir():
                        raise ArchiveError("Root tar member must be a directory")
                    root_member = member
                if member.islnk():
                    _member_name(member.linkname.lstrip("/"), allow_root=False)
                key = (str(layer), member.name)
                if key in needed:
                    metadata[key] = member
    opened = _LayerFiles()
    inodes = {}
    try:
        with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as target:
            if root_member is not None:
                target.addfile(_header(root_member, "."))
            for number, name in enumerate(sorted(index.entries), 1):
                entry = index.entries[name]
                if entry.layer:
                    archive = opened.get(entry.layer)
                    member = metadata[(entry.layer, entry.member)]
                    info = _header(entry.metadata or member, name)
                else:
                    archive = None
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                if entry.kind in ("file", "hardlink"):
                    key = id(entry.inode)
                    if key in inodes:
                        info.type, info.linkname, info.size = tarfile.LNKTYPE, inodes[key], 0
                        target.addfile(info)
                    else:
                        inodes[key] = name
                        info.type, info.linkname = tarfile.REGTYPE, ""
                        info.size = member.size
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ArchiveError("Unreadable rootfs file: " + name)
                        with stream:
                            target.addfile(info, stream)
                else:
                    target.addfile(info)
                if reporter is not None:
                    reporter.progress(number, len(index.entries), "Writing merged rootfs", unit="files")
    finally:
        opened.close()
    return len(index.entries)


def prepare_rootfs(source, output):
    """读取本地 rootfs tar（可 gzip/bzip2/xz），规范化为经过校验的单层 tar。

    不解包到宿主；拒绝重复路径、层 whiteout 保留名及通过链接父目录的内容。
    rootfs 里的 .wh.* 是实际文件，不能误按镜像层删除标记处理，因此明确拒绝。
    """
    seen = set()
    with tarfile.open(source, "r:*") as archive, \
            tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as target:
        for member in archive:
            name = _member_name(member.name)
            _supported(member)
            if not name:
                if not member.isdir():
                    raise ArchiveError("Root tar member must be a directory")
                name = "."
            if name in seen:
                raise ArchiveError("Duplicate path in rootfs archive: " + name)
            seen.add(name)
            if any(part.startswith(".wh.") for part in name.split("/")):
                raise ArchiveError("Reserved OCI whiteout path in rootfs archive: " + name)
            info = _header(member, name)
            if member.islnk():
                info.linkname = _member_name(member.linkname.lstrip("/"), allow_root=False)
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError("Unreadable rootfs file: " + name)
                with stream:
                    target.addfile(info, stream)
            else:
                target.addfile(info)
    index = RootFSIndex()
    index.apply_layer(output)
    return len(index.entries)


@contextmanager
def open_image(source, workspace, tag=None, platform="linux/amd64", store=None, kind=None):
    """从归档或本地引用打开镜像；CAS 直接提供层，无需转换成中间 Docker tar。"""
    platform = normalize_platform(platform)
    path = Path(source)
    if path.is_file():
        with tarfile.open(path, "r:*") as archive:
            members = archive_members(archive)
            is_docker = "manifest.json" in members
        if is_docker:
            from image_cli import _select_tag
            selected = _select_tag(path, tag)
            image = ImageArchiveReader(path, workspace / "layers").read(selected, platform)
        else:
            from optimizer import load_image
            _, selected, image = load_image(path, workspace, tag, with_image=True)
            if image.config.get("os") != "linux" or image.config.get("architecture") != architecture(platform):
                raise BuildError("Image archive platform does not match " + platform)
        image.original_config_bytes()
        yield image, selected
        return
    # 明确的文件路径不能被误当成待解析镜像引用，更不会触发联网下载。
    if path.is_absolute() or str(source).startswith((".", "\\")) or "\\" in str(source):
        raise BuildError("Image archive not found: " + str(source))
    reference = str(source) if "@" in str(source) else image_tag(str(source))
    if tag is not None:
        raise BuildError("--tag selects an archive image; specify stored reference directly")
    root = Path(store or default_store()).resolve()
    cas = CASStore(root)
    kinds = (kind,) if kind else ("local", "registry", "artifactory")
    matches = [item for item in kinds if cas.has_ref(reference, platform, item) or
               _archive_path(root, reference, platform, item).is_file()]
    if not matches:
        raise BuildError("Image not present in PyImageBuilder store: " + reference)
    if len(matches) != 1:
        raise BuildError("Multiple stored sources; specify --source")
    selected_kind = matches[0]
    with cas.ref_lock(reference, platform, selected_kind):
        if cas.has_ref(reference, platform, selected_kind):
            image = cas.open_base(reference, platform, selected_kind)
        else:
            path = _archive_path(root, reference, platform, selected_kind)
            image = ImageArchiveReader(path, workspace / "layers").read(docker_archive_tag(reference), platform)
        image.original_config_bytes()
        yield image, docker_archive_tag(reference)


def _output_path(output):
    """预检新输出和父目录；最终发布仍使用排他操作防止竞争覆盖。"""
    if str(output) == "-":
        raise BuildError("Image output requires a file path; only export supports stdout")
    requested = Path(output)
    if requested.exists() or requested.is_symlink():
        raise BuildError("Output must be a new file: " + str(requested))
    path = requested.resolve()
    if path.exists():
        raise BuildError("Output must be a new file: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_rootfs(source, output, tag=None, platform="linux/amd64", store=None,
                  kind=None, reporter=None, stdout=None):
    """导出文件系统，不携带 config/manifest/history；'-' 仅写二进制标准输出。"""
    binary = str(output) == "-"
    destination = None if binary else _output_path(output)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-rootfs-",
                                     dir=destination.parent if destination else None) as temporary:
        workspace = Path(temporary)
        with open_image(source, workspace, tag, platform, store, kind) as (image, selected):
            if reporter is not None:
                reporter.phase_start("Merge and export rootfs")
            merged = workspace / "rootfs.tar"
            files = merge_rootfs(image.layers, merged, reporter)
            digest, size = sha256_file(merged), merged.stat().st_size
            if binary:
                stream = stdout if stdout is not None else sys.stdout.buffer
                with open(merged, "rb") as data:
                    shutil.copyfileobj(data, stream, BUFFER)
                stream.flush()
            else:
                publish_new_file(merged, destination)
            if reporter is not None:
                reporter.phase_success()
            return {"operation": "export", "tag": selected, "output": "-" if binary else str(destination),
                    "format": "rootfs", "files": files, "bytes": size, "sha256": digest}


def _derived_config(config, layer, epoch, operation, message=None):
    """扁平化确实替换了层链，显式生成新配置/身份，不尝试冒用原 ImageID。"""
    result = copy.deepcopy(config)
    result["created"] = timestamp(epoch)
    result["rootfs"] = {"type": "layers", "diff_ids": ["sha256:" + sha256_file(layer)]}
    history = {"created": timestamp(epoch), "created_by": "PyImageBuilder " + operation}
    if message is not None:
        history["comment"] = message
    result["history"] = [history]
    return result


def _write_image(layer, config, output, tag, format, epoch, reporter=None, image=None):
    """通过派生镜像接口写私有产物，验证完成后才允许调用方发布。"""
    if format not in ("docker", "oci"):
        raise BuildError("Image output format must be docker or oci")
    writer = ImageArchiveWriter(epoch) if format == "docker" else OCIImageWriter(epoch)
    if reporter is not None:
        reporter.phase_start("Write and verify " + format + " image")
    if format == "oci" and image is not None and image.source_manifest.get("schemaVersion") == 2:
        template = copy.deepcopy(image.source_manifest)
        # 合并后的层不继承任一旧层的注解；镜像级及 config descriptor 扩展可保留。
        template["layers"] = [{}]
        writer.write_new(output, config, [layer], tag, progress=reporter.progress if reporter else None,
                         manifest_template=template, index_template=image.source_index)
    else:
        writer.write_new(output, config, [layer], tag, progress=reporter.progress if reporter else None)
    writer.verify(output, tag)
    if reporter is not None:
        reporter.phase_success()


def import_rootfs(source, reference, output, changes=None, platform="linux/amd64",
                  format="docker", source_date_epoch=0, message=None, reporter=None, stdin=None):
    """从 rootfs 创建一层新镜像；不继承任何原镜像配置，也不自动注册到镜像库。"""
    tag = image_tag(reference)
    platform, epoch = normalize_platform(platform), parse_epoch(source_date_epoch)
    instructions = parse_changes(changes)
    destination = _output_path(output)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-import-", dir=destination.parent) as temporary:
        workspace = Path(temporary)
        if str(source) == "-":
            source = workspace / "stdin.tar"
            with open(source, "wb") as target:
                shutil.copyfileobj(stdin if stdin is not None else sys.stdin.buffer, target, BUFFER)
        if reporter is not None:
            reporter.phase_start("Validate rootfs archive")
        layer = workspace / "layer.tar"
        files = prepare_rootfs(source, layer)
        config = _derived_config({"os": "linux", "architecture": architecture(platform), "config": {}},
                                 layer, epoch, "import", message)
        config = apply_changes(config, instructions)
        if reporter is not None:
            reporter.phase_success()
        partial = workspace / "image.tar"
        _write_image(layer, config, partial, tag, format, epoch, reporter)
        digest = sha256_file(partial)
        result = {"operation": "import", "tag": tag, "output": str(destination), "format": format,
                  "files": files, "layers": 1, "image_id": _config_digest(partial, format),
                  "sha256": digest}
        publish_new_file(partial, destination)
        return result


def _config_digest(path, format):
    """从已校验产物读取实际配置身份，不重新序列化解析后的配置。"""
    with tarfile.open(path, "r:") as archive:
        if format == "docker":
            manifest = json.load(archive.extractfile("manifest.json"))
            return "sha256:" + remove_suffix(manifest[0]["Config"], ".json")
        index = json.load(archive.extractfile("index.json"))
        name = "blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]
        return json.load(archive.extractfile(name))["config"]["digest"]


def flatten_image(source, output, reference=None, tag=None, platform="linux/amd64",
                  store=None, kind=None, changes=None, format="docker", source_date_epoch=0,
                  reporter=None):
    """将现有镜像合成一层；保留运行配置，可用 --change 显式覆盖部分字段。"""
    platform, epoch = normalize_platform(platform), parse_epoch(source_date_epoch)
    instructions = parse_changes(changes)
    destination = _output_path(output)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-flatten-", dir=destination.parent) as temporary:
        workspace = Path(temporary)
        with open_image(source, workspace, tag, platform, store, kind) as (image, selected):
            output_tag = image_tag(reference or selected)
            if reporter is not None:
                reporter.phase_start("Flatten source layers")
            layer = workspace / "layer.tar"
            files = merge_rootfs(image.layers, layer, reporter)
            config = apply_changes(_derived_config(image.config, layer, epoch, "flatten"), instructions)
            if reporter is not None:
                reporter.phase_success()
            partial = workspace / "image.tar"
            _write_image(layer, config, partial, output_tag, format, epoch, reporter, image)
            digest = sha256_file(partial)
            result = {"operation": "flatten", "tag": output_tag, "output": str(destination),
                      "format": format, "files": files, "before_layers": len(image.layers), "layers": 1,
                      "source_image_id": image.config_digest, "image_id": _config_digest(partial, format),
                      "sha256": digest}
            publish_new_file(partial, destination)
            return result


def main(argv=None):
    """独立入口与 main.py 共用分派；进度写 stderr，不污染二进制管道或 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export an image's merged filesystem as rootfs tar")
    importing = commands.add_parser("import", help="Create a single-layer image from rootfs tar")
    flatten = commands.add_parser("flatten", help="Merge layers while preserving image runtime config")
    for command in (export, importing, flatten):
        command.add_argument("input", help="Stored image or image archive; import takes rootfs tar or -")
        command.add_argument("-o", "--output", required=command is not export, default="-",
                             help="New output file; export defaults to binary stdout")
        command.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
        command.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
        command.add_argument("--quiet", action="store_true")
        command.add_argument("--debug", action="store_true")
        command.add_argument("--no-color", action="store_true")
    for command in (export, flatten):
        command.add_argument("--tag" if command is export else "--input-tag", dest="tag",
                             help="Select input tag in an image archive")
        command.add_argument("--source", choices=("local", "registry", "artifactory"))
        command.add_argument("--image-store", type=Path)
        command.add_argument("--config", type=Path)
    for command in (importing, flatten):
        command.add_argument("-c", "--change", action="append", default=[], help="Metadata Dockerfile instruction")
        command.add_argument("--format", choices=("docker", "oci"), default="docker")
        command.add_argument("--source-date-epoch", default=os.environ.get("SOURCE_DATE_EPOCH", "0"))
    importing.add_argument("reference", help="Output repository[:tag]")
    importing.add_argument("-m", "--message", help="Import history comment")
    flatten.add_argument("-t", "--tag", "--image-tag", dest="reference",
                         help="Output repository[:tag]; defaults to input tag")
    args = parser.parse_args(argv)
    reporter = make_reporter(args.progress, args.quiet, debug=args.debug,
                             no_color=args.no_color, stream=sys.stderr)
    reporter.start(args.input, args.platform, mode=args.command)
    try:
        if args.command == "import":
            result = import_rootfs(args.input, args.reference, args.output, args.change, args.platform,
                                   args.format, args.source_date_epoch, args.message, reporter)
        else:
            settings, _ = load_settings(args.config)
            store = args.image_store or cache_directory(settings, "imageStore", None)
            if args.command == "export":
                result = export_rootfs(args.input, args.output, args.tag, args.platform, store,
                                       args.source, reporter)
            else:
                result = flatten_image(args.input, args.output, args.reference, args.tag, args.platform,
                                       store, args.source, args.change, args.format, args.source_date_epoch, reporter)
        reporter.success(**result)
        if args.command != "export" or args.output != "-":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BuildError, OSError, tarfile.TarError, ValueError, EOFError, lzma.LZMAError, zlib.error) as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
