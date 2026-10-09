"""在进入 namespace 前验证 RUN 的临时 cache/secret 挂载。"""

import hashlib
import re
from contextlib import contextmanager, ExitStack
from pathlib import Path

from errors import BuildError, UnsupportedInstruction
from layer import container_path


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


def prepare(options, secret_sources, cache_root, workspace, stage_platform):
    """解析 cache/secret 挂载来源，拒绝重叠目标和虚拟文件系统路径。"""
    mounts = []
    targets = set()
    for item in options:
        kind = item["type"]
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
            key = hashlib.sha256((stage_platform + "\0" + identifier).encode()).hexdigest()
            base = Path(cache_root) if cache_root is not None else Path(workspace)
            mount_root = base / "run-mounts"
            mount_root.mkdir(parents=True, exist_ok=True)
            if mount_root.is_symlink():
                raise BuildError("Unsafe cache mount root")
            source = mount_root / key
            source.mkdir(parents=True, exist_ok=True)
            if source.is_symlink() or not source.is_dir():
                raise BuildError("Unsafe cache mount directory")
        else:
            if not identifier or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", identifier):
                raise BuildError("Secret mount needs a valid id")
            target = item.get("target", "/run/secrets/" + identifier)
            required = item.get("required", "true")
            if required not in ("true", "false"):
                raise BuildError("Secret required must be true or false")
            source = secret_sources.get(identifier)
            if source is None:
                if required == "false":
                    continue
                raise BuildError("Required secret was not supplied: " + identifier)
        if not target.startswith("/") or target == "/":
            raise BuildError("RUN mount target must be an absolute non-root path")
        target = container_path(target)
        if target in ("/proc", "/sys", "/dev") or target.startswith(
                ("/proc/", "/sys/", "/dev/")):
            raise BuildError("RUN mount cannot cover a virtual filesystem")
        if target in targets or any(target.startswith(old + "/") or old.startswith(target + "/")
                                    for old in targets):
            raise BuildError("Overlapping RUN mount targets")
        targets.add(target)
        mounts.append((kind, source, target))
    return mounts


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
            stream = stack.enter_context(open(source.parent / (source.name + ".lock"), "a+b"))
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, stream.fileno(), fcntl.LOCK_UN)
        yield
