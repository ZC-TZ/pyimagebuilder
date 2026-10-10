"""生成本地 COPY/ADD 层，保留所需的文件类型、权限和属主信息。"""

import glob
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import stat
import tarfile
from pathlib import Path

from compat import is_linked_directory
from errors import ArchiveError, BuildError, UnsupportedInstruction
from image_reader import sha256_file
from rootfs import clean_path


def container_path(value, workdir="/"):
    """按 WORKDIR 解析 Dockerfile 路径，并限制在镜像根目录内。"""
    if not value:
        raise BuildError("Empty container path")
    joined = value if value.startswith("/") else posixpath.join(workdir, value)
    result = posixpath.normpath(joined)
    if not result.startswith("/") or result.startswith("/../"):
        raise BuildError("Invalid container path: " + value)
    return result


def _safe_name(name):
    if "\\" in name:
        raise ArchiveError("Backslash in archive path: " + name)
    result = clean_path(name)
    if any(part.startswith(".wh.") for part in result.split("/")):
        raise UnsupportedInstruction("Reserved OCI whiteout path: " + name)
    return result


def _matches_exclude(name, patterns):
    """匹配排除规则时同时排除该路径的所有后代。"""
    parts = name.strip("/").split("/")
    for end in range(1, len(parts) + 1):
        candidate = "/".join(parts[:end])
        if any(pattern.rstrip("/") and
               (fnmatch.fnmatchcase(candidate, pattern.rstrip("/")) or
                fnmatch.fnmatchcase(parts[end - 1], pattern.rstrip("/")))
               for pattern in patterns):
            return True
    return False


def check_context_source(context, path):
    """允许复制符号链接自身，拒绝穿过目录链接读取上下文外的数据。

    Windows junction 在 lstat 中表现为目录，必须单独识别，不能仅检查 is_symlink。
    context 应为已解析的根路径；path 保留词法路径，以便逐级检查源的父目录。
    """
    path = Path(path)
    try:
        parts = path.relative_to(context).parts
    except ValueError as exc:
        raise BuildError("Source escapes build context: " + str(path)) from exc
    current = context
    for part in parts[:-1]:
        current /= part
        if is_linked_directory(current):
            raise UnsupportedInstruction("Source through a context directory link is unsupported: " + str(path))
    if not path.is_symlink():
        if not path.exists():
            raise BuildError("Source not found: " + str(path))
        if path.is_dir() and is_linked_directory(path):
            raise UnsupportedInstruction("Context directory junction is unsupported: " + str(path))


def context_sources(context, pattern):
    """在读取每一级目录前验证边界，安全展开本地源通配符；无匹配时返回空列表。

    不使用 recursive=True 的 glob，它会先递归链接目标再返回结果，事后校验无法
    阻止越界扫描或目录循环。** 只递归普通目录，源符号链接仍可作为最终节点复制。
    """
    if (pattern.startswith("/") or "\\" in pattern or Path(pattern).drive or
            any(part == ".." for part in pattern.split("/"))):
        raise BuildError("Source escapes build context: " + pattern)
    if "://" in pattern or pattern.startswith("git@"):
        raise UnsupportedInstruction("Remote sources are unsupported: " + pattern)

    def walk(current, parts):
        if not parts:
            if current.exists() or current.is_symlink():
                check_context_source(context, current)
                yield current
            return
        if parts[0] in ("", "."):
            yield from walk(current, parts[1:])
            return
        if not current.exists() and not current.is_symlink():
            return
        check_context_source(context, current)
        if current.is_symlink():
            raise UnsupportedInstruction("Source through a context directory link is unsupported: " + str(current))
        if not current.is_dir():
            return
        if parts[0] == "**":
            yield from walk(current, parts[1:])
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
            for entry in children:
                if entry.name.startswith("."):
                    continue
                child = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    check_context_source(context, child)
                    yield from walk(child, parts)
                elif len(parts) == 1:
                    yield from walk(child, ())
        else:
            # 只有源表达式是 glob；上下文目录自身的 [] 等字符必须按字面量处理。
            expression = glob.escape(str(current)) + os.sep + parts[0]
            for match in sorted(glob.glob(expression)):
                yield from walk(Path(match), parts[1:])

    return sorted(walk(Path(context), pattern.split("/")))


