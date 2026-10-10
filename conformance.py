#!/usr/bin/env python3
"""使用独立读取器验证离线 OCI/Docker 样例，并可与真实 Docker 构建对照。"""

import argparse
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from builder import build
from errors import ArchiveError, BuildError


BUFFER = 4 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024


def _read_json(archive, name):
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise ArchiveError("Missing archive member: " + name) from exc
    if not member.isfile() or member.size > MAX_JSON:
        raise ArchiveError("Invalid or oversized archive JSON: " + name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveError("Unreadable archive member: " + name)
    with stream:
        try:
            return json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise ArchiveError("Invalid archive JSON: " + name) from exc


def _sha(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(BUFFER), b""):
        value.update(block)
    return value.hexdigest()


def _path(name):
    value = name
    while value.startswith("./"):
        value = value[2:]
    if not value or value == ".":
        return ""
    if value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise ArchiveError("Unsafe layer path: " + name)
    return posixpath.normpath(value).rstrip("/")


def _blob(archive, descriptor, temp_path):
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str):
        raise ArchiveError("Invalid OCI descriptor")
    digest = descriptor["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ArchiveError("Unsupported OCI digest")
    name = "blobs/sha256/" + digest[7:]
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise ArchiveError("Missing OCI blob: " + name) from exc
    if not member.isfile() or member.size != descriptor.get("size"):
        raise ArchiveError("OCI blob size mismatch: " + name)
    source = archive.extractfile(member)
    if source is None:
        raise ArchiveError("Unreadable OCI blob: " + name)
    with source, open(temp_path, "wb") as output:
        shutil.copyfileobj(source, output, BUFFER)
    if _sha_file(temp_path) != digest[7:]:
        raise ArchiveError("OCI blob digest mismatch: " + name)


def _sha_file(path):
    with open(path, "rb") as stream:
        return _sha(stream)


def _image_parts(path, tag, workspace):
    """独立读取归档成员，避免兼容性验证沿用被测构建器的镜像读取逻辑。"""
    with tarfile.open(path, "r:*") as archive:
        names = [member.name for member in archive]
        if len(names) != len(set(names)):
            raise ArchiveError("Duplicate outer archive member")
        if "manifest.json" in names:
            manifest = _read_json(archive, "manifest.json")
            if not isinstance(manifest, list):
                raise ArchiveError("Invalid Docker save manifest")
            choices = [item for item in manifest if isinstance(item, dict) and
                       tag in (item.get("RepoTags") or [])]
            if len(choices) != 1:
                raise ArchiveError("Docker save tag must identify one image")
            item = choices[0]
            config = _read_json(archive, item["Config"])
            config_name = item["Config"]
            if config_name.endswith(".json") and len(config_name) == 69:
                source = archive.extractfile(config_name)
                if source is None:
                    raise ArchiveError("Missing Docker config")
                with source:
                    if _sha(source) + ".json" != config_name:
                        raise ArchiveError("Docker config digest mismatch")
            if (not isinstance(config, dict) or
                    (config.get("config") is not None and not isinstance(config["config"], dict)) or
                    not isinstance(config.get("rootfs"), dict) or
                    config["rootfs"].get("type") != "layers"):
                raise ArchiveError("Docker rootfs.type must be layers")
            diff_ids = config["rootfs"].get("diff_ids")
            names = item.get("Layers")
            if not isinstance(names, list) or not isinstance(diff_ids, list) or len(names) != len(diff_ids):
                raise ArchiveError("Docker layer count mismatch")
            layers = []
            for number, (name, diff_id) in enumerate(zip(names, diff_ids)):
                source = archive.extractfile(name)
                if source is None:
                    raise ArchiveError("Missing Docker layer: " + name)
                target = workspace / ("layer-{}.tar".format(number))
                with source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output, BUFFER)
                if diff_id != "sha256:" + _sha_file(target):
                    raise ArchiveError("Docker layer diff ID mismatch")
                layers.append(target)
            return config, layers
        if "index.json" in names:
            if _read_json(archive, "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
                raise ArchiveError("Invalid OCI layout marker")
            index = _read_json(archive, "index.json")
            entries = index.get("manifests") if isinstance(index, dict) else None
            if (not isinstance(index, dict) or index.get("schemaVersion") != 2 or
                    index.get("mediaType") != "application/vnd.oci.image.index.v1+json" or
                    not isinstance(entries, list) or len(entries) != 1):
                raise ArchiveError("Conformance supports one OCI platform per archive")
            entry = entries[0]
            if (not isinstance(entry, dict) or
                    entry.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"):
                raise ArchiveError("Invalid OCI manifest descriptor")
            if entry.get("annotations", {}).get("org.opencontainers.image.ref.name") != tag:
                raise ArchiveError("OCI tag mismatch")
            manifest_path = workspace / "manifest.json"
            _blob(archive, entry, manifest_path)
            try:
                manifest = json.loads(manifest_path.read_bytes())
            except (ValueError, UnicodeError) as exc:
                raise ArchiveError("Invalid OCI manifest JSON") from exc
            if (not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict) or
                    manifest.get("schemaVersion") != 2 or
                    manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json" or
                    manifest["config"].get("mediaType") !=
                    "application/vnd.oci.image.config.v1+json"):
                raise ArchiveError("Invalid OCI image manifest")
            config_path = workspace / "config.json"
            _blob(archive, manifest["config"], config_path)
            try:
                config = json.loads(config_path.read_bytes())
            except (ValueError, UnicodeError) as exc:
                raise ArchiveError("Invalid OCI config JSON") from exc
            if (not isinstance(config, dict) or
                    (config.get("config") is not None and not isinstance(config["config"], dict)) or
                    not isinstance(config.get("rootfs"), dict) or
                    config["rootfs"].get("type") != "layers" or
                    entry.get("platform") != {"os": config.get("os"),
                                               "architecture": config.get("architecture")}):
                raise ArchiveError("OCI config platform or rootfs mismatch")
            diff_ids = config["rootfs"].get("diff_ids")
            if (not isinstance(manifest.get("layers"), list) or not isinstance(diff_ids, list) or
                    len(manifest["layers"]) != len(diff_ids)):
                raise ArchiveError("OCI layer count mismatch")
            layers = []
            for number, (descriptor, diff_id) in enumerate(zip(manifest["layers"], diff_ids)):
                if descriptor.get("mediaType") != "application/vnd.oci.image.layer.v1.tar":
                    raise ArchiveError("Conformance expects uncompressed OCI layers")
                target = workspace / ("layer-{}.tar".format(number))
                _blob(archive, descriptor, target)
                if descriptor["digest"] != diff_id:
                    raise ArchiveError("OCI uncompressed layer differs from diff ID")
                layers.append(target)
            return config, layers
    raise ArchiveError("Expected Docker save or OCI image layout tar")


def _drop(tree, name, children=True):
    for key in list(tree):
        if key == name or (children and key.startswith(name + "/")):
            del tree[key]


def _parents(tree, name):
    parent = posixpath.dirname(name)
    while parent:
        if parent in tree and tree[parent]["type"] != "dir":
            raise ArchiveError("Layer member has a non-directory parent: " + name)
        tree.setdefault(parent, {"type": "dir", "implicit": True})
        parent = posixpath.dirname(parent)


def _layer_tree(layers):
    """独立应用 OCI whiteout，不调用 RootFSIndex 或 RootFSMaterializer。"""
    tree = {}
    for layer in layers:
        with tarfile.open(layer, "r:*") as archive:
            members = []
            seen = set()
            for member in archive:
                name = _path(member.name)
                if not name:
                    continue
                if name in seen:
                    raise ArchiveError("Duplicate path within layer: " + name)
                seen.add(name)
                members.append((name, member))
            # 独立检查整层的祖先类型，避免复制被测索引的顺序依赖错误。
            declared = {name: member for name, member in members
                        if not posixpath.basename(name).startswith(".wh.")}
            for name, member in members:
                base = posixpath.basename(name)
                if base.startswith(".wh."):
                    if not member.isfile() or member.size:
                        raise ArchiveError("Invalid OCI whiteout")
                    if base != ".wh..wh..opq" and base[4:] in ("", ".", ".."):
                        raise ArchiveError("Invalid OCI whiteout target")
                parent = posixpath.dirname(name)
                while parent:
                    if posixpath.basename(parent).startswith(".wh."):
                        raise ArchiveError("Layer member has a whiteout parent: " + name)
                    ancestor = declared.get(parent)
                    if not base.startswith(".wh.") and ancestor is not None and not ancestor.isdir():
                        raise ArchiveError("Layer member has a non-directory parent: " + name)
                    parent = posixpath.dirname(parent)
            for name, member in members:
                base = posixpath.basename(name)
                parent = posixpath.dirname(name)
                if not base.startswith(".wh."):
                    continue
                if base == ".wh..wh..opq":
                    for key in list(tree):
                        if not parent or key.startswith(parent + "/"):
                            del tree[key]
                else:
                    _drop(tree, posixpath.join(parent, base[4:]))
            hardlinks = []
            for name, member in members:
                if posixpath.basename(name).startswith(".wh."):
                    continue
                _parents(tree, name)
                if member.isdir():
                    previous = tree.get(name)
                    if previous and previous["type"] != "dir":
                        _drop(tree, name)
                    kind = "dir"
                elif member.isfile():
                    _drop(tree, name)
                    kind = "file"
                elif member.issym():
                    _drop(tree, name)
                    kind = "symlink"
                elif member.islnk():
                    _drop(tree, name)
                    kind = "hardlink"
                else:
                    _drop(tree, name)
                    kind = "special"
                data = {"type": kind, "mode": member.mode & 0o7777,
                        "uid": member.uid, "gid": member.gid}
                xattrs = {key[len("SCHILY.xattr."):]: value
                          for key, value in member.pax_headers.items()
                          if key.startswith("SCHILY.xattr.")}
                if xattrs:
                    data["xattrs"] = xattrs
                if kind == "file":
                    # 每次写普通文件都是新 inode；内容摘要相同不代表与旧别名共享权限。
                    data["_inode"] = object()
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable regular file: " + name)
                    with stream:
                        data["sha256"] = _sha(stream)
                    data["size"] = member.size
                elif kind in ("symlink", "hardlink"):
                    data["target"] = member.linkname
                    if kind == "hardlink":
                        hardlinks.append(name)
                elif kind == "special":
                    data["devmajor"] = member.devmajor
                    data["devminor"] = member.devminor
                tree[name] = data
            # 独立验收按依赖扫描，而不调用生产索引的解析器。完成链接时，其头的权限
            # 作用于所有仍可见的同 inode 名字；字节摘要固定到当时目标，不再追踪路径。
            pending = dict.fromkeys(hardlinks)
            ready = [name for name in hardlinks
                     if _path(tree[name]["target"].lstrip("/")) not in pending]
            while pending:
                if not ready:
                    raise ArchiveError("Cyclic hardlink in layer: " + next(iter(pending)))
                batch, ready = ready, []
                for name in batch:
                    data = tree[name]
                    target = _path(data["target"].lstrip("/"))
                    linked = tree.get(target)
                    if linked is None:
                        raise ArchiveError("Hardlink target missing in layer: " + name)
                    if linked["type"] not in ("file", "hardlink"):
                        raise ArchiveError("Hardlink target is not a file: " + name)
                    data["sha256"], data["size"] = linked["sha256"], linked["size"]
                    data["_inode"] = linked["_inode"]
                    attrs = dict(linked.get("xattrs", {}))
                    attrs.pop("security.capability", None)
                    attrs.update(data.get("xattrs", {}))
                    for alias in tree.values():
                        if alias.get("_inode") is data["_inode"]:
                            alias.update({key: data[key] for key in ("uid", "gid", "mode")})
                            if attrs:
                                alias["xattrs"] = dict(attrs)
                            else:
                                alias.pop("xattrs", None)
                    del pending[name]
                    ready.extend(child for child in pending
                                 if _path(tree[child]["target"].lstrip("/")) == name)
    for data in tree.values():
        data.pop("_inode", None)
    return tree


def _runtime(config):
    runtime = config.get("config") or {}
    env = {}
    for item in runtime.get("Env") or []:
        key, separator, value = item.partition("=")
        if separator:
            env[key] = value
    return {"architecture": config.get("architecture"), "os": config.get("os"),
            "env": env, "workdir": runtime.get("WorkingDir") or "",
            "user": runtime.get("User") or "", "cmd": runtime.get("Cmd"),
            "entrypoint": runtime.get("Entrypoint"), "shell": runtime.get("Shell"),
            "healthcheck": runtime.get("Healthcheck"),
            "stop_signal": runtime.get("StopSignal") or "",
            "onbuild": runtime.get("OnBuild") or [],
            "labels": runtime.get("Labels") or {},
            "ports": sorted((runtime.get("ExposedPorts") or {}).keys()),
            "volumes": sorted((runtime.get("Volumes") or {}).keys())}


def snapshot(path, tag):
    """通过独立读取器提取归档最终可见的文件树和运行配置。"""
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-conformance-read-") as temp:
        config, layers = _image_parts(path, tag, Path(temp))
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        history = config.get("history")
        # 保持独立于生产读取器的校验，避免兼容性测试重复接受被测逻辑的错误。
        if history is None:
            history = []
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise ArchiveError("Invalid config history array")
        if any(item.get("empty_layer") is not None and type(item["empty_layer"]) is not bool
               for item in history):
            raise ArchiveError("Invalid config history empty_layer type")
        if not isinstance(diff_ids, list) or (history and
                sum(not item.get("empty_layer", False) for item in history) != len(diff_ids)):
            raise ArchiveError("Config history and diff IDs do not align")
        return {"runtime": _runtime(config), "tree": _layer_tree(layers),
                "layer_count": len(layers)}


def _normalize_tree(tree):
    """比较运行语义时忽略 tar 时间戳和隐式父目录元数据。"""
    normalized = {}
    for path, data in tree.items():
        if data.get("type") == "dir" and data.get("implicit"):
            normalized[path] = {"type": "dir", "mode": 0o755, "uid": 0, "gid": 0}
        elif data.get("type") == "dir":
            normalized[path] = {key: data[key] for key in ("type", "mode", "uid", "gid")}
        elif data.get("type") == "symlink":
            normalized[path] = {key: data[key] for key in ("type", "target", "uid", "gid")}
            if "xattrs" in data:
                normalized[path]["xattrs"] = data["xattrs"]
        else:
            normalized[path] = data
    return normalized


def compare_snapshots(actual, expected):
    """规范化仅影响归档表示的细节后，报告两个镜像快照的语义差异。"""
    problems = []
    for key in sorted(set(actual["runtime"]) | set(expected["runtime"])):
        if actual["runtime"].get(key) != expected["runtime"].get(key):
            problems.append({"field": "config." + key,
                             "actual": actual["runtime"].get(key),
                             "expected": expected["runtime"].get(key)})
    left = _normalize_tree(actual["tree"])
    right = _normalize_tree(expected["tree"])
    for path in sorted(set(left) | set(right)):
        if left.get(path) != right.get(path):
            problems.append({"field": "rootfs/" + path,
                             "actual": left.get(path), "expected": right.get(path)})
    return problems


def _bundle(path):
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("nested/hello.txt")
        content = b"hello from tar\n"
        info.size = len(content)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))


