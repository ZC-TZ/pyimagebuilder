"""在 Linux namespace 和镜像 rootfs 中实际执行 RUN。"""

import codecs
import ctypes
import os
import platform
import signal
import stat
import threading
import traceback
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from file_resources import fdopen_owned
from image_identity import checked_identity_id
from overlay import mount
from sandbox import install_filter
from run_mounts import normalize_mount_target


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
RUN_LOG_FRAGMENT_BYTES = 64 * 1024
RUN_DIAGNOSTIC_BYTES = 256 * 1024


def _iter_run_log_fragments(stream):
    """按有限字节读取日志；长行可提前转发，UTF-8 跨段字符保持完整。"""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending_cr = ""
    while True:
        # 给上一段尚未完成的字符和 CR 留位置，单段缓冲不随整行长度增长。
        size = RUN_LOG_FRAGMENT_BYTES - len(decoder.getstate()[0]) - len(pending_cr)
        raw = stream.readline(size)
        if not raw:
            tail = pending_cr + decoder.decode(b"", final=True)
            if tail:
                yield tail.rstrip("\r\n")
            return
        complete = raw.endswith(b"\n") or len(raw) < size
        text = pending_cr + decoder.decode(raw, final=complete)
        pending_cr = ""
        if complete:
            decoder.reset()
            yield text.rstrip("\r\n")
        else:
            # CRLF 恰好跨读取边界时，不把换行的 CR 当作可见内容转发。
            if text.endswith("\r"):
                pending_cr, text = "\r", text[:-1]
            if text:
                yield text


def _read_run_diagnostics(stream):
    """排空诊断管道，仅保留有限的开头和结尾，兼顾调用栈与最终失败原因。"""
    head = None
    retained = bytearray()
    half = RUN_DIAGNOSTIC_BYTES // 2
    while True:
        chunk = stream.read(RUN_LOG_FRAGMENT_BYTES)
        if not chunk:
            break
        retained.extend(chunk)
        if head is None and len(retained) > RUN_DIAGNOSTIC_BYTES:
            head = bytes(retained[:half])
        if head is not None:
            del retained[:-half]
    if head is None:
        raw = bytes(retained)
    else:
        raw = head + b"\n...[RUN diagnostics truncated]...\n" + bytes(retained)
    return raw.decode("utf-8", "replace")


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def _fork_with_pipes(with_logs):
    """创建 RUN 管道及首个子进程；准备失败时回收已分配的全部描述符。

    成功后由 execute 的父/子分支分别接管管道。仅关闭本次已创建的描述符，
    一个 close 报错也继续尝试其余描述符，并保留原始创建/中断异常。
    """
    descriptors = []
    try:
        if threading.active_count() != 1:
            raise UnsupportedInstruction("RUN fork requires one active Python thread; pause background workers first")
        diagnostics = os.pipe()
        descriptors.extend(diagnostics)
        stdout = os.pipe() if with_logs else (None, None)
        if with_logs:
            descriptors.extend(stdout)
        stderr = os.pipe() if with_logs else (None, None)
        if with_logs:
            descriptors.extend(stderr)
        pid = os.fork()
    except BaseException:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    return pid, diagnostics, stdout, stderr


def _wait_child(pid, options=0):
    """回收指定子进程；只重试系统 EINTR，不吞掉用户取消。"""
    while True:
        try:
            return os.waitpid(pid, options)
        except InterruptedError:
            continue


def _prepare_run_supervisor():
    """隔离终端信号，并让父进程的取消请求在 namespace 准备阶段也可生效。"""
    os.setpgid(0, 0)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


