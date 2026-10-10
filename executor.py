"""在 Linux namespace 和镜像 rootfs 中实际执行 RUN。"""

import ctypes
import os
import platform
import stat
import traceback
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from image_identity import checked_identity_id
from overlay import mount
from sandbox import install_filter


CLONE_NEWNS = 0x00020000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
CLONE_NEWUSER = 0x10000000
MS_REC = 0x4000
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MS_REMOUNT = 32
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
PR_SET_NO_NEW_PRIVS = 38
PR_CAPBSET_DROP = 24
LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def _map_rootless_identity(host_uid, host_gid):
    """仅把当前宿主用户及组映射为 namespace 内的 UID/GID 0。"""
    Path("/proc/self/setgroups").write_text("deny\n", encoding="ascii")
    Path("/proc/self/uid_map").write_text("0 {} 1\n".format(host_uid), encoding="ascii")
    Path("/proc/self/gid_map").write_text("0 {} 1\n".format(host_gid), encoding="ascii")


def _no_new_privileges():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise BuildError("PR_SET_NO_NEW_PRIVS failed: " + os.strerror(ctypes.get_errno()))


def _drop_bounding_capabilities():
    libc = ctypes.CDLL(None, use_errno=True)
    for number in range(64):
        if libc.prctl(PR_CAPBSET_DROP, number, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            if error != 22:  # 内核不认识此 capability 编号，后续更大的编号也不适用。
                raise BuildError("Capability bounding-set drop failed: " + os.strerror(error))


def _clear_capabilities():
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    libc.capset.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]
    libc.capset.restype = ctypes.c_int
    if libc.capset(ctypes.byref(header), data) != 0:
        raise BuildError("capset failed: " + os.strerror(ctypes.get_errno()))


def _unshare(flags):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(flags) != 0:
        raise BuildError("unshare failed: " + os.strerror(ctypes.get_errno()))


def _device(path, mode, major, minor):
    os.mknod(path, stat.S_IFCHR | mode, os.makedev(major, minor))


def _prepare_devices(merged, rootless=False):
    devices = merged / "dev"
    if devices.is_symlink():
        raise BuildError("Image /dev must not be a symlink during RUN setup")
    devices.mkdir(exist_ok=True)
    if not devices.is_dir():
        raise BuildError("Image /dev must be a directory")
    mount("tmpfs", devices, "tmpfs", MS_NOSUID, "mode=755")
    for name, major, minor, mode in (
        ("null", 1, 3, 0o666), ("zero", 1, 5, 0o666),
        ("random", 1, 8, 0o666), ("urandom", 1, 9, 0o666),
        ("tty", 5, 0, 0o666),
    ):
        if rootless:
            target = devices / name
            target.touch()
            mount("/dev/" + name, target, None, MS_BIND)
        else:
            _device(devices / name, mode, major, minor)
    pts = devices / "pts"
    pts.mkdir()
    mount("devpts", pts, "devpts", MS_NOSUID | MS_NOEXEC, "newinstance,ptmxmode=0666,mode=0620")
    os.symlink("pts/ptmx", devices / "ptmx")
    os.symlink("/proc/self/fd", devices / "fd")
    for name, number in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        os.symlink("/proc/self/fd/{}".format(number), devices / name)


def _identity_records(path, minimum_fields, id_fields):
    """读取镜像内账户记录，保留原始顺序并拒绝无效的数值身份。"""
    records = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split(":")
                if len(fields) < minimum_fields:
                    continue
                for index in id_fields:
                    fields[index] = checked_identity_id(fields[index], path + " ID")
                records.append(fields)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        raise BuildError("Cannot read image identity file {}: {}".format(path, exc)) from exc
    return records


def _resolve_user(value):
    """按镜像账户解析 RUN 身份；数字 UID 和具名用户均使用首个匹配记录。"""
    user, separator, group = (value or "0").partition(":")
    if not user or (separator and not group):
        raise BuildError("Invalid image USER: " + value)
    passwd = _identity_records("/etc/passwd", 4, (2, 3))
    if user.isdecimal():
        uid = checked_identity_id(user, "UID")
        matched = next((fields for fields in passwd if fields[2] == uid), None)
        gid = matched[3] if matched is not None else 0
    else:
        matched = next((fields for fields in passwd if fields[0] == user), None)
        if matched is None:
            raise BuildError("USER not found in image /etc/passwd: " + user)
        uid, gid = matched[2:4]
    # 附加组属于匹配到的账户名，不能经 UID 反查另一个同 UID 的账户。
    effective_name = matched[0] if matched is not None else None
    supplementary = []
    if separator:
        if group.isdecimal():
            gid = checked_identity_id(group, "GID")
        else:
            matched_group = next((fields for fields in _identity_records("/etc/group", 3, (2,))
                                  if fields[0] == group), None)
            if matched_group is None:
                raise BuildError("Group not found in image /etc/group: " + group)
            gid = matched_group[2]
    elif effective_name:
        for fields in _identity_records("/etc/group", 4, (2,)):
            if effective_name in fields[3].split(",") and fields[2] != gid:
                supplementary.append(fields[2])
    return uid, gid, sorted(set(supplementary))