def _fixtures(root):
    cases = []
    for name, dockerfile, files in (
        ("copy_metadata", "FROM scratch\nARG DEST=/opt/app\nENV APP_MODE=prod\n"
         "WORKDIR ${DEST}\nCOPY --chown=1001:1002 --chmod=0640 payload.txt /opt/app/data.txt\n"
         "COPY --chown=1001:1002 --chmod=0750 payload.txt /created/nested/data.txt\n"
         "LABEL org.example.case=copy\nEXPOSE 8080/tcp\nUSER 1001:1002\n"
         "CMD [\"/opt/app/data.txt\"]\n", {"payload.txt": b"phase15 payload\n"}),
        ("add_multistage", "FROM scratch AS assets\nADD bundle.tar /bundle/\n"
         "FROM scratch\nCOPY --from=assets /bundle/nested/hello.txt /srv/hello.txt\n"
         "ENTRYPOINT [\"/srv/hello.txt\"]\n", {}),
        ("overwrite", "FROM scratch\nCOPY old.txt /etc/item\n"
         "COPY new.txt /etc/item\n", {"old.txt": b"old", "new.txt": b"new"}),
        ("config_volume", "FROM scratch\nVOLUME [\"/var/data\"]\n"
         "SHELL [\"/bin/sh\", \"-ec\"]\nCMD echo ready\n", {}),
    ):
        context = root / name
        context.mkdir()
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        for filename, content in files.items():
            (context / filename).write_bytes(content)
        if name == "add_multistage":
            _bundle(context / "bundle.tar")
        cases.append((name, context))
    return cases


