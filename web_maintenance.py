"""工作台维护操作：配置预览采用同目录验证，缓存清理只执行确认过的文件计划。"""

import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from cache_activity import cache_activity
from compat import unlink_missing
from errors import BuildError
from fast import DEFAULTS, _validate_atom, load_profile
from file_resources import fdopen_owned
from image_cli import prune_old_cache
from image_reader import MAX_JSON, digest_hex, parse_image_json
from settings import cache_directory, load_settings
from store_lock import file_lock


PROFILE_FIELDS = ("flavor", "baseImage", "baseTar", "baseUrl", "owner", "deployDir", "serverConfig", "outputDir")


def _config_bytes(path):
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_JSON:
        raise BuildError("配置必须是普通 JSON 文件")
    return path.read_bytes()


def _revision(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


@contextmanager
def candidate_profile(path, data):
    """只替换选中的模板字段；在配置目录验证相对路径，不写原文件。"""
    original = _config_bytes(path)
    if original is not None:
        load_settings(path)
    config = json.loads(original.decode("utf-8-sig")) if original is not None else {"schemaVersion": 2}
    name = _validate_atom(data.get("name", "").strip(), "profile")
    section = config.setdefault("fast", {})
    profiles = section.setdefault("profiles", {})
    previous = profiles.get(name)
    mode = data.get("mode")
    if (mode == "new" and previous is not None) or (mode == "edit" and previous is None):
        raise BuildError("模板已存在或已移除，请刷新配置页")
    if mode not in ("new", "edit"):
        raise BuildError("无效的模板操作")
    profile = {key: data.get(key, "").strip() for key in PROFILE_FIELDS if data.get(key, "").strip()}
    if any(len(value) > 4096 or any(ord(char) < 32 for char in value) for value in profile.values()):
        raise BuildError("模板字段太长或含控制字符")
    if profile.get("flavor") not in DEFAULTS:
        raise BuildError("请选择 Tomcat 或 TongWeb")
    url = urlsplit(profile.get("baseUrl", ""))
    if url.username or url.password or url.query or "?" in url.fragment:
        raise BuildError("基础地址不能携带账号、密码或查询参数")
    profiles[name] = profile
    old_default = section.get("defaultProfile")
    if data.get("default") == "1":
        section["defaultProfile"] = name
    new_default = section.get("defaultProfile")
    if not path.parent.is_dir():
        raise BuildError("配置文件所在目录不存在")
    fd, temporary = tempfile.mkstemp(prefix=".web-profile-", suffix=".json", dir=str(path.parent))
    try:
        with fdopen_owned(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        validated = load_profile(temporary, name)
        yield name, previous or {}, profile, old_default, new_default, original, Path(temporary), validated
    finally:
        unlink_missing(temporary)


def save_profile(path, data, expected_revision):
    with file_lock(path.with_name(path.name + ".lock"), "Fast configuration"):
        with candidate_profile(path, data) as (_, _, _, _, _, original, pending, _):
            if _revision(original) != expected_revision or _config_bytes(path) != original:
                raise BuildError("配置已变更，请重新预览差异")
            os.replace(str(pending), str(path))
    return load_settings(path)[0]


def test_profile(path, data, platform):
    from image_store import _check_archive_platform
    from platforms import normalize_platform
    platform = normalize_platform(platform)
    with candidate_profile(path, data) as (name, _, _, _, _, _, _, profile):
        if profile.get("baseTar") is not None:
            _check_archive_platform(profile["baseTar"], profile["baseImage"], platform, verify_layers=True)
            return {"profile": name, "platform": platform, "result": "本地归档已验证"}
        from artifactory_download import make_opener, request
        url = profile["baseUrl"].rstrip("/") + "/"
        username = profile.get("username")
        password = os.environ.get(profile["passwordEnv"]) if username is not None else None
        if username is not None and password is None:
            raise BuildError("缺少配置指定的 Artifactory 密码环境变量")
        opener = make_opener(url, username, password, profile.get("caFile"))

        def read(name):
            with request(opener, url + name, timeout=15) as response:
                raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise BuildError("基础镜像元数据过大")
            return raw

        manifest = parse_image_json(read("manifest.json"), "Artifactory manifest")
        descriptor = manifest["config"]
        expected = digest_hex(descriptor["digest"])
        for name_candidate in ("sha256__" + expected, "sha256_" + expected, expected):
            try:
                raw = read(name_candidate)
            except HTTPError as exc:
                exc.close()
                if exc.code == 404:
                    continue
                raise
            break
        else:
            raise BuildError("Artifactory 目录中未找到镜像配置")
        if hashlib.sha256(raw).hexdigest() != expected or len(raw) != descriptor.get("size"):
            raise BuildError("Artifactory 镜像配置摘要或大小不匹配")
        config = parse_image_json(raw, "Artifactory config")
        if not isinstance(config, dict) or str(config.get("os")) + "/" + str(config.get("architecture")) != platform:
            raise BuildError("Artifactory 镜像平台与所选平台不符")
        layers, rootfs = manifest.get("layers"), config.get("rootfs")
        if (not isinstance(layers, list) or not isinstance(rootfs, dict) or rootfs.get("type") != "layers" or
                not isinstance(rootfs.get("diff_ids"), list) or len(rootfs["diff_ids"]) != len(layers)):
            raise BuildError("Artifactory 镜像层和配置不一致")
        for layer, diff_id in zip(layers, rootfs["diff_ids"]):
            digest_hex(layer["digest"])
            digest_hex(diff_id)
        return {"profile": name, "platform": platform, "result": "远端 manifest/config 可读，未下载镜像层"}


def cache_root(settings):
    return Path(cache_directory(settings, "layerCache", Path(tempfile.gettempdir()) /
                                "pyimagebuilder-layer-cache")).resolve()


def cache_plan(root, days):
    report = prune_old_cache(root, days, dry_run=True)
    rows = [(Path(path), "过期条目") for path in report["expired_entries"]]
    for key, reason in (("invalid_entries", "无效条目"), ("orphan_layers", "无引用层")):
        rows.extend((Path(item["path"]), reason) for item in report["cleanup"][key])
    plan = []
    for path, reason in rows:
        if path.parent not in (root / "entries", root / "layers"):
            raise BuildError("清理路径超出构建缓存")
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode):
            raise BuildError("候选项不是普通文件：" + str(path))
        from cache_identity import file_identity
        plan.append((str(path), reason, meta.st_size, file_identity(path)))
    return plan


def cache_fingerprint(plan):
    raw = json.dumps(plan, ensure_ascii=True, sort_keys=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def clean_cache(root, days, expected):
    with cache_activity(root, maintenance=True):
        plan = cache_plan(root, days)
        if cache_fingerprint(plan) != expected:
            raise BuildError("候选缓存已变化，未删除任何文件，请重新预览")
        for filename, _, _, _ in plan:
            Path(filename).unlink()
    return {"deleted": len(plan), "bytes": sum(row[2] for row in plan), "cache": str(root)}
