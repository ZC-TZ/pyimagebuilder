"""供所有入口共用的配置解析；凭据通过环境变量引用，不写入配置。"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from errors import BuildError


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def config_path(candidate=None):
    """按显式路径、环境变量、当前目录和工具目录顺序选择 config.json。"""
    if candidate is not None:
        return Path(candidate).resolve()
    if os.environ.get("PYIMAGEBUILDER_CONFIG"):
        return Path(os.environ["PYIMAGEBUILDER_CONFIG"]).resolve()
    local = Path.cwd() / "config.json"
    return local if local.is_file() else Path(__file__).resolve().parent / "config.json"


def _path(value, root, label):
    if not isinstance(value, str) or not value:
        raise BuildError(label + " must be a nonempty path")
    item = Path(value)
    return (item if item.is_absolute() else root / item).resolve()


def load_settings(candidate=None, required=False):
    """加载 schemaVersion=2 配置，并相对配置文件目录解析所配置的路径。"""
    path = config_path(candidate)
    if not path.is_file():
        if required or candidate is not None or os.environ.get("PYIMAGEBUILDER_CONFIG"):
            raise BuildError("Configuration file not found: " + str(path))
        return {"schemaVersion": 2, "cache": {}, "repositories": {}, "fast": {}}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("Cannot read configuration {}: {}".format(path, exc)) from exc
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise BuildError("config.json requires schemaVersion 2")
    if any(not isinstance(data.get(key, {}), dict) for key in ("cache", "repositories", "fast")):
        raise BuildError("cache, repositories, and fast must be objects")
    root = path.parent
    data.setdefault("cache", {})
    data.setdefault("repositories", {})
    data.setdefault("fast", {})
    for key in ("imageStore", "layerCache"):
        if key in data["cache"]:
            data["cache"][key] = _path(data["cache"][key], root, "cache." + key)
    # 有配置文件时默认将镜像库放在其目录下，便于随项目配置定位；
    # 无配置的独立命令仍使用临时目录中的默认镜像库。
    data["cache"].setdefault("imageStore", root / "data" / "image-store")
    for host, entry in data["repositories"].items():
        if (not isinstance(host, str) or not host or "/" in host or
                not isinstance(entry, dict)):
            raise BuildError("Invalid repositories entry")
        if entry.get("source", "registry") not in ("registry", "artifactory"):
            raise BuildError("Invalid repository source for " + host)
        if "password" in entry or "token" in entry:
            raise BuildError("Do not store passwords or tokens in config.json")
        if "passwordEnv" in entry and not ENV_NAME.fullmatch(str(entry["passwordEnv"])):
            raise BuildError("Invalid passwordEnv for " + host)
        if "caFile" in entry:
            entry["caFile"] = _path(entry["caFile"], root, "caFile")
        if "insecureHttp" in entry and type(entry["insecureHttp"]) is not bool:
            raise BuildError("insecureHttp must be a boolean for " + host)
        if "workers" in entry and (type(entry["workers"]) is not int or
                                   not 0 <= entry["workers"] <= 64):
            raise BuildError("workers must be between 0 and 64 for " + host)
    fast = data["fast"]
    if not isinstance(fast.get("profiles", {}), dict):
        raise BuildError("fast.profiles must be an object")
    if "defaultProfile" in fast and not isinstance(fast["defaultProfile"], str):
        raise BuildError("fast.defaultProfile must be a string")
    return data, path


def repository_settings(settings, reference):
    """按镜像引用的主机选择仓库鉴权和传输设置。"""
    if "://" in reference:
        host = urlsplit(reference).netloc
    else:
        host = reference.split("/", 1)[0]
    return settings.get("repositories", {}).get(host, {})


def cache_directory(settings, name, fallback):
    """返回配置中的缓存路径；未配置时使用调用方的默认值。"""
    return settings.get("cache", {}).get(name, fallback)
