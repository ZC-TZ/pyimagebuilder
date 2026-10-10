"""统一检查镜像账户 ID，避免文件层与 RUN 接受无法安全执行的属主。"""

import re

from errors import BuildError


MAX_ID = (1 << 31) - 1


def checked_identity_id(value, label):
    """按 Docker 执行用户的兼容范围检查非负 UID/GID，非法值抛出 BuildError。"""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise BuildError("Invalid image {}: {}".format(label, value))
    try:
        number = int(value)
    except ValueError as exc:
        raise BuildError("Invalid image {}".format(label)) from exc
    if number > MAX_ID:
        raise BuildError("Image {} must be between 0 and {}: {}".format(label, MAX_ID, value))
    return number
