"""挂载 Linux OverlayFS，并把 upperdir 中的变化转换为 OCI 层。"""

import ctypes
import errno
import os
import stat
import tarfile
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from image_reader import sha256_file
from path_policy import overlay_path


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


def _overlay_prefix(rootless):
    return "user.overlay." if rootless else "trusted.overlay."


def _overlay_value(path, suffix, rootless=False):
    """只读取当前挂载使用的内部命名空间，不能跨 namespace 猜测删除标记。"""
    return _xattr(path, _overlay_prefix(rootless) + suffix)


def _tarinfo(name, data, epoch=0):
    info = tarfile.TarInfo(name)
    info.uid, info.gid = data.st_uid, data.st_gid
    info.mode = stat.S_IMODE(data.st_mode)
    info.mtime = epoch
    return info


def _file_xattrs(path, rootless=False):
    """排除当前挂载的内部属性，另一 namespace 的普通属性仍应导出。"""
    if not hasattr(os, "listxattr"):
        # Windows 文件树测试没有该 API；真实 RUN 由 Linux 入口限定。
        return {}
    try:
        return {key: os.getxattr(path, key, follow_symlinks=False)
                for key in os.listxattr(path, follow_symlinks=False)
                if not key.startswith(_overlay_prefix(rootless))}
    except OSError as exc:
        raise BuildError("Cannot read RUN xattrs for {}: {}".format(path, exc)) from exc


def _metadata_signature(data, attrs):
    # mtime 统一采用 source_date_epoch，不作为额外生成根目录头的原因。
    return stat.S_IMODE(data.st_mode), data.st_uid, data.st_gid, attrs


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
        except (OSError, NotImplementedError) as exc:
            raise BuildError("Cannot preserve overlay root metadata: " + str(exc)) from exc
        if self.upper.stat().st_dev != self.work.stat().st_dev:
            raise BuildError("Overlay upperdir and workdir must share a filesystem")
        attrs = _file_xattrs(self.rootfs, self.rootless)
        try:
            for key, value in attrs.items():
                os.setxattr(self.upper, key, value, follow_symlinks=False)
        except (OSError, AttributeError) as exc:
            raise BuildError("Cannot preserve overlay root xattrs: " + str(exc)) from exc
        try:
            # 与层解包保持同一顺序：ACL 写入可能改变 mode，必须最后恢复权限位。
            self.upper.chmod(stat.S_IMODE(root_metadata.st_mode))
        except (OSError, NotImplementedError) as exc:
            raise BuildError("Cannot preserve overlay root mode: " + str(exc)) from exc
        self._initial_root = _metadata_signature(self.upper.stat(), attrs)

    def mount_in_child(self):
        """在子进程私有 mount namespace 中挂载 merged rootfs。"""
        for path in (self.rootfs, self.upper, self.work):
            overlay_path(path)
        options = "lowerdir={},upperdir={},workdir={},metacopy=off,redirect_dir=off,index=off".format(
            self.rootfs, self.upper, self.work)
        if self.rootless:
            options += ",userxattr"
        mount("overlay", self.merged, "overlay", data=options)

    def to_layer(self, target):
        """把内核 whiteout 和 opaque 目录转换为 OCI 层；拒绝无法保留的元数据。"""
        def check_directory_attrs(name, before, after):
            removed = sorted(set(before) - set(after))
            if removed:
                # Docker 解包会保留目录头未声明的 xattr；缺失字段不能表达属性删除。
                # 不能用空值冒充删除，也不能只加 opaque 而丢掉下层子文件。
                raise UnsupportedInstruction("RUN directory xattr removal cannot be preserved at /{}: {}".format(
                    name, ", ".join(removed)))

        def lower_directory(name):
            """只定位同名下层目录；符号链接的属性不能当作另一目录的旧属性。"""
            path = self.rootfs
            for part in name.split("/"):
                path = path / part
                if path.is_symlink() or not path.is_dir():
                    return None
            return path

        root_data = self.upper.stat()
        root_attrs = _file_xattrs(self.upper, self.rootless)
        check_directory_attrs("", self._initial_root[3], root_attrs)
        hardlinks = {}
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
            def info_with_attributes(name, data, attrs):
                info = _tarinfo(name, data, self.source_date_epoch)
                if self.rootless:
                    if data.st_uid != os.geteuid() or data.st_gid != os.getegid():
                        raise BuildError("Rootless RUN produced an unmapped file owner: " + (name or "."))
                    info.uid = info.gid = 0
                for key, value in attrs.items():
                    try:
                        info.pax_headers["SCHILY.xattr." + key] = value.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise UnsupportedInstruction("Non-UTF8 xattr cannot be serialized: " + name) from exc
                return info

            # 遍历子项不会看到 upper 根自身；其权限变化和 opaque 标记必须单独输出。
            if _overlay_value(self.upper, "metacopy", self.rootless) or _overlay_value(
                    self.upper, "redirect", self.rootless):
                raise UnsupportedInstruction("Overlay root metacopy/redirect cannot be serialized safely")
            if _metadata_signature(root_data, root_attrs) != self._initial_root:
                info = info_with_attributes(".", root_data, root_attrs)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            if _overlay_value(self.upper, "opaque", self.rootless) == b"y":
                _add_whiteout(archive, ".wh..wh..opq", self.source_date_epoch)

            def visit(directory, prefix=""):
                for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                    path = Path(entry.path)
                    name = prefix + entry.name
                    if name in self.excluded_paths:
                        continue
                    # 实体 .wh.* 在 OCI 中无法表示：空文件会被误作删除，不是普通文件。
                    # 真正的内核 whiteout 在下面按设备号/xattr 转成专用标记。
                    if entry.name.startswith(".wh."):
                        raise UnsupportedInstruction("Reserved OCI whiteout path produced by RUN: " + name)
                    data = path.lstat()
                    if _overlay_value(path, "metacopy", self.rootless) or _overlay_value(
                            path, "redirect", self.rootless):
                        raise UnsupportedInstruction("Overlay metacopy/redirect cannot be serialized safely: " + name)
                    is_whiteout = (stat.S_ISCHR(data.st_mode) and
                                   os.major(data.st_rdev) == 0 and os.minor(data.st_rdev) == 0)
                    marker = _overlay_value(path, "whiteout", self.rootless)
                    if marker is not None and not is_whiteout:
                        if not stat.S_ISREG(data.st_mode) or data.st_size != 0:
                            raise UnsupportedInstruction("Invalid OverlayFS xattr whiteout: " + name)
                        is_whiteout = True
                    # OverlayFS 用设备节点或扩展属性表示删除，
                    # OCI 层则用空的 .wh.<name> 文件表达同一语义。
                    if is_whiteout:
                        _add_whiteout(archive, prefix + ".wh." + entry.name,
                                      self.source_date_epoch)
                        continue
                    attrs = _file_xattrs(path, self.rootless)
                    info = info_with_attributes(name, data, attrs)
                    if stat.S_ISDIR(data.st_mode):
                        lower = lower_directory(name)
                        if lower is not None:
                            check_directory_attrs(name, _file_xattrs(lower, self.rootless), attrs)
                        info.name += "/"
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                        # opaque 目录遮蔽所有下层子项，
                        # 包括本次 upperdir 遍历中没有出现的名字。
                        if _overlay_value(path, "opaque", self.rootless) == b"y":
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
