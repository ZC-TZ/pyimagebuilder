"""统一镜像引用解析、本地 CAS/tar 复用及 Registry/Artifactory 拉取。"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from artifactory_download import (artifact_directory_url, download_and_pack,
                                  image_reference_from_directory, list_files, make_opener)
from cas_store import CASStore, docker_archive_tag
from compat import unlink_missing
from build_args import expand
from dockerfile_parser import parse
from errors import BuildError
from fast import _require_base_tag
from image_reader import (MAX_JSON, digest_hex, docker_manifest, outer_name, parse_image_json,
                          read_image_json_file, same_json_value, verify_layer_tar)
from platforms import architecture, host_architecture, normalize_platform
from registry import pull as registry_pull
from settings import cache_directory, load_settings, repository_settings


def default_store():
    """返回未配置 imageStore 时使用的临时目录下镜像库。"""
    return Path(tempfile.gettempdir()) / "pyimagebuilder-image-store"


def _archive_path(store, reference, platform, source):
    key = hashlib.sha256((source + "\0" + reference + "\0" + platform).encode("utf-8")).hexdigest()
    return Path(store).resolve() / (key + ".tar")


def _cas_source(store, reference, platform, source):
    return {"type": "cas", "store": str(Path(store).resolve()),
            "reference": reference, "platform": platform, "source": source}


def _check_archive_platform(path, reference, platform, verify_layers=False, with_config_digest=False):
    """选取唯一平台并检查配置；需要时返回原始配置摘要供转存身份核对。"""
    expected_tag = docker_archive_tag(reference)
    try:
        with tarfile.open(path, "r:*") as archive:
            members = {}
            for member in archive:
                name = outer_name(member.name)
                if not name:
                    continue
                if name in members:
                    raise BuildError("Duplicate cached archive member: " + name)
                members[name] = member
            # 与完整读取器使用同一规范化规则，拒绝重复成员和 ./ 路径别名。
            # getmember 只取最后一个同名条目，不能用它代替整份归档的重复检查。
            manifest_member = members["manifest.json"]
            if not manifest_member.isfile() or manifest_member.size > MAX_JSON:
                raise BuildError("Missing or oversized pulled manifest.json")
            with archive.extractfile(manifest_member) as stream:
                manifest = docker_manifest(json.load(stream))
            matches = []
            for item in manifest:
                if expected_tag in (item.get("RepoTags") or []):
                    config_member = members[outer_name(item["Config"])]
                    if not config_member.isfile() or config_member.size > MAX_JSON:
                        raise BuildError("Missing or oversized pulled image config")
                    with archive.extractfile(config_member) as stream:
                        raw_config = stream.read()
                    config = parse_image_json(raw_config, "cached image config")
                    if (config.get("os") == "linux" and
                            config.get("architecture") == architecture(platform)):
                        matches.append((item, config, "sha256:" + hashlib.sha256(raw_config).hexdigest()))
            if len(matches) == 1:
                item, config, config_digest = matches[0]
                if verify_layers:
                    rootfs = config.get("rootfs")
                    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
                    names = item.get("Layers")
                    if (not isinstance(rootfs, dict) or rootfs.get("type") != "layers" or
                            not isinstance(diff_ids, list) or not isinstance(names, list) or
                            len(names) != len(diff_ids)):
                        raise BuildError("Cached image config/layer count mismatch")
                    # 复用 tar 不能只检查 config；直接流式核对各层摘要，
                    # 无需再向磁盘写一份解包副本。
                    for name, diff_id in zip(names, diff_ids):
                        member = members[outer_name(name)]
                        if not member.isfile():
                            raise BuildError("Cached image layer is not a regular file")
                        hasher = hashlib.sha256()
                        with archive.extractfile(member) as stream:
                            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                                hasher.update(block)
                            if hasher.hexdigest() != digest_hex(diff_id):
                                raise BuildError("Cached image layer DiffID mismatch: " + name)
                            verify_layer_tar(stream, "cached image layer: " + name)
                return (config, config_digest) if with_config_digest else config
    except (OSError, tarfile.TarError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BuildError("Cannot inspect pulled image platform: " + str(exc)) from exc
    raise BuildError("Pulled image needs exactly one {} variant for {}".format(platform, reference))


def _check_cached_archive(cas, path, reference, platform, source):
    """核对 tar/CAS 内容；只因旧 JSON 重编码改变身份的适配 tar 可离线修复。"""
    with cas.ref_lock(reference, platform, source):
        has_ref = cas.has_ref(reference, platform, source)
        archive_config, config_digest = _check_archive_platform(
            path, reference, platform, verify_layers=has_ref, with_config_digest=True)
        if has_ref:
            ref = cas.resolve(reference, platform, source)
            stored_config, _ = read_image_json_file(cas._blob(ref["config"]), "cached CAS config")
            if not same_json_value(stored_config, archive_config):
                raise BuildError("Stored tar and CAS reference disagree for {}; remove the stale tar".format(reference))
            if config_digest != ref["config"]:
                # 字段及实际层均一致，差异仅来自旧导出器重新序列化 JSON。
                # 在同一引用锁内验证新 tar 后原子替换，失败保留旧 tar/ref。
                with tempfile.TemporaryDirectory(prefix="tar-identity-", dir=Path(path).parent) as temporary:
                    repaired = Path(temporary) / "base.tar"
                    cas.export_docker(reference, repaired, platform, source)
                    os.replace(repaired, path)


def locate_archive(store, reference, platform, source=None):
    """按引用、平台及可选来源定位镜像；只有 CAS 时按需生成 tar。"""
    root = Path(store or default_store()).resolve()
    cas = CASStore(root)
    sources = (source,) if source else ("registry", "artifactory", "local")
    matches = [(kind, _archive_path(root, reference, platform, kind)) for kind in sources
               if _archive_path(root, reference, platform, kind).is_file() or
               cas.has_ref(reference, platform, kind)]
    if not matches:
        raise BuildError("Image not present in PyImageBuilder store: {} ({})".format(reference, platform))
    if len(matches) > 1 and source is None:
        raise BuildError("Multiple stored sources for {}; specify --source".format(reference))
    kind, path = matches[0]
    with cas.ref_lock(reference, platform, kind):
        if not path.is_file():
            cas.export_docker(reference, path, platform, kind)
        _check_cached_archive(cas, path, reference, platform, kind)
    return kind, path


def pull_image(reference, *, platform="linux/amd64", source="registry", store=None,
               refresh=False, offline=False, username=None, password_env=None, ca_file=None,
               insecure_http=False, auth_host=None, workers=0, reporter=None,
               artifactory_url=None, materialize_tar=True):
    """解析或下载基础镜像，返回 (source, downloaded)。

    materialize_tar=True 时 source 是已校验的归档 Path；False 时是 builder 使用的 CAS 描述字典。
    普通构建优先使用显式导入的 local 镜像，再复用所选远端来源的缓存。
    refresh=True 跳过已有引用并刷新所选来源；offline=True 禁止进入下载路径。
    持引用锁完成复用、下载与失败恢复，已落盘但无引用的 blob 留给后续清理。
    同一引用、平台和来源被其他操作占用时立即报 busy，调用方可在其完成后重试。

    Args:
        reference: 镜像引用，作为存储检索键。
        source: registry 或 artifactory，决定远端下载方式。
        store: 镜像库目录；None 使用默认临时目录。
        materialize_tar: 是否生成或返回 Docker save tar；构建通常设为 False。

    Returns:
        二元组：基础镜像来源，以及本次是否执行了远端拉取。

    Raises:
        BuildError: 离线镜像缺失、缓存不一致、鉴权配置不完整或下载校验失败。
    """
    platform = normalize_platform(platform)
    if source not in ("registry", "artifactory"):
        raise BuildError("Pull source must be registry or artifactory")
    if artifactory_url is not None and source != "artifactory":
        raise BuildError("Artifactory URL requires source=artifactory")
    directory_url = None
    if source == "artifactory":
        raw_url = artifactory_url or (reference if "://" in reference else (
            "http://" if insecure_http else "https://") + reference)
        directory_url = artifact_directory_url(raw_url)
        if image_reference_from_directory(directory_url) != reference:
            raise BuildError("Artifactory URL resolves to a different image reference")
    store = Path(store or default_store()).resolve()
    store.mkdir(parents=True, exist_ok=True)
    cas = CASStore(store)
    # 引用锁覆盖下载与恢复全过程，否则失败者可能回滚另一个成功者的更新。
    with cas.ref_lock(reference, platform, source):
        return _pull_image_locked(cas, reference, platform, source, store, refresh, offline,
                                  username, password_env, ca_file, insecure_http, auth_host,
                                  workers, reporter, directory_url, materialize_tar)


def _pull_image_locked(cas, reference, platform, source, store, refresh, offline,
                       username, password_env, ca_file, insecure_http, auth_host,
                       workers, reporter, directory_url, materialize_tar):
    """在对应远端来源引用锁内复用镜像或下载，并在异常时恢复同一操作的快照。"""
    archive = _archive_path(store, reference, platform, source)
    archive_tag = docker_archive_tag(reference)
    if not refresh:
        # local 优先于远端缓存；复用它时也需避免与 load 的 tar/ref 更新交错。
        with cas.ref_lock(reference, platform, "local"):
            local = _archive_path(store, reference, platform, "local")
            if not materialize_tar and cas.has_ref(reference, platform, "local"):
                cas.resolve(reference, platform, "local")
                return _cas_source(store, reference, platform, "local"), False
            if not local.is_file() and cas.has_ref(reference, platform, "local"):
                cas.export_docker(reference, local, platform, "local")
            if local.is_file():
                _check_cached_archive(cas, local, reference, platform, "local")
                if not cas.has_ref(reference, platform, "local"):
                    cas.import_docker(local, reference, platform, "local")
                return (local if materialize_tar else _cas_source(store, reference, platform, "local")), False
    if not materialize_tar and not refresh and cas.has_ref(reference, platform, source):
        cas.resolve(reference, platform, source)
        return _cas_source(store, reference, platform, source), False
    if not archive.is_file() and not refresh and cas.has_ref(reference, platform, source):
        cas.export_docker(reference, archive, platform, source)
    if archive.is_file() and not refresh:
        _require_base_tag(archive, archive_tag)
        _check_cached_archive(cas, archive, reference, platform, source)
        if not cas.has_ref(reference, platform, source):
            cas.import_docker(archive, reference, platform, source)
        return (archive if materialize_tar else _cas_source(store, reference, platform, source)), False
    if offline:
        raise BuildError("Base image is absent from local image store: " + reference + " (" + platform + ")")
    if password_env is None:
        password_env = ("PYIMAGEBUILDER_ARTIFACTORY_PASSWORD" if source == "artifactory" else
                        "PYIMAGEBUILDER_REGISTRY_PASSWORD")
    password = os.environ.get(password_env)
    if (username is None) != (password is None):
        raise BuildError("Pull authentication needs --username and password environment variable together")
    previous_ref = cas.snapshot_ref(reference, platform, source)
    raw_imported = False
    native_cas_only = False
    try:
        with tempfile.TemporaryDirectory(prefix="pull-", dir=store) as temporary:
            pending = Path(temporary) / "base.tar"
            if source == "registry":
                options = {"reporter": reporter} if reporter is not None else {}
                import inspect
                parameters = inspect.signature(registry_pull).parameters
                if "reporter" not in parameters:
                    options.pop("reporter", None)
                if "workers" in parameters:
                    options["workers"] = workers
                if "cas_store" in parameters:
                    options["cas_store"] = cas
                if "cas_reference" in parameters:
                    options["cas_reference"] = reference
                native_cas_only = not materialize_tar and "cas_only" in parameters and "cas_store" in parameters
                if native_cas_only:
                    options["cas_only"] = True
                registry_pull(reference, pending, username, password, ca_file,
                              insecure_http, auth_host, "docker", None, platform, **options)
                raw_imported = "cas_store" in parameters
            else:
                opener = make_opener(directory_url, username, password, ca_file)
                import time
                listing_started = time.monotonic()
                files = list_files(opener, directory_url)
                if reporter is not None:
                    reporter.record_timing("List/auth Artifactory", time.monotonic() - listing_started)
                import inspect
                parameters = inspect.signature(download_and_pack).parameters
                options = {"reporter": reporter} if reporter is not None and "reporter" in parameters else {}
                if "cas_store" in parameters:
                    options["cas_store"] = cas
                    options["target_platform"] = platform
                native_cas_only = not materialize_tar and "cas_only" in parameters and "cas_store" in parameters
                if native_cas_only:
                    options["cas_only"] = True
                download_and_pack(opener, directory_url, files, pending, workers, reference,
                                  **options)
                raw_imported = "cas_store" in parameters
            if native_cas_only:
                cas.resolve(reference, platform, source)
            else:
                _require_base_tag(pending, archive_tag)
                _check_archive_platform(pending, reference, platform)
            # 下载器可能已把原始压缩 blob 入库并发布 ref；
            # 旧下载适配器则需从 tar 导入未压缩层，补齐 CAS 引用。
            if not raw_imported:
                cas.import_docker(pending, reference, platform, source, replace=True)
            if materialize_tar:
                os.replace(pending, archive)
            else:
                unlink_missing(archive)
    except Exception:
        cas.restore_ref(reference, platform, source, previous_ref)
        raise
    return (archive if materialize_tar else _cas_source(store, reference, platform, source)), True


def external_bases(dockerfile, target_platform, target_stage, build_args):
    """解析构建到 --target 为止会遇到的外部 FROM 引用及其平台集合。"""
    try:
        instructions = parse(Path(dockerfile).read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise BuildError("Dockerfile must be UTF-8") from exc
    headers = [item for item in instructions if item.op == "FROM"]
    if target_stage is None:
        target_index = len(headers) - 1
    elif str(target_stage).isdecimal() and int(target_stage) < len(headers):
        target_index = int(target_stage)
    else:
        matches = [index for index, item in enumerate(headers)
                   if item.value.alias and item.value.alias.lower() == str(target_stage).lower()]
        if not matches:
            raise BuildError("Unknown --target stage: " + str(target_stage))
        target_index = matches[0]
    target_platform = normalize_platform(target_platform)
    global_args = {"TARGETPLATFORM": target_platform, "TARGETOS": "linux",
                   "TARGETARCH": architecture(target_platform), "TARGETVARIANT": ""}
    host_arch = host_architecture()
    if host_arch is not None:
        global_args.update(BUILDPLATFORM="linux/" + host_arch, BUILDOS="linux",
                           BUILDARCH=host_arch, BUILDVARIANT="")
    names = set()
    external = {}
    stage_index = -1
    for instruction in instructions:
        if instruction.op == "ARG" and stage_index < 0:
            for name, separator, default in instruction.value:
                if name in build_args:
                    global_args[name] = build_args[name]
                elif separator:
                    global_args[name] = expand(default, global_args)
        elif instruction.op == "FROM":
            stage_index += 1
            if stage_index > target_index:
                break
            reference = expand(instruction.value.reference, global_args)
            platform = normalize_platform(expand(instruction.value.platform, global_args)
                                          if instruction.value.platform else target_platform)
            is_prior = reference.lower() in names or (
                reference.isdecimal() and int(reference) < stage_index)
            if not is_prior and reference.lower() != "scratch":
                external.setdefault(reference, set()).add(platform)
            if instruction.value.alias:
                names.add(instruction.value.alias.lower())
    return external


def pull_base_map(dockerfile, *, target_platform, target_stage, build_args,
                  source, store, refresh, offline, username, password_env, ca_file,
                  insecure_http, auth_host, workers, destination, settings=None, reporter=None):
    """把所需外部 FROM 解析为 CAS 描述，并写出按引用和平台组织的映射文件。"""
    references = external_bases(dockerfile, target_platform, target_stage, build_args)
    mapping = {}
    for reference, platforms in references.items():
        connection = repository_settings(settings or {}, reference)
        selected = {}
        for platform in sorted(platforms):
            if reporter is not None:
                reporter.log("Resolving {} ({})".format(reference, platform))
            archive, downloaded = pull_image(
                reference, platform=platform,
                source=source or connection.get("source", "registry"), store=store,
                refresh=refresh, offline=offline,
                username=username if username is not None else connection.get("username"),
                password_env=password_env if password_env is not None else connection.get("passwordEnv"),
                ca_file=ca_file if ca_file is not None else connection.get("caFile"),
                insecure_http=insecure_http or connection.get("insecureHttp", False),
                auth_host=auth_host if auth_host is not None else connection.get("authHost"),
                workers=workers if workers is not None else connection.get("workers", 0),
                reporter=reporter, materialize_tar=False)
            message = "{} base {} ({})".format("Pulled" if downloaded else "Cached",
                                                reference, platform)
            if reporter is None:
                print(message)
            else:
                reporter.log(message)
            selected[platform] = archive
        mapping[reference] = selected
    destination = Path(destination)
    destination.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return destination if mapping else None


def pull_main(argv=None):
    """独立脚本与 main.py 共用的 pull 入口，可额外导出 Docker tar。"""
    parser = argparse.ArgumentParser(prog="main.py pull",
                                     description="Pull a base image into the local Python image store")
    parser.add_argument("reference", help="Registry image reference, e.g. host/repository:tag")
    parser.add_argument("-o", "--output", type=Path,
                        help="Also copy the Docker save tar to this new path")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    parser.add_argument("--source", choices=("registry", "artifactory"))
    parser.add_argument("--image-store", type=Path)
    parser.add_argument("--config", type=Path, help="Shared config.json path")
    parser.add_argument("--username")
    parser.add_argument("--password-env")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--insecure-http", action="store_true")
    parser.add_argument("--auth-host")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    from progress import make_reporter
    reporter = make_reporter(args.progress, args.quiet, args.verbose, args.debug, args.no_color)
    reporter.start(args.reference, args.platform, mode="pull")
    try:
        settings, _ = load_settings(args.config)
        connection = repository_settings(settings, args.reference)
        args.image_store = args.image_store or cache_directory(settings, "imageStore", None)
        if args.output is not None:
            if args.output.suffix.lower() != ".tar":
                raise BuildError("--output must end in .tar")
            if args.output.exists():
                raise BuildError("Output already exists: " + str(args.output))
        reporter.phase_start("Pulling base image")
        archive, downloaded = pull_image(
            args.reference, platform=args.platform,
            source=args.source or connection.get("source", "registry"),
            store=args.image_store, refresh=True,
            username=args.username if args.username is not None else connection.get("username"),
            password_env=args.password_env or connection.get("passwordEnv"),
            ca_file=args.ca_file or connection.get("caFile"),
            insecure_http=args.insecure_http or connection.get("insecureHttp", False),
            auth_host=args.auth_host or connection.get("authHost"),
            workers=args.workers if args.workers is not None else connection.get("workers", 0),
            reporter=reporter)
        reporter.log("{} image from {}".format("Pulled" if downloaded else "Cached",
                                              args.source or connection.get("source", "registry")))
        reporter.phase_success("Pulling base image")
        if args.output is not None:
            output = args.output.resolve()
            if output == archive:
                raise BuildError("--output must be a distinct .tar path")
            output.parent.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                with open(archive, "rb") as source, open(output, "xb") as sink:
                    created = True
                    shutil.copyfileobj(source, sink, 4 * 1024 * 1024)
            except OSError:
                if created:
                    unlink_missing(output)
                raise
            reporter.log("Created " + str(output))
        import hashlib
        digest = hashlib.sha256()
        with open(archive, "rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        reporter.success(image=args.reference, platform=args.platform,
                         output=str(args.output.resolve() if args.output is not None else archive),
                         digest="sha256:" + digest.hexdigest(), size=archive.stat().st_size)
        return 0
    except (BuildError, OSError, RuntimeError, ValueError) as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1
