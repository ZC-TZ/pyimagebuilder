"""在进入 namespace 前验证 RUN 的临时 cache/secret 挂载。"""

import hashlib
import os
import re
from contextlib import contextmanager, ExitStack
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from compat import is_linked_directory
from layer import container_path
from path_policy import output_path


def secrets(items):
    """预检 --secret 的 ID 与宿主绝对文件路径绑定。"""
    result = {}
    for item in items or ():
        key, separator, source = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            raise BuildError("--secret must be id=/absolute/file")
        raw = Path(source)
        path = raw.resolve()
        if not raw.is_absolute() or not path.is_file():
            raise BuildError("Secret source must be an existing file: " + key)
        result[key] = path
    return result


def normalize_mount_target(target, targets):
    """先归一化镜像内路径，再检查根目录、虚拟文件系统和挂载重叠。

    posixpath 保留双前导斜杠，但执行器用 lstrip 定位镜像内路径；统一成单斜杠
    才能让预检与实际挂载看到同一个目标。不能在归一化前判断 /cache/.. 是否为根。
    """
    if not isinstance(target, str) or not target.startswith("/") or "\0" in target:
        raise BuildError("RUN mount target must be an absolute non-root path")
    target = container_path(target, non_root=True)
    if target == "/":
        raise BuildError("RUN mount target must be an absolute non-root path")
    if target in ("/proc", "/sys", "/dev") or target.startswith(
            ("/proc/", "/sys/", "/dev/")):
        raise BuildError("RUN mount cannot cover a virtual filesystem")
    if target in targets or any(target.startswith(old + "/") or old.startswith(target + "/")
                                for old in targets):
        raise BuildError("Overlapping RUN mount targets")
    targets.add(target)
    return target


def prepare(options, secret_sources, cache_root, workspace, stage_platform):
    """解析 cache/secret 挂载来源，拒绝重叠目标和虚拟文件系统路径。"""
    mounts = []
    targets = set()
    for item in options:
        kind = item["type"]
        if kind not in ("cache", "secret"):
            raise UnsupportedInstruction("Unsupported RUN mount type")
        allowed = ({"type", "target", "id", "sharing"} if kind == "cache" else
                   {"type", "target", "id", "required"})
        if set(item) - allowed:
            raise UnsupportedInstruction("Unsupported RUN mount option")
        identifier = item.get("id")
        if kind == "cache":
            target = item.get("target")
            if not target or not target.startswith("/"):
                raise BuildError("Cache mount needs an absolute target")
            if item.get("sharing", "locked") != "locked":
                raise UnsupportedInstruction("Only sharing=locked cache mounts are supported")
            identifier = identifier or target
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", identifier):
                raise BuildError("Invalid cache mount id")
            source = hashlib.sha256((stage_platform + "\0" + identifier).encode()).hexdigest()
        else:
            if not identifier or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", identifier):
                raise BuildError("Secret mount needs a valid id")
            target = item.get("target", "/run/secrets/" + identifier)
            required = item.get("required", "true")
            if required not in ("true", "false"):
                raise BuildError("Secret required must be true or false")
            # 即使可选 secret 未提供，其声明路径也必须通过语法和边界校验。
            target = normalize_mount_target(target, targets)
            source = secret_sources.get(identifier)
            if source is None:
                if required == "false":
                    continue
                raise BuildError("Required secret was not supplied: " + identifier)
        if kind == "cache":
            target = normalize_mount_target(target, targets)
        mounts.append((kind, source, target))
    # 所有声明预检完成后才创建持久目录；后续无效参数不能留下先前的 cache mount。
    prepared = []
    for kind, source, target in mounts:
        if kind == "cache":
            base = Path(cache_root) if cache_root is not None else Path(workspace)
            mount_root = base / "run-mounts"
            if is_linked_directory(mount_root):
                raise BuildError("Unsafe cache mount root")
            mount_root.mkdir(parents=True, exist_ok=True)
            source = mount_root / source
            if is_linked_directory(source):
                raise BuildError("Unsafe cache mount directory")
            try:
                source.mkdir()
            except FileExistsError:
                pass
            else:
                # bind mount 显示的是来源目录权限；只修正镜像内挂载点并不够。
                # 单 UID/GID rootless 映射对应宿主当前身份，root 模式则对应 0:0。
                if hasattr(os, "geteuid"):
                    os.chown(source, os.geteuid(), os.getegid())
                source.chmod(0o755)
            if not source.is_dir():
                raise BuildError("Unsafe cache mount directory")
        prepared.append((kind, source, target))
    return prepared


@contextmanager
def locked_caches(mounts):
    """在 Linux 上串行化相同 cache ID 的写入者，避免并发破坏缓存。"""
    sources = sorted({path for kind, path, _ in mounts if kind == "cache"})
    if not sources:
        yield
        return
    import fcntl
    with ExitStack() as stack:
        for source in sources:
            stream = stack.enter_context(open(output_path(source.parent / (source.name + ".lock")), "a+b"))
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, stream.fileno(), fcntl.LOCK_UN)
        yield