def _wait_status(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


class RunExecutor:
    """在准备好的 rootfs 中执行 RUN，按配置启用 namespace、挂载与沙箱限制。"""
    def __init__(self, network="none", sandbox="legacy"):
        if platform.system() != "Linux":
            raise UnsupportedInstruction("RUN requires Linux or a WSL Linux environment")
        if sandbox not in ("legacy", "hardened", "rootless"):
            raise BuildError("RUN sandbox must be legacy, hardened, or rootless")
        if sandbox != "rootless" and os.geteuid() != 0:
            raise UnsupportedInstruction("RUN currently requires Linux root with mount namespace privileges")
        if sandbox == "rootless" and os.geteuid() == 0:
            raise BuildError("Rootless RUN must start as a non-root host user")
        if network not in ("none", "host"):
            raise BuildError("RUN network must be none or host")
        self.network = network
        self.sandbox = sandbox

    def execute(self, overlay, command, environment, workdir, user, shell, mounts=()):
        """使用阶段的环境、工作目录和用户执行一条 RUN，并等待子进程完成。

        成功返回 None，文件变化保留在 overlay 中；调用方随后用 overlay.to_layer 生成层。
        非零退出或环境设置失败会抛出 BuildError，不返回 layer 文件。
        """
        if command.shell_form:
            argv = list(shell) + [command.value]
        else:
            argv = list(command.value)
        if not argv:
            raise BuildError("Empty RUN command")
        read_fd, write_fd = os.pipe()
        log_callback = getattr(self, "log", None)
        log_read, log_write = os.pipe() if log_callback is not None else (None, None)
        err_read, err_write = os.pipe() if log_callback is not None else (None, None)
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            if log_callback is not None:
                os.close(log_read)
                os.close(err_read)
                os.dup2(log_write, 1)
                os.dup2(err_write, 2)
                os.close(log_write)
                os.close(err_write)
            try:
                if self.sandbox == "rootless":
                    host_uid, host_gid = os.geteuid(), os.getegid()
                    _unshare(CLONE_NEWUSER)
                    _map_rootless_identity(host_uid, host_gid)
                flags = CLONE_NEWNS | CLONE_NEWPID
                if self.network == "none":
                    flags |= CLONE_NEWNET
                _unshare(flags)
                mount(None, "/", None, MS_REC | MS_PRIVATE)
                overlay.mount_in_child()
                merged = overlay.merged
                for kind, source, target in mounts:
                    relative = target.lstrip("/")
                    destination = merged
                    parts = relative.split("/")
                    for part in parts[:-1]:
                        destination = destination / part
                        if destination.is_symlink():
                            raise BuildError("RUN mount traverses an image symlink")
                        destination.mkdir(exist_ok=True)
                    destination = destination / parts[-1]
                    if destination.is_symlink():
                        raise BuildError("RUN mount target is an image symlink")
                    if kind == "cache":
                        destination.mkdir(exist_ok=True)
                    else:
                        if destination.exists() and not destination.is_file():
                            raise BuildError("Secret mount target is not a file")
                        destination.touch(exist_ok=True)
                    mount(str(source), destination, None, MS_BIND)
                    if kind == "secret":
                        mount(None, destination, None, MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
                proc = merged / "proc"
                if proc.is_symlink():
                    raise BuildError("Image /proc must not be a symlink during RUN setup")
                proc.mkdir(exist_ok=True)
                temporary = merged / "tmp"
                if temporary.is_symlink():
                    raise BuildError("Image /tmp must not be a symlink during RUN setup")
                if not temporary.exists():
                    temporary.mkdir(mode=0o1777)
                    # mkdir 的 mode 会被宿主 umask 削减，必须恢复 world-write 和 sticky 位。
                    # 有特定权限的已有 /tmp 保持镜像配置，不擅自放宽权限。
                    if self.sandbox != "rootless":
                        os.chown(temporary, 0, 0)
                    temporary.chmod(0o1777)
                elif not temporary.is_dir():
                    raise BuildError("Image /tmp must be a directory during RUN setup")
                _prepare_devices(merged, self.sandbox == "rootless")
                runner = os.fork()
                if runner == 0:
                    try:
                        mount("proc", merged / "proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
                        os.chroot(merged)
                        os.chdir(workdir)
                        if self.sandbox != "legacy":
                            _no_new_privileges()
                            _drop_bounding_capabilities()
                        # 未写 USER 也需要按镜像 UID 0 解析并重置组，不能继承宿主组权限。
                        uid, gid, groups = _resolve_user(user)
                        if self.sandbox == "rootless" and (uid != 0 or gid != 0 or groups):
                            raise BuildError("Rootless RUN supports only image USER 0:0")
                        if self.sandbox != "rootless":
                            os.setgroups(groups)
                            os.setgid(gid)
                            os.setuid(uid)
                        if self.sandbox != "legacy":
                            _clear_capabilities()
                            install_filter()
                        env = dict(environment)
                        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
                        os.execvpe(argv[0], argv, env)
                    except BaseException:
                        os.write(write_fd, traceback.format_exc().encode("utf-8", "replace"))
                        os._exit(127)
                _, status = os.waitpid(runner, 0)
                os._exit(_wait_status(status))
            except BaseException:
                os.write(write_fd, traceback.format_exc().encode("utf-8", "replace"))
                os._exit(127)
        os.close(write_fd)
        if log_callback is not None:
            os.close(log_write)
            os.close(err_write)
            import threading
            def forward_logs(fd, source):
                with os.fdopen(fd, "rb") as output_stream:
                    for line in output_stream:
                        log_callback(line.decode("utf-8", "replace").rstrip("\r\n"), source)
            log_threads = [threading.Thread(target=forward_logs, args=(fd, source), daemon=True)
                           for fd, source in ((log_read, "stdout"), (err_read, "stderr"))]
            for thread in log_threads:
                thread.start()
        with os.fdopen(read_fd, "rb") as stream:
            diagnostics = stream.read().decode("utf-8", "replace")
        _, status = os.waitpid(pid, 0)
        if log_callback is not None:
            for thread in log_threads:
                thread.join()
        code = _wait_status(status)
        if code:
            detail = diagnostics.strip() or "process exited with code {}".format(code)
            raise BuildError("RUN failed: " + detail)
