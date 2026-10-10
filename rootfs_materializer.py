"""把镜像层安全应用到私有 Linux rootfs，供真实 RUN 使用。"""

import os
import posixpath
import shutil
import stat
import tarfile
from pathlib import Path

from errors import ArchiveError, BuildError
from rootfs import clean_path, validate_layer_paths


class RootFSMaterializer:
    """在私有目录物化已验证镜像层，为 RUN 提供实际文件系统。"""
    def __init__(self, root, rootless=False):
        self.root = Path(root).resolve()
        self.rootless = rootless
        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        if created:
            if hasattr(os, "chown"):
                self._default_directory_metadata(self.root)
            else:
                # Windows 文件树测试不能设置 POSIX 属主；真实 RUN 由 Linux 入口限定。
                self.root.chmod(0o755)

    def _default_directory_metadata(self, path):
        """为未显式声明的镜像目录设置默认值，避免继承宿主 umask 或组。"""
        member = tarfile.TarInfo(path.name)
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        member.uid = member.gid = member.mtime = 0
        self._metadata(path, member)

    def _real(self, image_path, follow_final=False):
        """在镜像 root 内解析符号链接，包括镜像中的绝对路径链接。"""
        name = clean_path(image_path)
        parts = [] if not name else name.split("/")
        resolved = []
        hops = 0
        while parts:
            part = parts.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise ArchiveError("Path escapes image rootfs: " + image_path)
                resolved.pop()
                continue
            candidate = self.root.joinpath(*resolved, part)
            if candidate.is_symlink() and (parts or follow_final):
                hops += 1
                if hops > 40:
                    raise ArchiveError("Too many rootfs symlinks: " + image_path)
                target = os.readlink(candidate).replace("\\", "/")
                if target.startswith("/"):
                    resolved = []
                parts = target.split("/") + parts
            else:
                resolved.append(part)
        return self.root.joinpath(*resolved)

    def _parents(self, image_path):
        parent = posixpath.dirname(image_path)
        if not parent:
            return
        parts = parent.split("/")
        for index in range(1, len(parts) + 1):
            current = self._real("/".join(parts[:index]), follow_final=True)
            if current.exists() and not current.is_dir():
                raise ArchiveError("Non-directory rootfs parent: " + str(current))
            created = not current.exists()
            current.mkdir(exist_ok=True)
            if created:
                self._default_directory_metadata(current)

    def _remove(self, image_path):
        path = self._real(image_path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _metadata(self, path, member, symlink=False):
        try:
            if self.rootless:
                if member.uid != 0 or member.gid != 0:
                    raise BuildError("Rootless RUN supports only uid/gid 0 in image layers: " + str(path))
                if hasattr(os, "chown"):
                    # setgid 父目录可使新节点继承另一个宿主组；单 GID 映射只允许主组。
                    # 所有者仍是当前用户，非 root 也可将自己文件的组改为自己的主组。
                    os.chown(path, os.geteuid(), os.getegid(), follow_symlinks=False)
            else:
                os.chown(path, member.uid, member.gid, follow_symlinks=False)
            if not symlink:
                os.chmod(path, member.mode, follow_symlinks=False)
            os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)
        except (OSError, NotImplementedError) as exc:
            raise BuildError("Cannot preserve layer metadata for {}: {}".format(path, exc)) from exc
        for key, value in member.pax_headers.items():
            if key.startswith("SCHILY.xattr."):
                try:
                    os.setxattr(path, key[len("SCHILY.xattr."):], value.encode("utf-8"),
                                follow_symlinks=False)
                except OSError as exc:
                    raise BuildError("Cannot preserve xattr for {}: {}".format(path, exc)) from exc

    def apply(self, layer_path):
        """应用镜像层并保留元数据，始终检查 rootfs 路径边界。"""
        with tarfile.open(layer_path, "r:") as archive:
            members = [(clean_path(member.name), member) for member in archive]
            members = [(name, member) for name, member in members if name]
            seen = set()
            for name, _ in members:
                if name in seen:
                    raise ArchiveError("Duplicate path in layer: " + name)
                seen.add(name)
            validate_layer_paths(members)
            for name, _ in members:
                base = posixpath.basename(name)
                parent = posixpath.dirname(name)
                if base == ".wh..wh..opq":
                    directory = self._real(parent, follow_final=True) if parent else self.root
                    if directory.is_dir():
                        for child in list(directory.iterdir()):
                            self._remove(posixpath.join(parent, child.name))
                elif base.startswith(".wh."):
                    self._remove(posixpath.join(parent, base[4:]))
            directories = []
            hardlinks = []
            for name, member in members:
                if posixpath.basename(name).startswith(".wh."):
                    continue
                self._parents(name)
                path = self._real(name)
                if member.isdir():
                    if path.is_symlink() or (path.exists() and not path.is_dir()):
                        self._remove(name)
                    path.mkdir(exist_ok=True)
                    directories.append((path, member))
                elif member.islnk():
                    hardlinks.append((name, member))
                else:
                    self._remove(name)
                    if member.isfile():
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ArchiveError("Unreadable file in layer: " + name)
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        if hasattr(os, "O_NOFOLLOW"):
                            flags |= os.O_NOFOLLOW
                        descriptor = os.open(path, flags, 0o600)
                        with os.fdopen(descriptor, "wb") as output, stream:
                            shutil.copyfileobj(stream, output, 4 * 1024 * 1024)
                        self._metadata(path, member)
                    elif member.issym():
                        os.symlink(member.linkname, path)
                        self._metadata(path, member, symlink=True)
                    elif member.isfifo():
                        os.mkfifo(path, member.mode)
                        self._metadata(path, member)
                    elif member.ischr() or member.isblk():
                        if self.rootless:
                            raise BuildError("Rootless RUN cannot materialize image device nodes: " + name)
                        kind = stat.S_IFCHR if member.ischr() else stat.S_IFBLK
                        os.mknod(path, kind | member.mode, os.makedev(member.devmajor, member.devminor))
                        self._metadata(path, member)
                    else:
                        raise ArchiveError("Unsupported layer member type: " + name)
            links = dict(hardlinks)
            targets = {name: clean_path(member.linkname.lstrip("/"))
                       for name, member in hardlinks}
            waiting = {}
            ready = []
            for name, target in targets.items():
                if target in links:
                    waiting.setdefault(target, []).append(name)
                else:
                    ready.append(name)
            # 先物化被依赖的链接，再处理指向它的链接；即使下层有同名文件也必须等待。
            # ready 迭代时追加新就绪项，避免长链反复扫描造成平方级开销。
            completed = 0
            for name in ready:
                member = links[name]
                source = self._real(targets[name], follow_final=True)
                if not source.is_file():
                    raise ArchiveError("Hardlink target missing: " + member.linkname)
                self._remove(name)
                target = self._real(name)
                os.link(source, target)
                self._metadata(target, member)
                completed += 1
                ready.extend(waiting.get(name, ()))
            if completed != len(links):
                processed = set(ready)
                raise ArchiveError("Cyclic hardlink in layer: " + next(name for name in links if name not in processed))
            for path, member in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
                self._metadata(path, member)