def _expect(name, current):
    tree = current["tree"]
    runtime = current["runtime"]
    errors = []
    def file_at(path, content, mode=None, uid=None, gid=None):
        item = tree.get(path)
        if item is None or item.get("type") != "file" or item.get("sha256") != hashlib.sha256(content).hexdigest():
            errors.append("Expected file content at /" + path)
        elif any(expected is not None and item.get(field) != expected
                 for field, expected in (("mode", mode), ("uid", uid), ("gid", gid))):
            errors.append("Wrong file metadata at /" + path)
    if name == "copy_metadata":
        file_at("opt/app/data.txt", b"phase15 payload\n", 0o640, 1001, 1002)
        file_at("created/nested/data.txt", b"phase15 payload\n", 0o750, 1001, 1002)
        # 使用独立读取器检查自动创建的目录，不能只核对最终文件。
        for path in ("created", "created/nested"):
            item = tree.get(path, {})
            if (item.get("type"), item.get("mode"), item.get("uid"), item.get("gid")) != \
                    ("dir", 0o750, 1001, 1002):
                errors.append("Wrong created directory metadata at /" + path)
        if runtime["env"].get("APP_MODE") != "prod" or runtime["workdir"] != "/opt/app":
            errors.append("ENV/WORKDIR configuration differs from expected")
        if runtime["user"] != "1001:1002" or runtime["cmd"] != ["/opt/app/data.txt"]:
            errors.append("USER/CMD configuration differs from expected")
        if runtime["ports"] != ["8080/tcp"] or runtime["labels"].get("org.example.case") != "copy":
            errors.append("EXPOSE/LABEL configuration differs from expected")
    elif name == "add_multistage":
        file_at("srv/hello.txt", b"hello from tar\n")
        if "bundle/nested/hello.txt" in tree:
            errors.append("Intermediate stage file leaked into final image")
        if runtime["entrypoint"] != ["/srv/hello.txt"]:
            errors.append("ENTRYPOINT differs from expected")
    elif name == "overwrite":
        file_at("etc/item", b"new")
        if current["layer_count"] != 2:
            errors.append("Expected two filesystem layers")
    elif name == "config_volume":
        if (runtime["volumes"] != ["/var/data"] or runtime["shell"] != ["/bin/sh", "-ec"] or
                runtime["cmd"] != ["/bin/sh", "-ec", "echo ready"]):
            errors.append("VOLUME/SHELL/CMD configuration differs from expected")
        if tree.get("var/data", {}).get("type") != "dir":
            errors.append("VOLUME target directory is missing")
    return errors


