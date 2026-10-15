"""统一检查层权限声明，并合并仍属于同一文件系统节点的扩展属性。"""

import copy
import stat

from errors import BuildError
from image_identity import checked_layer_id


XATTR_PREFIX = "SCHILY.xattr."


def layer_permission_mode(member):
    """分离权限和兼容的 st_mode 类型位，类型必须与 tar typeflag 一致。"""
    mode = member.mode
    if type(mode) is not int or mode < 0 or mode > 0o177777:
        raise BuildError("Invalid layer permission mode: " + repr(mode))
    if mode <= 0o7777:
        return mode
    kind = stat.S_IFMT(mode)
    expected = (stat.S_IFDIR if member.isdir() else
                stat.S_IFREG if member.isfile() or member.islnk() else
                stat.S_IFLNK if member.issym() else
                stat.S_IFIFO if member.isfifo() else
                stat.S_IFCHR if member.ischr() else
                stat.S_IFBLK if member.isblk() else None)
    if kind != expected:
        raise BuildError("Invalid layer permission mode: " + repr(mode))
    # 例如 16877=040755；只在内存中取 0755，原始 layer 字节与 DiffID 不变。
    return stat.S_IMODE(mode)


def validate_metadata(member, materialize=False):
    """在 whiteout 或写盘前验证权限，禁止系统调用截断、哨兵值或编码改变语义。

    返回 Unix 权限位（包含 setuid/setgid/sticky），兼容匹配 typeflag 的 st_mode。
    不修改来源 TarInfo。归档转存保留二进制 xattr；
    当前 RUN 写盘仅支持 UTF-8 xattr，必须在整层预检时失败，避免部分修改。
    """
    checked_layer_id(member.uid, "UID")
    checked_layer_id(member.gid, "GID")
    mode = layer_permission_mode(member)
    if not isinstance(member.pax_headers, dict):
        raise BuildError("Invalid layer PAX metadata")
    for key, value in member.pax_headers.items():
        if not isinstance(key, str):
            raise BuildError("Invalid layer PAX key")
        if key.startswith(XATTR_PREFIX):
            name = key[len(XATTR_PREFIX):]
            if not name or "\0" in name or not isinstance(value, str):
                raise BuildError("Invalid layer xattr declaration")
            try:
                value.encode("utf-8", "strict" if materialize else "surrogateescape")
            except UnicodeError as exc:
                raise BuildError("Cannot preserve xattr {}: unsupported encoding".format(name)) from exc
    return mode


def normalized_metadata(member):
    """提供只含权限位的解析头；需要改变 mode 时复制，绝不重写来源层或原始头。"""
    mode = validate_metadata(member)
    if mode == member.mode:
        return member
    updated = copy.copy(member)
    updated.mode = mode
    return updated


def merge_metadata(previous, current, clear_capability=False):
    """更新已有节点的头，保留未声明覆盖的 xattr，不修改来源 TarInfo。

    目录头更新不会重建目录。硬链接也复用 inode，但其 lchown 会清除普通
    文件的旧 capability；显式新 capability 仍由当前头重新设置。
    """
    updated = copy.copy(current)
    attrs = {key: value for key, value in previous.pax_headers.items()
             if key.startswith(XATTR_PREFIX) and
             not (clear_capability and key == XATTR_PREFIX + "security.capability")}
    attrs.update(current.pax_headers)
    updated.pax_headers = attrs
    return updated
