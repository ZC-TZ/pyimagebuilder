class BuildError(Exception):
    """构建输入或镜像约束不满足要求。"""


class UnsupportedInstruction(BuildError):
    """当前实现或执行环境不支持所请求的 Dockerfile 语义。"""


class BaseImageNotFound(BuildError):
    """FROM 没有对应的可用基础镜像。"""


class ArchiveError(BuildError):
    """镜像归档、层文件或 CAS 元数据结构不合法。"""
