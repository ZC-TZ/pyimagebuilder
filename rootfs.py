"""以虚拟索引应用 OCI 层和 whiteout，不把 rootfs 解包到宿主磁盘。"""

import posixpath
import tarfile
from dataclasses import dataclass

from errors import ArchiveError


@dataclass
class Entry:
    """记录可见路径的类型及其实际内容所在的层成员；硬链接固定到原始内容。"""
    kind: str
    linkname: str = ""
    layer: str = ""
    member: str = ""


def clean_path(name):
    """规范化层内相对路径，拒绝绝对路径及父目录跳转。"""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name == ".":
        return ""
    if name.startswith("/") or any(part == ".." for part in name.split("/")):
        raise ArchiveError("Layer member escapes rootfs: " + name)
    return posixpath.normpath(name).rstrip("/")


def validate_layer_paths(members):
    """在改变索引或磁盘前拒绝同层的非目录父路径，不受成员排列顺序影响。

    members 的路径已规范化且去重。whiteout 描述下层删除，不作为新文件树
    的成员参与检查；下层目录被本层文件替换仍是允许的跨层操作。
    """
    declared = {path: member for path, member in members
                if not posixpath.basename(path).startswith(".wh.")}
    for path in declared:
        parent = posixpath.dirname(path)
        while parent:
            member = declared.get(parent)
            if member is not None and not member.isdir():
                raise ArchiveError("Layer member descends through a non-directory parent: " + path)
            parent = posixpath.dirname(parent)


class RootFSIndex:
    """按层顺序维护最终可见文件树，应用 OCI whiteout 而不实际解包。"""
    def __init__(self):
        self.entries = {}

    def kind(self, path):
        """返回镜像路径当前可见的文件类型；路径不存在时返回 None。"""
        entry = self.entries.get(path.strip("/"))
        return entry.kind if entry else None

    def read_file(self, path, limit=2 * 1024 * 1024):
        """直接读取镜像中较小的配置文件，无需物化 Linux rootfs。"""
        pending = clean_path(path.lstrip("/")).split("/")
        resolved = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if resolved:
                    resolved.pop()
                continue
            name = "/".join(resolved + [part])
            entry = self.entries.get(name)
            if entry is not None and entry.kind == "symlink":
                hops += 1
                if hops > 40:
                    raise ArchiveError("Too many rootfs links: " + path)
                if entry.linkname.startswith("/"):
                    resolved = []
                pending = entry.linkname.split("/") + pending
            else:
                resolved.append(part)
        current = "/".join(resolved)
        for _ in range(40):
            entry = self.entries.get(current)
            if entry is None:
                return None
            if entry.kind == "hardlink" and not entry.layer:
                current = clean_path(entry.linkname.lstrip("/"))
                continue
            if entry.kind not in ("file", "hardlink"):
                raise ArchiveError("Rootfs path is not a file: " + path)
            with tarfile.open(entry.layer, "r:") as archive:
                member = archive.getmember(entry.member)
                if member.size > limit:
                    raise ArchiveError("Rootfs config file exceeds size limit: " + path)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError("Unreadable rootfs file: " + path)
                with stream:
                    return stream.read(limit + 1)
        raise ArchiveError("Too many rootfs hardlinks: " + path)

    def _remove(self, path, include=True):
        for existing in list(self.entries):
            if (include and existing == path) or existing.startswith(path + "/"):
                del self.entries[existing]

    def _parents(self, path):
        parent = posixpath.dirname(path)
        while parent:
            current = self.entries.get(parent)
            if current and current.kind != "dir":
                raise ArchiveError("Layer member descends through a non-directory parent: " + path)
            self.entries.setdefault(parent, Entry("dir"))
            parent = posixpath.dirname(parent)

    def apply_layer(self, layer_path):
        """先应用本层 whiteout，再登记新增条目，最后解析硬链接内容。

        whiteout 只遮蔽下层内容，不删除同层新增文件；硬链接固定实际成员，
        避免后续覆盖目标路径时错误改变旧链接的可读字节。
        """
        with tarfile.open(layer_path, "r:") as archive:
            members = []
            seen = set()
            for member in archive:
                path = clean_path(member.name)
                if path:
                    if path in seen:
                        raise ArchiveError("Duplicate path in layer: " + path)
                    seen.add(path)
                    members.append((path, member))
            # 子条目可能先于父条目出现；不能等父文件覆盖索引后才解析硬链接。
            validate_layer_paths(members)
            # OCI whiteout 只遮蔽下层条目，不能删除同层新增内容。
            for path, member in members:
                base = posixpath.basename(path)
                parent = posixpath.dirname(path)
                if base.startswith(".wh.") and (not member.isfile() or member.size != 0):
                    raise ArchiveError("OCI whiteout must be an empty regular file: " + path)
                if base == ".wh..wh..opq":
                    if parent:
                        self._remove(parent, include=False)
                    else:
                        self.entries.clear()
                elif base.startswith(".wh."):
                    target = posixpath.join(parent, base[4:])
                    if not base[4:]:
                        raise ArchiveError("Invalid empty whiteout")
                    self._remove(target)
            for path, member in members:
                if posixpath.basename(path).startswith(".wh."):
                    continue
                self._parents(path)
                if member.isdir():
                    old = self.entries.get(path)
                    if old and old.kind != "dir":
                        self._remove(path)
                    entry = Entry("dir", layer=str(layer_path), member=member.name)
                else:
                    self._remove(path)
                    if member.isfile():
                        entry = Entry("file", layer=str(layer_path), member=member.name)
                    elif member.issym():
                        entry = Entry("symlink", member.linkname, str(layer_path), member.name)
                    elif member.islnk():
                        clean_path(member.linkname.lstrip("/"))
                        # 整层索引完成前不固定硬链接的内容来源，
                        # 因为目标可能是尚未出现、最终指向下层内容的另一个链接。
                        entry = Entry("hardlink", member.linkname)
                    else:
                        # 导出/扁平化仍需原始 FIFO、设备节点的类型和设备号；不在宿主创建它们。
                        entry = Entry("other", layer=str(layer_path), member=member.name)
                self.entries[path] = entry
            # 后续层替换目标路径不会改变旧硬链接所引用的字节，
            # 因此此时固定实际的层成员，后续读取不再追踪目标路径。
            for path, member in members:
                if not member.islnk():
                    continue
                entry = self.entries[path]
                target = clean_path(entry.linkname.lstrip("/"))
                seen_links = {path}
                while True:
                    if target in seen_links:
                        raise ArchiveError("Cyclic hardlink in layer: " + path)
                    seen_links.add(target)
                    backing = self.entries.get(target)
                    if backing is None:
                        raise ArchiveError("Hardlink target missing in layer: " + path)
                    if backing.kind == "file" or (backing.kind == "hardlink" and backing.layer):
                        entry.layer, entry.member = backing.layer, backing.member
                        break
                    if backing.kind != "hardlink":
                        raise ArchiveError("Hardlink target is not a file: " + path)
                    target = clean_path(backing.linkname.lstrip("/"))