def _whiteout_fixture(root):
    def layer(path, entries):
        with tarfile.open(path, "w") as archive:
            for name, content in entries:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    first = root / "whiteout-base.tar"
    second = root / "whiteout-change.tar"
    digests = [layer(first, [("etc/old", b"remove"), ("var/a", b"remove")]),
               layer(second, [("etc/.wh.old", b""), ("var/.wh..wh..opq", b""),
                              ("var/b", b"keep")])]
    config = {"architecture": "amd64", "os": "linux", "config": {},
              "rootfs": {"type": "layers", "diff_ids": digests},
              "history": [{"created_by": "base"}, {"created_by": "change"}]}
    tag = "pyimagebuilder/conformance:whiteout"
    docker = root / "whiteout-docker.tar"
    oci = root / "whiteout-oci.tar"
    from image_writer import ImageArchiveWriter
    from oci_writer import OCIImageWriter
    ImageArchiveWriter().write_new(docker, config, [first, second], tag)
    OCIImageWriter().write_new(oci, config, [first, second], tag)
    snapshots = [snapshot(item, tag) for item in (docker, oci)]
    errors = []
    for item in snapshots:
        if "etc/old" in item["tree"] or "var/a" in item["tree"]:
            errors.append("OCI whiteout/opaque directory did not hide lower files")
        if item["tree"].get("var/b", {}).get("sha256") != hashlib.sha256(b"keep").hexdigest():
            errors.append("OCI opaque directory hid its own same-layer addition")
    errors.extend("Docker/OCI: " + str(item) for item in compare_snapshots(*snapshots))
    return {"case": "whiteout", "passed": not errors, "errors": errors}