def _identity_file(rootfs, path):
    raw = rootfs.read_file(path)
    if raw is None:
        raise BuildError("Image identity file missing: " + path)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("Image identity file is not UTF-8: " + path) from exc


def resolve_chown(value, rootfs):
    """从数字参数或基础 rootfs 账户文件解析 --chown，返回 (UID, GID)。

    未提供 value 时返回 None；省略组时，数字 UID 使用同值 GID，用户名使用 passwd 主组。
    名称查找使用镜像内的 passwd/group，不能读取宿主账户；名称不存在时抛出 BuildError。
    """
    if value is None:
        return None
    user, separator, group = value.partition(":")
    if not user or (separator and not group):
        raise BuildError("Invalid --chown value: " + value)
    if user.isdecimal():
        uid = int(user)
        primary_gid = uid
    else:
        uid = None
        for line in _identity_file(rootfs, "etc/passwd").splitlines():
            fields = line.split(":")
            if (len(fields) >= 4 and fields[0] == user and
                    fields[2].isdecimal() and fields[3].isdecimal()):
                uid = int(fields[2])
                primary_gid = int(fields[3])
                break
        if uid is None:
            raise BuildError("--chown user not found in image: " + user)
    if not separator:
        return uid, primary_gid
    if group.isdecimal():
        return uid, int(group)
    for line in _identity_file(rootfs, "etc/group").splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[0] == group and fields[2].isdecimal():
            return uid, int(fields[2])
    raise BuildError("--chown group not found in image: " + group)


