"""供所有入口共用的配置解析；凭据通过环境变量引用，不写入配置。"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from errors import BuildError


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _json_object(pairs):
    """拒绝重复键，避免后一个值静默覆盖已审查的鉴权或缓存配置。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key: " + key)
        result[key] = value
    return result


def _json_constant(value):
    """配置采用标准 JSON；NaN/Infinity 不能作为合法数值进入后续校验。"""
    raise ValueError("Non-JSON configuration number: " + value)


def _known_fields(value, allowed, label):
    """尽早定位拼错或放错层级的字段，不允许配置被静默忽略。"""
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise BuildError("Unknown configuration fields in {}: {}".format(label, ", ".join(unknown)))


def _optional_text(value, key, label, nullable=False):
    """共享入口先校验文本类型，避免某个命令在较深调用处出现 TypeError。"""
    if key in value and not (nullable and value[key] is None):
        if not isinstance(value[key], str) or not value[key].strip():
            raise BuildError(label + "." + key + " must be a nonempty string")


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
    """严格加载 schemaVersion=2 配置，并相对配置文件目录解析路径。

    共享入口先检查 JSON、字段及基础类型；Fast 的选中 profile 再检查文件和部署语义。
    """
    path = config_path(candidate)
    if not path.is_file():
        if required or candidate is not None or os.environ.get("PYIMAGEBUILDER_CONFIG"):
            raise BuildError("Configuration file not found: " + str(path))
        return {"schemaVersion": 2, "cache": {}, "repositories": {}, "fast": {}}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (OSError, ValueError) as exc:
        raise BuildError("Cannot read configuration {}: {}".format(path, exc)) from exc
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or
            data.get("schemaVersion") != 2):
        raise BuildError("config.json requires schemaVersion 2")
    _known_fields(data, ("schemaVersion", "cache", "repositories", "fast"), "config")
    if any(not isinstance(data.get(key, {}), dict) for key in ("cache", "repositories", "fast")):
        raise BuildError("cache, repositories, and fast must be objects")
    root = path.parent
    data.setdefault("cache", {})
    data.setdefault("repositories", {})
    data.setdefault("fast", {})
    _known_fields(data["cache"], ("imageStore", "layerCache"), "cache")
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
        _known_fields(entry, ("source", "username", "passwordEnv", "caFile",
                              "insecureHttp", "workers", "authHost"), "repositories." + host)
        for key in ("username", "authHost"):
            _optional_text(entry, key, "repositories." + host, nullable=True)
        if "passwordEnv" in entry and (not isinstance(entry["passwordEnv"], str) or
                                       not ENV_NAME.fullmatch(entry["passwordEnv"])):
            raise BuildError("Invalid passwordEnv for " + host)
        if "caFile" in entry:
            entry["caFile"] = _path(entry["caFile"], root, "caFile")
        if "insecureHttp" in entry and type(entry["insecureHttp"]) is not bool:
            raise BuildError("insecureHttp must be a boolean for " + host)
        if "workers" in entry and (type(entry["workers"]) is not int or
                                   not 0 <= entry["workers"] <= 64):
            raise BuildError("workers must be between 0 and 64 for " + host)
    fast = data["fast"]
    _known_fields(fast, ("profiles", "defaultProfile"), "fast")
    if not isinstance(fast.get("profiles", {}), dict):
        raise BuildError("fast.profiles must be an object")
    _optional_text(fast, "defaultProfile", "fast")
    for name, profile in fast.get("profiles", {}).items():
        if not name.strip() or not isinstance(profile, dict):
            raise BuildError("fast.profiles entries must be named objects")
        if "baseCacheDir" in profile:
            raise BuildError("Move fast profile baseCacheDir to cache.imageStore in config.json")
        _known_fields(profile, ("flavor", "baseImage", "baseTar", "baseUrl", "owner",
                                "deployDir", "serverConfig", "outputDir"), "fast.profiles." + name)
        for key in ("flavor", "baseImage", "baseTar", "baseUrl", "owner", "deployDir"):
            _optional_text(profile, key, "fast.profiles." + name)
        for key in ("serverConfig", "outputDir"):
            _optional_text(profile, key, "fast.profiles." + name, nullable=True)
    if "defaultProfile" in fast and fast["defaultProfile"] not in fast.get("profiles", {}):
        raise BuildError("fast.defaultProfile does not name an existing profile")
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
