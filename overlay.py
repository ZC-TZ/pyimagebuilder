"""挂载 Linux OverlayFS，并把 upperdir 中的变化转换为 OCI 层。"""

import ctypes
import errno
import os
import stat
import tarfile
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from image_reader import sha256_file


_LIBC = ctypes.CDLL(None, use_errno=True) if os.name == "posix" else None
if _LIBC is not None:
    _LIBC.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                            ctypes.c_ulong, ctypes.c_char_p]
    _LIBC.mount.restype = ctypes.c_int


def mount(source, target, filesystem, flags=0, data=None):
    """调用 Linux mount 系统调用，把 errno 失败转换为构建错误。"""
    if _LIBC is None:
        raise UnsupportedInstruction("Linux mount syscall is required for RUN")
    values = [item.encode() if isinstance(item, str) else item
              for item in (source, str(target), filesystem, data)]
    result = _LIBC.mount(values[0], values[1], values[2], flags, values[3])
    if result != 0:
        error = ctypes.get_errno()
        raise BuildError("mount {} on {} failed: {}".format(filesystem, target, os.strerror(error)))


def _xattr(path, name):
    try:
        return os.getxattr(path, name, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP) or (
                name.startswith("trusted.overlay.") and
                exc.errno in (errno.EPERM, errno.EACCES)):
            return None
        raise


def _overlay_value(path, suffix):
    first = _xattr(path, "trusted.overlay." + suffix)
    if first is not None:
        return first
    return _xattr(path, "user.overlay." + suffix)


def _tarinfo(name, data, epoch=0):
    info = tarfile.TarInfo(name)
    info.uid, info.gid = data.st_uid, data.st_gid
    info.mode = stat.S_IMODE(data.st_mode)
    info.mtime = epoch
    return info


def _add_whiteout(archive, name, epoch=0):
    info = tarfile.TarInfo(name)
    info.mode = 0o000
    info.size = 0
    info.uid = info.gid = 0
    info.mtime = epoch
    archive.addfile(info)


class OverlayManager:
    """通过 OverlayFS 捕获 RUN 文件变化，将内核 whiteout 转换为 OCI 表示。"""
    def __init__(self, rootfs, workspace, rootless=False):
        self.rootfs = Path(rootfs).resolve()
        self.workspace = Path(workspace).resolve()
        self.rootless = rootless
        self.upper = self.workspace / "upper"
        self.work = self.workspace / "work"
        self.merged = self.workspace / "merged"
        self.source_date_epoch = 0
        self.excluded_paths = set()
        for path in (self.upper, self.work, self.merged):
            path.mkdir(parents=True, exist_ok=False)
        # 合并目录的元数据来自 upper；仅修正 lower 无法排除 upper 的宿主 umask。
        # 复制 rootfs 根目录的属主、权限，使非 root RUN 看到原来的根目录访问规则。
        root_metadata = self.rootfs.stat()
        try:
            if hasattr(os, "chown"):
                os.chown(self.upper, root_metadata.st_uid, root_metadata.st_gid,
                         follow_symlinks=False)
            self.upper.chmod(stat.S_IMODE(root_metadata.st_mode))
        except (OSError, NotImplementedError) as exc:
            raise BuildError("Cannot preserve overlay root metadata: " + str(exc)) from exc
        if self.upper.stat().st_dev != self.work.stat().st_dev:
            raise BuildError("Overlay upperdir and workdir must share a filesystem")

    def mount_in_child(self):
        """在子进程私有 mount namespace 中挂载 merged rootfs。"""
        options = "lowerdir={},upperdir={},workdir={},metacopy=off,redirect_dir=off,index=off".format(
            self.rootfs, self.upper, self.work)
        if self.rootless:
            options += ",userxattr"
        mount("overlay", self.merged, "overlay", data=options)

    def to_layer(self, target):
        """把内核 whiteout 和 opaque 目录转换为 OCI 层；拒绝无法保留的元数据。"""
        hardlinks = {}
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            def visit(directory, prefix=""):
                for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                    path = Path(entry.path)
                    name = prefix + entry.name
                    if name in self.excluded_paths:
                        continue
                    data = path.lstat()
                    if _overlay_value(path, "metacopy") or _overlay_value(path, "redirect"):
                        raise UnsupportedInstruction("Overlay metacopy/redirect cannot be serialized safely: " + name)
                    is_whiteout = (stat.S_ISCHR(data.st_mode) and
                                   os.major(data.st_rdev) == 0 and os.minor(data.st_rdev) == 0)
                    is_whiteout = is_whiteout or _overlay_value(path, "whiteout") is not None
                    # OverlayFS 用设备节点或扩展属性表示删除，
                    # OCI 层则用空的 .wh.<name> 文件表达同一语义。
                    if is_whiteout:
                        _add_whiteout(archive, prefix + ".wh." + entry.name,
                                      self.source_date_epoch)
                        continue
                    info = _tarinfo(name, data, self.source_date_epoch)
                    if self.rootless:
                        if data.st_uid != os.geteuid() or data.st_gid != os.getegid():
                            raise BuildError("Rootless RUN produced an unmapped file owner: " + name)
                        info.uid = info.gid = 0
                    for key in os.listxattr(path, follow_symlinks=False):
                        if key.startswith(("trusted.overlay.", "user.overlay.")):
                            continue
                        value = os.getxattr(path, key, follow_symlinks=False)
                        try:
                            info.pax_headers["SCHILY.xattr." + key] = value.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise UnsupportedInstruction("Non-UTF8 xattr cannot be serialized: " + name) from exc
                    if stat.S_ISDIR(data.st_mode):
                        info.name += "/"
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                        # opaque 目录遮蔽所有下层子项，
                        # 包括本次 upperdir 遍历中没有出现的名字。
                        if _overlay_value(path, "opaque") == b"y":
                            _add_whiteout(archive, name + "/.wh..wh..opq",
                                          self.source_date_epoch)
                        visit(path, name + "/")
                    elif stat.S_ISREG(data.st_mode):
                        identity = (data.st_dev, data.st_ino)
                        if data.st_nlink > 1 and identity in hardlinks:
                            info.type = tarfile.LNKTYPE
                            info.linkname = hardlinks[identity]
                            info.size = 0
                            archive.addfile(info)
                        else:
                            hardlinks[identity] = name
                            info.size = data.st_size
                            with open(path, "rb") as stream:
                                archive.addfile(info, stream)
                    elif stat.S_ISLNK(data.st_mode):
                        info.type = tarfile.SYMTYPE
                        info.linkname = os.readlink(path)
                        archive.addfile(info)
                    elif stat.S_ISFIFO(data.st_mode):
                        info.type = tarfile.FIFOTYPE
                        archive.addfile(info)
                    elif stat.S_ISCHR(data.st_mode) or stat.S_ISBLK(data.st_mode):
                        info.type = tarfile.CHRTYPE if stat.S_ISCHR(data.st_mode) else tarfile.BLKTYPE
                        info.devmajor = os.major(data.st_rdev)
                        info.devminor = os.minor(data.st_rdev)
                        archive.addfile(info)
                    else:
                        raise UnsupportedInstruction("Unsupported RUN filesystem object: " + name)
            visit(self.upper)
        return "sha256:" + sha256_file(target)