def _fork_namespace_runner():
    """阻塞取消信号直到记录 runner PID，防止刚 fork 的 namespace init 成为孤儿。

    supervisor 收到 SIGTERM 后直接杀死并回收 namespace init；内核随后终止该
    PID namespace 内其他进程，包括另建 session 的后台程序。不能仅杀宿主进程组。
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    pid = None
    try:
        pid = os.fork()
        if pid:
            def terminate(_number, _frame):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                try:
                    # 正常 waitpid 可能刚回收 runner；旧 PID 此时不能再用于发信号。
                    try:
                        found, _ = _wait_child(pid, getattr(os, "WNOHANG", 1))
                    except ChildProcessError:
                        found = pid
                    if found != pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        _wait_child(pid)
                finally:
                    os._exit(128 + signal.SIGTERM)
            signal.signal(signal.SIGTERM, terminate)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return pid
    except BaseException:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_child(pid)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, set(previous) - {signal.SIGTERM})


def _abort_run(pid):
    """请求 supervisor 清理 namespace init，再回收 supervisor，之后才清理 rootfs。"""
    try:
        found, _ = _wait_child(pid, getattr(os, "WNOHANG", 1))
    except ChildProcessError:
        return
    if found == pid:
        return  # 已退出的直接子进程由此次检查回收，不向旧 PID 发信号。
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        _wait_child(pid)
    except ChildProcessError:
        pass  # 已被回收时不能再向这个可能复用的 PID 发信号。


def _exit_with_diagnostics(fd):
    """诊断管道损坏也必须用 _exit 退出，不能回到继承的构建器调用栈。"""
    try:
        os.write(fd, traceback.format_exc().encode("utf-8", "replace"))
    except OSError:
        pass
    finally:
        os._exit(127)


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
    os.chown(path, 0, 0)
    os.chmod(path, mode)


def _runtime_directory(path, mode=0o755):
    """只为新挂载目录设置确定的权限，避免宿主 umask 和父目录 setgid 泄漏。"""
    if path.is_symlink():
        raise BuildError("RUN setup directory must not be a symlink: " + str(path))
    if path.exists():
        if not path.is_dir():
            raise BuildError("RUN setup path must be a directory: " + str(path))
        return
    path.mkdir(mode=mode)
    # rootless 调用发生在 user namespace 内，此处的 0:0 即映射的宿主身份。
    os.chown(path, 0, 0)
    path.chmod(mode)


def _prepare_devices(merged, rootless=False):
    devices = merged / "dev"
    if devices.is_symlink():
        raise BuildError("Image /dev must not be a symlink during RUN setup")
    _runtime_directory(devices)
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
    _runtime_directory(pts)
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
        # execute 也可被直接调用，不能仅依赖 builder 的 Dockerfile 挂载预检。
        targets = set()
        validated_mounts = []
        for kind, source, target in mounts:
            if kind not in ("cache", "secret"):
                raise BuildError("Unsupported RUN mount type")
            validated_mounts.append((kind, source, normalize_mount_target(target, targets)))
        mounts = validated_mounts
        log_callback = getattr(self, "log", None)
        pid, diagnostic_pipe, stdout_pipe, stderr_pipe = _fork_with_pipes(log_callback is not None)
        read_fd, write_fd = diagnostic_pipe
        log_read, log_write = stdout_pipe
        err_read, err_write = stderr_pipe
        if pid == 0:
            try:
                _prepare_run_supervisor()
                os.close(read_fd)
                if log_callback is not None:
                    os.close(log_read)
                    os.close(err_read)
                    os.dup2(log_write, 1)
                    os.dup2(err_write, 2)
                    os.close(log_write)
                    os.close(err_write)
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
                        _runtime_directory(destination)
                    destination = destination / parts[-1]
                    if destination.is_symlink():
                        raise BuildError("RUN mount target is an image symlink")
                    if kind == "cache":
                        _runtime_directory(destination)
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
                _runtime_directory(proc)
                temporary = merged / "tmp"
                if temporary.is_symlink():
                    raise BuildError("Image /tmp must not be a symlink during RUN setup")
                _runtime_directory(temporary, mode=0o1777)
                _prepare_devices(merged, self.sandbox == "rootless")
                runner = _fork_namespace_runner()
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
                        # 仅修改执行命令的子进程；RUN 自己仍可通过 umask 显式覆盖。
                        os.umask(0o022)
                        os.execvpe(argv[0], argv, env)
                    except BaseException:
                        _exit_with_diagnostics(write_fd)
                _, status = _wait_child(runner)
                os._exit(_wait_status(status))
            except BaseException:
                _exit_with_diagnostics(write_fd)
        owned_fds = {fd for pair in (diagnostic_pipe, stdout_pipe, stderr_pipe)
                     for fd in pair if fd is not None}
        fd_owner_lock = threading.Lock()
        pending_log_fds = set()
        log_threads = []
        log_errors = []
        log_error_lock = threading.Lock()
        log_failed = threading.Event()
        reaped = False

        def close_owned(fd):
            with fd_owner_lock:
                owned_fds.discard(fd)
            os.close(fd)

        def record_log_error(source, error):
            with log_error_lock:
                if not log_errors:
                    log_errors.append((source, error))
                log_failed.set()

        def forward_logs(fd, source):
            try:
                # start 握手被中断时，原生线程可能已创建而 ident 尚未登记。
                # 只有领到所有权才可打开 FD；取消先撤销交接，迟启动线程直接退出。
                with fd_owner_lock:
                    if fd not in pending_log_fds:
                        return
                    pending_log_fds.remove(fd)
                    owned_fds.discard(fd)
                with fdopen_owned(fd, "rb") as output_stream:
                    for line in _iter_run_log_fragments(output_stream):
                        if log_failed.is_set():
                            continue
                        try:
                            log_callback(line, source)
                        except BaseException as exc:
                            # 回调失败后继续排空；日志失败不能改变子进程写入和退出行为。
                            record_log_error(source, exc)
            except BaseException as exc:
                record_log_error(source, exc)

        try:
            close_owned(write_fd)
            if log_callback is not None:
                close_owned(log_write)
                close_owned(err_write)
                for fd, source in ((log_read, "stdout"), (err_read, "stderr")):
                    with fd_owner_lock:
                        pending_log_fds.add(fd)
                    thread = threading.Thread(target=forward_logs, args=(fd, source), daemon=True)
                    log_threads.append(thread)
                    thread.start()
            # fdopen_owned 失败也会关闭描述符，不能在 finally 中再次关闭它。
            with fd_owner_lock:
                owned_fds.discard(read_fd)
            with fdopen_owned(read_fd, "rb") as stream:
                diagnostics = _read_run_diagnostics(stream)
            _, status = _wait_child(pid)
            reaped = True
            for thread in log_threads:
                thread.join()
            code = _wait_status(status)
            if code:
                detail = diagnostics.strip() or "process exited with code {}".format(code)
                raise BuildError("RUN failed: " + detail)
            if log_errors:
                source, error = log_errors[0]
                raise BuildError("RUN log forwarding failed for {}: {}".format(
                    source, type(error).__name__)) from error
        except BaseException:
            log_failed.set()
            if not reaped:
                _abort_run(pid)
            raise
        finally:
            # 领取与撤销使用同一把锁；ident 只决定可否 join，不再决定 FD 所有权。
            with fd_owner_lock:
                pending_log_fds.clear()
                remaining_fds = tuple(owned_fds)
                owned_fds.clear()
            for fd in remaining_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for thread in log_threads:
                if thread.ident is not None:
                    thread.join()