def _link_fixture(root):
    layer = root / "links-layer.tar"
    with tarfile.open(layer, "w") as archive:
        info = tarfile.TarInfo("bin/app")
        info.size = 3
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(b"app"))
        soft = tarfile.TarInfo("bin/current")
        soft.type = tarfile.SYMTYPE
        soft.linkname = "app"
        archive.addfile(soft)
        hard = tarfile.TarInfo("bin/alias")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "bin/app"
        archive.addfile(hard)
    digest = "sha256:" + _sha_file(layer)
    config = {"architecture": "amd64", "os": "linux", "config": {},
              "rootfs": {"type": "layers", "diff_ids": [digest]},
              "history": [{"created_by": "links"}]}
    from image_writer import ImageArchiveWriter
    output = root / "links-image.tar"
    ImageArchiveWriter().write_new(output, config, [layer], "pyimagebuilder/conformance:links")
    tree = snapshot(output, "pyimagebuilder/conformance:links")["tree"]
    errors = []
    if tree.get("bin/current", {}).get("target") != "app":
        errors.append("Symbolic link target differs")
    if (tree.get("bin/alias", {}).get("sha256") != hashlib.sha256(b"app").hexdigest() or
            tree.get("bin/alias", {}).get("type") != "hardlink"):
        errors.append("Hardlink content or type differs")
    return {"case": "links", "passed": not errors, "errors": errors}


