"""以虚拟索引应用 OCI 层和 whiteout，不把 rootfs 解包到宿主磁盘。"""

import copy
import posixpath
import tarfile
from dataclasses import dataclass

from errors import ArchiveError


@dataclass
class Inode:
    """共享文件元数据；内容成员固定后，链接头仍可改变整个 inode 的权限。"""
    metadata: object

    def apply_link(self, member):
        """应用链接头的 inode 属性，保留未覆盖的普通扩展属性。"""
        updated = copy.copy(member)
        # 链接不重新创建文件，未覆盖的普通 xattr 仍在 inode 上。
        # 物化时 lchown 会清除旧 capability，因此不能把该属性从旧头恢复回来。
        attrs = {key: value for key, value in self.metadata.pax_headers.items()
                 if key.startswith("SCHILY.xattr.") and key != "SCHILY.xattr.security.capability"}
        updated.pax_headers = dict(member.pax_headers)
        attrs.update(updated.pax_headers)
        updated.pax_headers = attrs
        self.metadata = updated


@dataclass
class Entry:
    """记录可见路径及固定内容位置；硬链接共享 inode 的最终元数据。"""
    kind: str
    linkname: str = ""
    layer: str = ""
    member: str = ""
    inode: object = None

    @property
    def metadata(self):
        """返回文件 inode 的最终元数据，不能用内容成员头替代。"""
        return self.inode.metadata if self.inode is not None else None


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
        避免后续覆盖目标路径时错误改变旧链接的字节；链接头更新共享 inode 属性。
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
                        entry = Entry("file", layer=str(layer_path), member=member.name,
                                      inode=Inode(member))
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
            links = {path: member for path, member in members if member.islnk()}
            targets = {path: clean_path(member.linkname.lstrip("/"))
                       for path, member in links.items()}
            ready, waiting = [], {}
            for path, target in targets.items():
                if target in links:
                    waiting.setdefault(target, []).append(path)
                else:
                    ready.append(path)
            # 与物化器使用相同的依赖顺序：固定内容，同时共享可变 inode 元数据。
            # 再次应用同一层的普通文件仍创建新 inode，不能按内容成员把旧别名合并。
            completed = 0
            for path in ready:
                backing = self.entries.get(targets[path])
                if backing is None:
                    raise ArchiveError("Hardlink target missing in layer: " + path)
                if backing.kind not in ("file", "hardlink") or backing.inode is None:
                    raise ArchiveError("Hardlink target is not a file: " + path)
                entry = self.entries[path]
                entry.layer, entry.member, entry.inode = backing.layer, backing.member, backing.inode
                entry.inode.apply_link(links[path])
                completed += 1
                ready.extend(waiting.get(path, ()))
            if completed != len(links):
                pending = set(links) - set(ready)
                raise ArchiveError("Cyclic hardlink in layer: " + next(iter(pending)))
