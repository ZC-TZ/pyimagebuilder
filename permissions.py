"""统一检查层权限声明，并合并仍属于同一文件系统节点的扩展属性。"""

import copy

from errors import BuildError
from image_identity import checked_layer_id


XATTR_PREFIX = "SCHILY.xattr."


def validate_metadata(member, materialize=False):
    """在 whiteout 或写盘前验证权限，禁止系统调用截断、哨兵值或编码改变语义。

    只接受 Unix 权限位（包含 setuid/setgid/sticky）。归档转存保留二进制 xattr；
    当前 RUN 写盘仅支持 UTF-8 xattr，必须在整层预检时失败，避免部分修改。
    """
    checked_layer_id(member.uid, "UID")
    checked_layer_id(member.gid, "GID")
    if type(member.mode) is not int or not 0 <= member.mode <= 0o7777:
        raise BuildError("Invalid layer permission mode: " + repr(member.mode))
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