def _cache_fixture(root):
    context = root / "cache-fixture"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY input.txt /data/input.txt\n", encoding="utf-8")
    source = context / "input.txt"
    source.write_bytes(b"old")
    tag = "pyimagebuilder/conformance:cache"
    cache_dir = root / "fixture-layer-cache"
    outputs = [root / ("cache-{}.tar".format(index)) for index in range(3)]
    stats = {}
    build(dockerfile, context, None, None, tag, outputs[0], cache_dir=cache_dir,
          cache_stats=stats)
    first_stats = dict(stats)
    build(dockerfile, context, None, None, tag, outputs[1], cache_dir=cache_dir,
          cache_stats=stats)
    second_stats = dict(stats)
    stat = source.stat()
    source.write_bytes(b"new")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    build(dockerfile, context, None, None, tag, outputs[2], cache_dir=cache_dir,
          cache_stats=stats)
    third_stats = dict(stats)
    errors = []
    if _sha_file(outputs[0]) != _sha_file(outputs[1]):
        errors.append("Cache hit changed archive bytes")
    if _sha_file(outputs[0]) == _sha_file(outputs[2]):
        errors.append("Changed file content did not invalidate the cache")
    if not second_stats["hits"] or not third_stats["misses"] or not first_stats["misses"]:
        errors.append("Expected cache hit/miss transitions were absent")
    if snapshot(outputs[2], tag)["tree"].get("data/input.txt", {}).get("sha256") != hashlib.sha256(b"new").hexdigest():
        errors.append("Rebuilt image still contains old input bytes")
    return {"case": "cache_invalidation", "passed": not errors, "errors": errors}


def _run(command, timeout=300):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout)
    if result.returncode:
        raise BuildError("Command failed ({}): {}".format(result.returncode,
            (result.stderr or result.stdout).strip()[-2000:]))
    return result.stdout


