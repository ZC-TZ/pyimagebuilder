"""应用 Dockerfile 配置变化，同时保留基础镜像的其他运行字段。"""

import copy

from errors import ArchiveError
from image_reader import image_config_bytes, image_history, same_json_value
from reproducible import timestamp


class ImageConfig:
    """复制基础配置并记录指令变化，避免原地修改被其他阶段复用的配置。"""
    def __init__(self, base, source_date_epoch=0, config_raw=None, manifest_raw=None):
        self.source_date_epoch = source_date_epoch
        self.original = copy.deepcopy(base)
        self.original_raw = image_config_bytes(base, config_raw) if config_raw is not None else None
        self.manifest_raw = manifest_raw
        self.value = copy.deepcopy(base)
        if self.value.get("config") is None:
            self.value["config"] = {}
        self.runtime = self.value["config"]
        if not isinstance(self.runtime, dict):
            raise ArchiveError("Image config.config must be an object")
        for key in ("ExposedPorts", "Labels", "Volumes"):
            value = self.runtime.get(key)
            if value is not None and not isinstance(value, dict):
                raise ArchiveError("Base image config.{} must be an object".format(key))
            if isinstance(value, dict):
                for item_key, item_value in value.items():
                    if not isinstance(item_key, str) or not item_key:
                        raise ArchiveError("Base image config.{} has an invalid key".format(key))
                    if key == "Labels":
                        if not isinstance(item_value, str):
                            raise ArchiveError("Base image config.Labels values must be strings")
                    elif not isinstance(item_value, dict):
                        raise ArchiveError("Base image config.{} values must be objects".format(key))
        environment = self.runtime.get("Env")
        if environment is not None and (not isinstance(environment, list) or
                                        any(not isinstance(item, str) or "=" not in item or item.startswith("=")
                                            for item in environment)):
            raise ArchiveError("Base image config.Env must contain KEY=value strings")
        shell = self.runtime.get("Shell")
        if shell is not None and (not isinstance(shell, list) or not shell or
                                  any(not isinstance(item, str) or not item for item in shell)):
            raise ArchiveError("Base image config.Shell must be a nonempty string array")
        for key in ("User", "WorkingDir"):
            value = self.runtime.get(key)
            if value is not None and not isinstance(value, str):
                raise ArchiveError("Base image config.{} must be a string".format(key))
        onbuild = self.runtime.get("OnBuild")
        if onbuild is None:
            if "OnBuild" in self.runtime:
                # null 与缺省均没有触发器；仅补全工作状态，export 仍保留未变的原始配置。
                self.runtime["OnBuild"] = []
        elif not isinstance(onbuild, list) or any(not isinstance(item, str) for item in onbuild):
            raise ArchiveError("Base image config.OnBuild must be an array of strings or null")
        self.rootfs = self.value.setdefault("rootfs", {})
        self.diff_ids = self.rootfs.setdefault("diff_ids", [])
        if not isinstance(self.diff_ids, list):
            raise ArchiveError("Image config diff_ids must be an array")
        self.history = image_history(self.value, len(self.diff_ids))
        self.value["history"] = self.history
        if not self.history and self.diff_ids:
            self.history.extend({"created_by": "imported base layer"} for _ in self.diff_ids)
        # 缺省运行字段与导入层 history 是工作状态的补全，不是 Dockerfile 的实际变更。
        # 仅 FROM 的构建不能因此丢失基础镜像的原始字节身份。
        self.initial = copy.deepcopy(self.value)

    def export(self):
        """返回实际交付配置及可复用的原始字节；有真实变更时才生成新配置。

        返回深拷贝，供已完成阶段保存，避免后续指令污染阶段快照。
        """
        if self.original_raw is not None and same_json_value(self.value, self.initial):
            return copy.deepcopy(self.original), self.original_raw
        return copy.deepcopy(self.value), None

    def record(self, instruction, diff_id=None):
        """追加一条 history；仅在产生文件系统层时追加 diff_id。"""
        now = timestamp(self.source_date_epoch)
        item = {"created": now, "created_by": instruction.raw}
        if diff_id is None:
            item["empty_layer"] = True
        else:
            self.diff_ids.append(diff_id)
        self.history.append(item)
        self.value["created"] = now

    def set_env(self, pairs):
        """合并 ENV，保留未被覆盖的基础环境变量。"""
        existing = {}
        for item in self.runtime.get("Env") or []:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                existing[key] = value
        for key, value in pairs:
            existing[key] = value
        self.runtime["Env"] = ["{}={}".format(key, value) for key, value in existing.items()]

    def set_workdir(self, value):
        """设置已按 Dockerfile 路径规则解析的运行工作目录。"""
        self.runtime["WorkingDir"] = value

    def set_command(self, key, value):
        """写入已经解析的 Cmd/Entrypoint 等运行命令字段。"""
        self.runtime[key] = value

    def set_exposed(self, ports):
        """把声明端口合并到基础 ExposedPorts 元数据。"""
        current = self.runtime.get("ExposedPorts") or {}
        current.update({port: {} for port in ports})
        self.runtime["ExposedPorts"] = current

    def set_labels(self, pairs):
        """把 Dockerfile 标签合并到基础 Labels。"""
        current = self.runtime.get("Labels") or {}
        current.update(pairs)
        self.runtime["Labels"] = current

    def set_volumes(self, paths):
        """把声明卷路径合并到基础 Volumes 元数据。"""
        current = self.runtime.get("Volumes") or {}
        current.update({path: {} for path in paths})
        self.runtime["Volumes"] = current
