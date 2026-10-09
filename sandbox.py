"""为 RUN 设置完成后的进程安装 Linux seccomp 禁用列表；未知架构拒绝执行。"""

import ctypes
import errno
import os
import platform

from errors import BuildError


BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_JMP_JGE_K = 0x35
BPF_JMP_JSET_K = 0x45
BPF_RET_K = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000 | errno.EPERM
SECCOMP_RET_ENOSYS = 0x00050000 | errno.ENOSYS
SECCOMP_RET_ALLOW = 0x7FFF0000
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2


class SockFilter(ctypes.Structure):
    """表示 Linux ABI 中一条经典 BPF seccomp 指令。"""
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    """表示传给 prctl 的经典 BPF 过滤程序。"""
    _fields_ = [("len", ctypes.c_ushort),
                ("filter", ctypes.POINTER(SockFilter))]


# 先列旧版架构专用系统调用编号，再加入现代通用编号。
DENIED = {
    "x86_64": (101, 155, 161, 165, 166, 175, 176, 246, 248, 249, 250,
               272, 298, 304, 308, 313, 321, 323),
    "aarch64": (39, 40, 41, 51, 104, 105, 106, 117, 217, 218, 219,
                241, 265, 268, 273, 280, 282),
}
COMMON_DENIED = (425, 428, 429, 430, 431, 432, 433, 442)
AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
CLONE_SYSCALL = {"x86_64": 56, "aarch64": 220}
CLONE_NAMESPACE_MASK = (0x00020000 | 0x02000000 | 0x04000000 |
                        0x08000000 | 0x10000000 | 0x20000000 |
                        0x40000000 | 0x00000080)


def build_filter(architecture):
    """按受支持架构生成 seccomp 禁用列表；未知架构直接报错，不能退化为无过滤。"""
    if architecture not in DENIED:
        raise BuildError("Hardened RUN seccomp supports only x86_64 and aarch64")
    instructions = [
        (BPF_LD_W_ABS, 0, 0, 4),
        (BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH[architecture]),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, 0),
    ]
    if architecture == "x86_64":
        # 拒绝 x32 系统调用编号，防止绕过原生架构的禁用列表。
        instructions.extend(((BPF_JMP_JGE_K, 0, 1, 0x40000000),
                             (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS)))
    # 允许普通 fork/线程创建，但拒绝 clone 中创建 namespace 的标志。
    instructions.extend(((BPF_JMP_JEQ_K, 0, 3, CLONE_SYSCALL[architecture]),
                         (BPF_LD_W_ABS, 0, 0, 16),
                         (BPF_JMP_JSET_K, 0, 1, CLONE_NAMESPACE_MASK),
                         (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO),
                         (BPF_LD_W_ABS, 0, 0, 0),
                         # 返回 ENOSYS，让 libc 在新内核上回退到受检查的 clone。
                         (BPF_JMP_JEQ_K, 0, 1, 435),
                         (BPF_RET_K, 0, 0, SECCOMP_RET_ENOSYS)))
    for number in sorted(set(DENIED[architecture] + COMMON_DENIED)):
        instructions.extend(((BPF_JMP_JEQ_K, 0, 1, number),
                             (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO)))
    instructions.append((BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    return instructions


def install_filter():
    """在 namespace 与挂载准备完成后，为 hardened/rootless RUN 安装 seccomp 过滤器。"""
    instructions = build_filter(platform.machine().lower())
    entries = (SockFilter * len(instructions))(*(SockFilter(*item) for item in instructions))
    program = SockFprog(len(instructions), entries)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                  ctypes.byref(program), 0, 0) != 0:
        raise BuildError("seccomp filter installation failed: " + os.strerror(ctypes.get_errno()))