def _docker_compare(docker, context, own_archive, own_tag, reference_tag, root, platform):
    containers = []
    try:
        _run([docker, "build", "--pull=false", "--network=none", "--platform", platform,
              "--file", str(context / "Dockerfile"), "--tag", reference_tag, str(context)])
        reference = root / (reference_tag.rsplit(":", 1)[-1] + ".tar")
        _run([docker, "save", "--output", str(reference), reference_tag])
        differences = compare_snapshots(snapshot(own_archive, own_tag),
                                        snapshot(reference, reference_tag))
        _run([docker, "load", "--input", str(own_archive)])
        inspected = json.loads(_run([docker, "image", "inspect", own_tag]))
        if not inspected or not isinstance(inspected[0].get("Config"), dict):
            raise BuildError("Docker could not inspect loaded image")
        config = {"architecture": inspected[0].get("Architecture"),
                  "os": inspected[0].get("Os"), "config": inspected[0]["Config"]}
        expected_runtime = snapshot(own_archive, own_tag)["runtime"]
        if _runtime(config) != expected_runtime:
            differences.append({"field": "docker-load.inspect", "actual": _runtime(config),
                                "expected": expected_runtime})
        exports = []
        for label, image_tag in (("own", own_tag), ("reference", reference_tag)):
            name = "pyimagebuilder-conformance-{}-{}".format(label, uuid.uuid4().hex[:12])
            _run([docker, "create", "--name", name, image_tag])
            containers.append(name)
            path = root / (label + "-export.tar")
            _run([docker, "export", "--output", str(path), name])
            exports.append(path)
        exported = [_layer_tree([item]) for item in exports]
        actual_export, reference_export = map(_normalize_tree, exported)
        differences.extend({"field": "docker-export/" + path,
                            "actual": actual_export.get(path),
                            "expected": reference_export.get(path)}
                           for path in sorted(set(actual_export) | set(reference_export))
                           if actual_export.get(path) != reference_export.get(path))
        return differences
    finally:
        for name in containers:
            subprocess.run([docker, "rm", "-v", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        for name in (own_tag, reference_tag):
            subprocess.run([docker, "image", "rm", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)


def suite(reference=False, docker="docker", platform="linux/amd64"):
    """运行离线语义样例；可选择用相同构建输入与真实 Docker 对照。"""
    if reference and shutil.which(docker) is None:
        raise BuildError("Docker reference mode requires a working Docker CLI and daemon")
    results = []
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-conformance-") as temp:
        root = Path(temp)
        for name, context in _fixtures(root):
            token = uuid.uuid4().hex[:12]
            own_tag = "pyimagebuilder/conformance-{}:{}".format(name, token)
            reference_tag = "pyimagebuilder/reference-{}:{}".format(name, token)
            archive = root / (name + ".tar")
            try:
                build(context / "Dockerfile", context, None, None, own_tag, archive,
                      target_platform=platform, image_format="both",
                      oci_output=root / (name + ".oci.tar"))
                own = snapshot(archive, own_tag)
                differences = _expect(name, own)
                differences.extend("Docker/OCI: " + str(item) for item in
                    compare_snapshots(own, snapshot(root / (name + ".oci.tar"), own_tag)))
                if reference:
                    differences.extend(_docker_compare(docker, context, archive, own_tag,
                                                      reference_tag, root, platform))
                results.append({"case": name, "passed": not differences,
                                "errors": differences[:30]})
            except (BuildError, ArchiveError, OSError, tarfile.TarError,
                    subprocess.TimeoutExpired) as exc:
                results.append({"case": name, "passed": False, "errors": [str(exc)]})
        try:
            results.append(_whiteout_fixture(root))
        except (BuildError, ArchiveError, OSError, tarfile.TarError) as exc:
            results.append({"case": "whiteout", "passed": False, "errors": [str(exc)]})
        for name, fixture in (("links", _link_fixture),
                              ("cache_invalidation", _cache_fixture)):
            try:
                results.append(fixture(root))
            except (BuildError, ArchiveError, OSError, tarfile.TarError) as exc:
                results.append({"case": name, "passed": False, "errors": [str(exc)]})
    return {"mode": "docker-reference" if reference else "offline",
            "docker_reference_executed": reference,
            "passed": all(item["passed"] for item in results), "cases": results}


def compare_project(archive, tag, dockerfile, context, docker="docker", platform="linux/amd64",
                    build_args=(), secrets=(), network="none"):
    """将用户归档与相同 Dockerfile/上下文的 Docker 构建结果比较。"""
    if shutil.which(docker) is None:
        raise BuildError("Project comparison requires a working Docker CLI and daemon")
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-reference-") as temp:
        root = Path(temp)
        reference_tag = "pyimagebuilder/project-reference:" + uuid.uuid4().hex[:12]
        try:
            command = [docker, "build", "--pull=false", "--network=" + network,
                       "--platform", platform, "--file", str(dockerfile),
                       "--tag", reference_tag]
            for item in build_args:
                command += ["--build-arg", item]
            for item in secrets:
                identifier, separator, source = item.partition("=")
                if not separator or not identifier or not Path(source).is_file():
                    raise BuildError("--secret must be ID=/existing/file")
                command += ["--secret", "id={},src={}".format(identifier, source)]
            command.append(str(context))
            _run(command)
            destination = root / "reference.tar"
            _run([docker, "save", "--output", str(destination), reference_tag])
            problems = compare_snapshots(snapshot(archive, tag), snapshot(destination, reference_tag))
            return {"mode": "project-reference", "docker_reference_executed": True,
                    "passed": not problems, "differences": problems[:100]}
        finally:
            subprocess.run([docker, "image", "rm", reference_tag],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def linux_suite(base_tar, reference, workspace="/tmp", sandbox="hardened"):
    """使用调用方提供的基础镜像测试真实 Linux RUN、挂载与 whiteout。"""
    if platform.system() != "Linux":
        raise BuildError("Linux RUN conformance must execute inside Linux/WSL")
    results = []
    root = Path(__file__).resolve().parent
    for name, script in (("linux_run", "linux_run_smoke.py"),
                         ("linux_cache_secret_mount", "linux_phase13_smoke.py")):
        command = [sys.executable, str(root / "tests" / script),
                   "--base-tar", str(base_tar), "--from", reference,
                   "--workspace", str(workspace), "--sandbox", sandbox]
        try:
            output = _run(command, timeout=600)
            results.append({"case": name, "passed": True, "output": output.strip()})
        except (BuildError, subprocess.TimeoutExpired) as exc:
            results.append({"case": name, "passed": False, "errors": [str(exc)]})
    return {"mode": "linux-run", "docker_reference_executed": False,
            "passed": all(item["passed"] for item in results), "cases": results}


def main(argv=None):
    """独立脚本入口：离线样例、Linux 执行、Docker 对照或项目结果比较。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("offline", help="Run independent local semantics fixtures")
    offline.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                         default="linux/amd64")
    offline.add_argument("--report", type=Path)
    reference = commands.add_parser("reference", help="Compare fixtures with Docker build/load")
    reference.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                           default="linux/amd64")
    reference.add_argument("--docker", default="docker")
    reference.add_argument("--report", type=Path)
    project = commands.add_parser("compare", help="Compare an existing archive with Docker build")
    project.add_argument("--archive", type=Path, required=True)
    project.add_argument("--tag", required=True)
    project.add_argument("--dockerfile", type=Path, required=True)
    project.add_argument("--context", type=Path, required=True)
    project.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                         default="linux/amd64")
    project.add_argument("--docker", default="docker")
    project.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE")
    project.add_argument("--secret", action="append", default=[], metavar="ID=/file")
    project.add_argument("--docker-network", choices=("none", "default", "host"),
                         default="none")
    project.add_argument("--report", type=Path)
    linux = commands.add_parser("linux", help="Run real namespace/OverlayFS and mount smoke tests")
    linux.add_argument("--base-tar", type=Path, required=True)
    linux.add_argument("--from", dest="reference", required=True)
    linux.add_argument("--workspace", default="/tmp")
    linux.add_argument("--sandbox", choices=("legacy", "hardened", "rootless"),
                       default="hardened")
    linux.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "linux":
            result = linux_suite(args.base_tar, args.reference, args.workspace, args.sandbox)
        elif args.command == "compare":
            result = compare_project(args.archive, args.tag, args.dockerfile, args.context,
                                     args.docker, args.platform, args.build_arg, args.secret,
                                     args.docker_network)
        else:
            result = suite(args.command == "reference", getattr(args, "docker", "docker"),
                           args.platform)
        raw = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.report:
            if args.report.exists():
                raise BuildError("Report already exists: " + str(args.report))
            args.report.write_text(raw, encoding="utf-8")
        print(raw, end="")
        return 0 if result["passed"] else 1
    except (ArchiveError, BuildError, OSError, tarfile.TarError,
            subprocess.TimeoutExpired) as exc:
        parser.exit(2, "Conformance error: {}\n".format(exc))


if __name__ == "__main__":
    sys.exit(main())