class LayerBuilder:
    """为 COPY、ADD、WORKDIR 和 VOLUME 生成保留文件元数据的层。"""
    def __init__(self, context, rootfs, source_date_epoch=0, reporter=None):
        self.context = Path(context).resolve()
        self.rootfs = rootfs
        self.source_date_epoch = source_date_epoch
        self.reporter = reporter
        if not self.context.is_dir():
            raise BuildError("Build context not found: " + str(self.context))

    def _excluded(self, path, transfer):
        relative = path.relative_to(self.context).as_posix()
        return _matches_exclude(relative, transfer.exclude)

    def _parent_name(self, path, expression):
        relative = path.relative_to(self.context).as_posix()
        if "/./" in expression:
            # 规范化路径前缀，不能用 lstrip 字符集合误删隐藏目录名中的点。
            prefix = clean_path(expression.split("/./", 1)[0])
            if relative.startswith(prefix + "/"):
                relative = relative[len(prefix) + 1:]
        return relative

    def _add_data(self, archive, info, stream):
        if self.reporter is None or info.size < 4 * 1024 * 1024:
            archive.addfile(info, stream)
        else:
            from progress.stream import CountingReader
            reader = CountingReader(stream, self.reporter.progress, info.size, "Copying " + info.name)
            archive.addfile(info, reader)

    def _check_context_path(self, path):
        check_context_source(self.context, path)

    def _sources(self, pattern):
        paths = context_sources(self.context, pattern)
        if not paths:
            raise BuildError("Source not found: " + pattern)
        return paths

    def fingerprint_sources(self, transfer):
        """计算 COPY/ADD 源输入指纹，包括内容、链接、权限、大小和源 mtime。

        源 mtime 参与保守的缓存失效判断；输出层时间戳仍按 source_date_epoch 规范化。
        """
        digest = hashlib.sha256()
        links = {}

        def update(value):
            digest.update(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")

        def visit(path):
            if self._excluded(path, transfer):
                return
            data = path.lstat()
            name = path.relative_to(self.context).as_posix()
            mode = stat.S_IMODE(data.st_mode)
            if stat.S_ISDIR(data.st_mode):
                self._check_context_path(path)
                update([name, "dir", mode, data.st_mtime_ns])
                with os.scandir(path) as scan:
                    children = sorted(scan, key=lambda entry: entry.name)
                for child in children:
                    visit(Path(child.path))
            elif stat.S_ISLNK(data.st_mode):
                update([name, "symlink", mode, data.st_mtime_ns, os.readlink(path)])
            elif stat.S_ISREG(data.st_mode):
                identity = (data.st_dev, data.st_ino)
                linked_to = links.setdefault(identity, name) if data.st_nlink > 1 else None
                update([name, "file", mode, data.st_mtime_ns, data.st_size, linked_to])
                with open(path, "rb") as stream:
                    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        digest.update(block)
                digest.update(b"\n")
            else:
                raise UnsupportedInstruction("Unsupported context file type: " + str(path))

        for expression in transfer.sources:
            update(["source", expression])
            for path in self._sources(expression):
                visit(path)
        return digest.hexdigest()

    def _reserve(self, name, kind, added):
        name = _safe_name(name)
        if not name:
            raise BuildError("Invalid root output entry")
        if name in added:
            raise BuildError("Duplicate output path in one instruction: /" + name)
        existing = self.rootfs.kind(name)
        if kind == "dir":
            if existing is not None and existing != "dir":
                raise BuildError("Cannot copy directory over non-directory: /" + name)
        elif existing == "dir":
            raise BuildError("Cannot copy file over directory: /" + name)
        added[name] = kind
        return name

    def _ensure_directories(self, archive, destination, added, ownership=None, chmod=None):
        """缺失的目标目录继承传输选项，已有目录保留原有权限和属主。"""
        path = destination.strip("/")
        if not path:
            return
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            name = _safe_name("/".join(parts[:index]))
            if name in added:
                if added[name] != "dir":
                    raise BuildError("Non-directory output parent: /" + name)
                continue
            existing = self.rootfs.kind(name)
            if existing is not None and existing != "dir":
                raise UnsupportedInstruction("COPY/ADD through non-directory is unsupported: /" + name)
            if existing == "dir":
                continue
            info = tarfile.TarInfo(name + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755 if chmod is None else chmod
            info.uid, info.gid = ownership if ownership is not None else (0, 0)
            info.mtime = self.source_date_epoch
            archive.addfile(info)
            added[name] = "dir"

    def _info(self, name, mode, mtime, uid, gid, ownership, chmod):
        info = tarfile.TarInfo(name)
        info.uid, info.gid = ownership if ownership is not None else (uid, gid)
        info.mode = mode if chmod is None else chmod
        info.mtime = self.source_date_epoch
        return info

    def _source_node(self, archive, source, name, added, ownership, chmod, hardlinks):
        name = _safe_name(name.strip("/"))
        self._ensure_directories(archive, posixpath.dirname(name), added, ownership, chmod)
        data = source.lstat()
        mode = stat.S_IMODE(data.st_mode)
        info = self._info(name, mode, data.st_mtime, 0, 0, ownership, chmod)
        if stat.S_ISDIR(data.st_mode):
            self._check_context_path(source)
            self._reserve(name, "dir", added)
            info.name += "/"
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        elif stat.S_ISLNK(data.st_mode):
            self._reserve(name, "symlink", added)
            info.type = tarfile.SYMTYPE
            info.mode = mode  # chmod 不改变符号链接自身的权限，保持原链接元数据。
            info.linkname = os.readlink(source)
            archive.addfile(info)
        elif stat.S_ISREG(data.st_mode):
            self._reserve(name, "file", added)
            key = data.st_dev, data.st_ino
            if data.st_nlink > 1 and key in hardlinks:
                info.type = tarfile.LNKTYPE
                info.linkname = hardlinks[key]
                archive.addfile(info)
            else:
                hardlinks[key] = name
                info.size = data.st_size
                with open(source, "rb") as stream:
                    self._add_data(archive, info, stream)
        else:
            raise UnsupportedInstruction("Unsupported context file type: " + str(source))

    def _tree(self, archive, source, destination, added, ownership, chmod, hardlinks,
              transfer=None):
        self._ensure_directories(archive, destination, added, ownership, chmod)

        def walk(directory, prefix):
            self._check_context_path(Path(directory))
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
            for entry in entries:
                if transfer is not None and self._excluded(Path(entry.path), transfer):
                    continue
                target = posixpath.join(prefix, entry.name)
                self._source_node(archive, Path(entry.path), target, added, ownership, chmod, hardlinks)
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path, target)

        walk(source, destination)

    def _add_archive(self, archive, source, destination, added, ownership, chmod,
                     exclude=()):
        self._ensure_directories(archive, destination, added, ownership, chmod)
        with tarfile.open(source, "r:*") as original:
            members = []
            seen = set()
            for member in original:
                name = _safe_name(member.name)
                if not name:
                    continue
                if _matches_exclude(name, exclude):
                    continue
                if name in seen:
                    raise ArchiveError("Duplicate member in ADD archive: " + name)
                seen.add(name)
                if not (member.isdir() or member.isfile() or member.issym() or member.islnk() or
                        member.isfifo() or member.ischr() or member.isblk()):
                    raise UnsupportedInstruction("Unsupported ADD archive member: " + name)
                members.append((name, member))
            regular = {name for name, member in members if member.isfile()}
            symlinks = {name for name, member in members if member.issym()}
            for name, member in members:
                if any(posixpath.dirname(name) == link or
                       posixpath.dirname(name).startswith(link + "/") for link in symlinks):
                    raise UnsupportedInstruction("ADD archive member traverses a symlink: " + name)
                if member.islnk():
                    link = _safe_name(member.linkname)
                    if link not in regular:
                        raise UnsupportedInstruction("ADD hardlink target must be a regular member: " + member.linkname)
            members.sort(key=lambda pair: (0 if pair[1].isdir() else 2 if pair[1].islnk() else 1,
                                           pair[0].count("/"), pair[0]))
            for name, member in members:
                target = posixpath.join(destination, name).lstrip("/")
                self._ensure_directories(archive, posixpath.dirname(target), added, ownership, chmod)
                kind = "dir" if member.isdir() else "file"
                self._reserve(target, kind, added)
                info = self._info(target, member.mode, member.mtime, member.uid, member.gid,
                                  ownership, chmod)
                if member.isdir():
                    info.name += "/"
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif member.isfile():
                    info.size = member.size
                    stream = original.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable ADD archive member: " + name)
                    with stream:
                        self._add_data(archive, info, stream)
                elif member.issym():
                    info.type = tarfile.SYMTYPE
                    info.mode = member.mode
                    info.linkname = member.linkname
                    archive.addfile(info)
                elif member.islnk():
                    info.type = tarfile.LNKTYPE
                    info.linkname = posixpath.join(destination, _safe_name(member.linkname)).lstrip("/")
                    archive.addfile(info)
                elif member.isfifo():
                    info.type = tarfile.FIFOTYPE
                    archive.addfile(info)
                elif member.ischr() or member.isblk():
                    info.type = tarfile.CHRTYPE if member.ischr() else tarfile.BLKTYPE
                    info.devmajor, info.devminor = member.devmajor, member.devminor
                    archive.addfile(info)

    def workdir_layer(self, path, target, user=""):
        """按当前 USER 为 WORKDIR 新建目录；已经存在的目录保持原属主。"""
        if self.rootfs.kind(path) == "dir":
            return None
        if self.rootfs.kind(path) is not None:
            raise BuildError("WORKDIR is not a directory: " + path)
        ownership = resolve_chown(user, self.rootfs) if user else None
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            self._ensure_directories(archive, path, {}, ownership)
        return "sha256:" + sha256_file(target)

    def volume_layer(self, paths, target):
        """为 VOLUME 路径创建必要目录，而不清空已有内容。"""
        added = {}
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            for path in paths:
                self._ensure_directories(archive, path, added)
        return "sha256:" + sha256_file(target) if added else None

    def transfer_layer(self, transfer, workdir, target, add=False):
        """校验上下文路径和元数据后，把本地源文件打入新层。"""
        expanded = [(path, expression) for expression in transfer.sources
                    for path in self._sources(expression)]
        destination = container_path(transfer.destination, workdir)
        directory_target = transfer.destination.endswith("/") or self.rootfs.kind(destination) == "dir"
        if len(expanded) > 1 and not directory_target:
            raise BuildError("Multiple sources require a directory destination")
        ownership = resolve_chown(transfer.chown, self.rootfs)
        added = {}
        hardlinks = {}
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            for source, expression in expanded:
                if self._excluded(source, transfer):
                    continue
                data = source.lstat()
                if stat.S_ISDIR(data.st_mode):
                    directory_target_path = (posixpath.join(destination,
                                              self._parent_name(source, expression))
                                             if transfer.parents else destination)
                    self._tree(archive, source, directory_target_path, added, ownership,
                               transfer.chmod, hardlinks, transfer)
                    continue
                if add and transfer.unpack is not False and stat.S_ISREG(data.st_mode):
                    with open(source, "rb") as stream:
                        prefix = stream.read(4)
                    if prefix == b"\x28\xb5\x2f\xfd":
                        raise UnsupportedInstruction("ADD of zstd tar needs Python tarfile zstd support")
                    if tarfile.is_tarfile(source):
                        self._add_archive(archive, source, destination, added, ownership,
                                          transfer.chmod, transfer.exclude)
                        continue
                logical_name = (source.name if any(char in expression for char in "*?[")
                                else posixpath.basename(expression.rstrip("/")))
                if transfer.parents:
                    logical_name = self._parent_name(source, expression)
                output_name = posixpath.join(destination, logical_name) if (directory_target or transfer.parents) else destination
                self._source_node(archive, source, output_name, added,
                                  ownership or (0, 0), transfer.chmod, hardlinks)
        return "sha256:" + sha256_file(target)

    def inline_layer(self, transfer, workdir, target):
        """把 heredoc 内容打包为单个文件层。"""
        if transfer.inline is None:
            raise BuildError("Inline COPY needs heredoc data")
        destination = container_path(transfer.destination, workdir)
        if transfer.destination.endswith("/"):
            raise BuildError("Heredoc COPY destination must be a file")
        ownership = resolve_chown(transfer.chown, self.rootfs)
        content = transfer.inline.encode("utf-8")
        added = {}
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            name = destination.lstrip("/")
            self._ensure_directories(archive, posixpath.dirname(name), added, ownership, transfer.chmod)
            self._reserve(name, "file", added)
            info = self._info(name, 0o644, self.source_date_epoch, 0, 0,
                              ownership, transfer.chmod)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return "sha256:" + sha256_file(target)

    def remote_layer(self, transfer, source, filename, workdir, target):
        """按校验和及解包选项处理已下载的 ADD 源，生成新层。"""
        destination = container_path(transfer.destination, workdir)
        directory_target = transfer.destination.endswith("/") or self.rootfs.kind(destination) == "dir"
        ownership = resolve_chown(transfer.chown, self.rootfs)
        added = {}
        if transfer.unpack is True:
            with open(source, "rb") as stream:
                if stream.read(4) == b"\x28\xb5\x2f\xfd":
                    raise UnsupportedInstruction("ADD of zstd tar needs Python tarfile zstd support")
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            if transfer.unpack is True and tarfile.is_tarfile(source):
                self._add_archive(archive, source, destination, added, ownership,
                                  transfer.chmod, transfer.exclude)
            else:
                output_name = posixpath.join(destination, filename) if directory_target else destination
                name = _safe_name(output_name.strip("/"))
                self._ensure_directories(archive, posixpath.dirname(name), added, ownership, transfer.chmod)
                self._reserve(name, "file", added)
                info = self._info(name, 0o600, self.source_date_epoch, 0, 0, ownership,
                                  transfer.chmod)
                info.size = source.stat().st_size
                with open(source, "rb") as stream:
                    self._add_data(archive, info, stream)
        return "sha256:" + sha256_file(target)

    def transfer_from_rootfs(self, source_rootfs, transfer, workdir, target):
        """从指定阶段的最终可见文件树生成 COPY --from 层。"""
        expanded = []
        for expression in transfer.sources:
            if "\\" in expression or any(part == ".." for part in expression.split("/")):
                raise BuildError("Stage source escapes rootfs: " + expression)
            name = clean_path(expression.lstrip("/"))
            if any(char in name for char in "*?["):
                if "**" in name:
                    patterns = (name.replace("**/", "*"), name.replace("**/", ""))
                    matches = sorted(path for path in source_rootfs.entries
                                     if any(fnmatch.fnmatchcase(path, item) for item in patterns))
                else:
                    parts = name.split("/")
                    matches = sorted(path for path in source_rootfs.entries
                                     if len(path.split("/")) == len(parts) and
                                     all(fnmatch.fnmatchcase(piece, pattern)
                                         for piece, pattern in zip(path.split("/"), parts)))
            else:
                matches = [name] if not name or source_rootfs.kind(name) is not None else []
            if not matches:
                raise BuildError("Source not found in prior stage: " + expression)
            expanded.extend((match, expression) for match in matches)

        destination = container_path(transfer.destination, workdir)
        directory_target = transfer.destination.endswith("/") or self.rootfs.kind(destination) == "dir"
        if len(expanded) > 1 and not directory_target:
            raise BuildError("Multiple sources require a directory destination")
        ownership = resolve_chown(transfer.chown, self.rootfs)
        added = {}

        def member_info(path):
            entry = source_rootfs.entries.get(path)
            if entry is None:
                raise BuildError("Source disappeared from prior stage: /" + path)
            if not entry.layer:
                return None
            with tarfile.open(entry.layer, "r:") as original:
                return original.getmember(entry.member)

        def file_entry(path):
            seen = set()
            while True:
                if path in seen:
                    raise ArchiveError("Cyclic hardlink in prior stage: /" + path)
                seen.add(path)
                entry = source_rootfs.entries.get(path)
                if entry is None:
                    raise BuildError("Hardlink target missing from prior stage: /" + path)
                if entry.kind in ("file", "hardlink") and entry.layer and entry.member:
                    return entry
                if entry.kind != "hardlink":
                    raise UnsupportedInstruction("Cannot copy non-file hardlink target: /" + path)
                path = clean_path(entry.linkname.lstrip("/"))

        def write_node(archive, source, output):
            entry = source_rootfs.entries[source]
            output = _safe_name(output.strip("/"))
            self._ensure_directories(archive, posixpath.dirname(output), added, ownership, transfer.chmod)
            kind = "dir" if entry.kind == "dir" else "file"
            self._reserve(output, kind, added)
            original_info = member_info(source)
            if entry.kind == "hardlink":
                data_entry = file_entry(source)
                with tarfile.open(data_entry.layer, "r:") as original:
                    original_info = original.getmember(data_entry.member)
            mode = original_info.mode if original_info else 0o755
            mtime = original_info.mtime if original_info else 0
            uid = original_info.uid if original_info else 0
            gid = original_info.gid if original_info else 0
            info = self._info(output, mode, mtime, uid, gid, ownership, transfer.chmod)
            if entry.kind == "dir":
                info.name += "/"
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.mode = mode
                info.linkname = entry.linkname
                archive.addfile(info)
            elif entry.kind in ("file", "hardlink"):
                data_entry = file_entry(source)
                with tarfile.open(data_entry.layer, "r:") as original:
                    member = original.getmember(data_entry.member)
                    stream = original.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable prior-stage file: /" + source)
                    info.size = member.size
                    with stream:
                        self._add_data(archive, info, stream)
            else:
                raise UnsupportedInstruction("Cannot COPY prior-stage special file: /" + source)

        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            for source, expression in expanded:
                if _matches_exclude(source, transfer.exclude):
                    continue
                parent_source = source
                if "/./" in expression:
                    prefix_name = clean_path(expression.split("/./", 1)[0].lstrip("/"))
                    if parent_source.startswith(prefix_name + "/"):
                        parent_source = parent_source[len(prefix_name) + 1:]
                entry = source_rootfs.entries.get(source)
                is_directory = not source or entry.kind == "dir"
                if is_directory:
                    root_destination = (posixpath.join(destination, parent_source) if transfer.parents
                                        else destination)
                    self._ensure_directories(archive, root_destination, added, ownership, transfer.chmod)
                    prefix = source + "/" if source else ""
                    descendants = sorted((path for path in source_rootfs.entries
                                          if path.startswith(prefix) and path != source),
                                         key=lambda path: (path.count("/"), path))
                    for path in descendants:
                        if _matches_exclude(path, transfer.exclude):
                            continue
                        relative = path[len(prefix):]
                        write_node(archive, path, posixpath.join(root_destination, relative))
                else:
                    basename = posixpath.basename(source)
                    output = (posixpath.join(destination, parent_source) if transfer.parents else
                              posixpath.join(destination, basename) if directory_target else destination)
                    write_node(archive, source, output)
        return "sha256:" + sha256_file(target)
