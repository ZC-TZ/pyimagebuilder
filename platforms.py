"""校验支持的 Linux 目标平台，并规范化宿主 CPU 架构。"""

import platform

from errors import BuildError


ARCHITECTURES = ("amd64", "arm64")


def normalize_platform(value):
    """只接受当前读取器、写入器和执行器实现的两个 Linux 平台。"""
    if value not in ("linux/amd64", "linux/arm64"):
        raise BuildError("Platform must be linux/amd64 or linux/arm64")
    return value


def architecture(value):
    """从已验证的 Linux 平台中取得 OCI 架构字段。"""
    return normalize_platform(value).split("/", 1)[1]


def host_architecture():
    """把当前机器标识映射为受支持的 OCI 架构；无法识别时返回 None。"""
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine)


def require_foreign_binfmt(target_architecture):
    """要求启用带 F 标志的 binfmt 解释器，使跨架构程序在 chroot 后仍能执行。"""
    from pathlib import Path

    directory = Path("/proc/sys/fs/binfmt_misc")
    if not directory.is_dir():
        raise BuildError("Foreign RUN needs a preconfigured binfmt_misc/QEMU interpreter")
    names = {"amd64": ("qemu-x86_64", "qemu-amd64"),
             "arm64": ("qemu-aarch64", "qemu-arm64")}[target_architecture]
    for entry in directory.iterdir():
        if entry.name in ("register", "status") or not entry.is_file():
            continue
        try:
            value = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = value.splitlines()
        interpreter = next((line[len("interpreter "):].strip().lower()
                            for line in lines if line.startswith("interpreter ")), "")
        if (lines and lines[0] == "enabled" and
                any(name in interpreter for name in names) and
                any(line.startswith("flags:") and "F" in line.split(":", 1)[1]
                    for line in lines)):
            return
    raise BuildError("Foreign RUN requires enabled binfmt_misc QEMU for {} with F flag".format(
        target_architecture))
