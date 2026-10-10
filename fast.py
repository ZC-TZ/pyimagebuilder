#!/usr/bin/env python3
"""根据 WAR/dist.zip 和保存的 profile 生成可 docker load 的镜像 tar。"""

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from builder import build
from errors import BuildError
from reproducible import parse_epoch
from settings import cache_directory, config_path, load_settings, repository_settings
from compat import unlink_missing


DEFAULTS = {
    "tomcat": {"owner": "tomcat:tomcat", "deployDir": "/soft/tomcat/webapps"},
    "tongweb": {"owner": "tongadmin:tonggrp", "deployDir": "/soft/TongWeb7.0/autodeploy"},
}
PROJECT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
VERSION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
MAX_ZIP_FILES = 100000
MAX_ZIP_BYTES = 4 * 1024 * 1024 * 1024


def _config_path(candidate):
    return config_path(candidate)


def _validate_path(value, field):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise BuildError(field + " must be an absolute Linux path")
    parts = PurePosixPath(value).parts
    if ".." in parts or "\n" in value or "\r" in value:
        raise BuildError(field + " contains an unsafe path")
    return value.rstrip("/")


def _validate_atom(value, field, pattern=PROJECT):
    if not isinstance(value, str) or not pattern.fullmatch(value) or value in (".", ".."):
        raise BuildError("Invalid {}: {}".format(field, value))
    return value


def _resolved_config_path(value, config_path):
    path = Path(value)
    return (path if path.is_absolute() else config_path.parent / path).resolve()


def load_profile(config_path, profile_name=None):
    """解析并校验 Fast profile，包括基础镜像来源、部署目录和可选 TongWeb XML。"""
    config_path = Path(config_path).resolve()
    settings, _ = load_settings(config_path, required=True)
    section = settings["fast"]
    selected = profile_name or section.get("defaultProfile")
    profiles = section.get("profiles", {})
    if selected is None:
        if len(profiles) != 1:
            raise BuildError("Select a fast profile with --profile")
        selected = next(iter(profiles))
    if selected not in profiles or not isinstance(profiles[selected], dict):
        raise BuildError("Unknown fast profile: " + str(selected))
    profile = dict(profiles[selected])
    flavor = profile.get("flavor")
    if flavor not in DEFAULTS:
        raise BuildError("Fast profile flavor must be tomcat or tongweb")
    if not isinstance(profile.get("baseImage"), str) or not profile["baseImage"].strip():
        raise BuildError("Fast profile needs baseImage")
    if any(char.isspace() for char in profile["baseImage"]):
        raise BuildError("Fast profile baseImage cannot contain whitespace")
    if bool(profile.get("baseTar")) == bool(profile.get("baseUrl")):
        raise BuildError("Fast profile needs exactly one of baseTar or baseUrl")
    if profile.get("baseTar"):
        profile["baseTar"] = _resolved_config_path(profile["baseTar"], config_path)
        if not profile["baseTar"].is_file():
            raise BuildError("Base image tar not found: " + str(profile["baseTar"]))
    else:
        from artifactory_download import artifact_directory_url, image_reference_from_directory
        base_url = profile["baseUrl"]
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise BuildError("baseUrl must explicitly start with http:// or https://")
        directory_url = artifact_directory_url(base_url)
        if image_reference_from_directory(directory_url) != profile["baseImage"]:
            raise BuildError("baseUrl resolves to a different image reference than baseImage")
        profile["baseUrl"] = directory_url
        connection = repository_settings(settings, directory_url)
        if connection and connection.get("source", "registry") != "artifactory":
            raise BuildError("Fast baseUrl repository must use source=artifactory")
        profile["username"] = connection.get("username")
        profile["passwordEnv"] = connection.get(
            "passwordEnv", "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD")
        profile["downloadWorkers"] = connection.get("workers", 0)
        if connection.get("caFile") is not None:
            profile["caFile"] = connection["caFile"]
        profile["imageStore"] = cache_directory(settings, "imageStore", None)
        username = profile.get("username")
        if username is not None and (not isinstance(username, str) or not username):
            raise BuildError("Invalid Artifactory username")
        password_env = profile.get("passwordEnv", "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD")
        if not isinstance(password_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
            raise BuildError("Invalid passwordEnv variable name")
        profile["passwordEnv"] = password_env
        if profile.get("caFile") is not None:
            profile["caFile"] = _resolved_config_path(profile["caFile"], config_path)
            if not profile["caFile"].is_file():
                raise BuildError("CA file not found: " + str(profile["caFile"]))
        workers = profile.get("downloadWorkers", 0)
        if type(workers) is not int or workers < 0 or workers > 64:
            raise BuildError("downloadWorkers must be an integer from 0 to 64")
    profile["deployDir"] = _validate_path(profile.get("deployDir", DEFAULTS[flavor]["deployDir"]),
                                          "deployDir")
    owner = profile.get("owner", DEFAULTS[flavor]["owner"])
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z_0-9][A-Za-z_0-9.-]*(?::[A-Za-z_0-9][A-Za-z_0-9.-]*)?", owner):
        raise BuildError("Invalid owner in fast profile")
    profile["owner"] = owner
    if profile.get("serverConfig") is not None:
        profile["serverConfig"] = _resolved_config_path(profile["serverConfig"], config_path)
        if not profile["serverConfig"].is_file():
            raise BuildError("Server config not found: " + str(profile["serverConfig"]))
        if flavor != "tongweb":
            raise BuildError("serverConfig is supported only for tongweb profiles")
    if profile.get("outputDir") is not None:
        profile["outputDir"] = _resolved_config_path(profile["outputDir"], config_path)
    return profile


def _require_base_tag(path, reference):
    try:
        with tarfile.open(path, "r:*") as archive:
            from image_reader import MAX_JSON, archive_members, docker_manifest
            entry = archive_members(archive)["manifest.json"]
            if not entry.isfile() or entry.size > MAX_JSON:
                raise BuildError("Base tar has no valid bounded manifest.json")
            stream = archive.extractfile(entry)
            if stream is None:
                raise BuildError("Base tar has no manifest.json")
            with stream:
                manifest = docker_manifest(json.load(stream))
    except (OSError, tarfile.TarError, ValueError, KeyError) as exc:
        raise BuildError("Cannot inspect base image tar: " + str(exc)) from exc
    if (not isinstance(manifest, list) or
            not any(isinstance(item, dict) and reference in (item.get("RepoTags") or [])
                    for item in manifest)):
        raise BuildError("Base tar does not contain FROM tag: " + reference)


def _base_archive(profile, platform="linux/amd64", reporter=None):
    if profile.get("baseTar") is not None:
        path = profile["baseTar"]
        if reporter is not None:
            reporter.log("Using local base " + str(path))
        _require_base_tag(path, profile["baseImage"])
        return path
    from image_store import pull_image
    archive, downloaded = pull_image(
        profile["baseImage"], platform=platform, source="artifactory",
        store=profile["imageStore"], username=profile.get("username"),
        password_env=profile["passwordEnv"], ca_file=profile.get("caFile"),
        workers=profile.get("downloadWorkers", 0), reporter=reporter,
        artifactory_url=profile["baseUrl"], materialize_tar=False)
    if reporter is not None:
        reporter.log("{} base {}".format("Downloaded" if downloaded else "Cached",
                                          profile["baseImage"]))
    return archive


def _validate_request(artifact, project, version):
    front = artifact.name.lower() == "dist.zip"
    if not front and artifact.suffix.lower() != ".war":
        raise BuildError("Fast mode accepts a .war file or a file named dist.zip")
    project = _validate_atom(project, "project")
    version = _validate_atom(version, "version", VERSION)
    return front, project, version


def _extract_dist(archive_path, destination, reporter=None):
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ZIP_FILES:
            raise BuildError("dist.zip is empty or has too many entries")
        total = sum(item.file_size for item in infos)
        if total > MAX_ZIP_BYTES:
            raise BuildError("dist.zip exceeds the 4 GiB uncompressed size limit")
        normalized = []
        for item in infos:
            name = item.filename
            if "\\" in name or "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise BuildError("Unsafe dist.zip entry: " + name)
            parts = PurePosixPath(name).parts
            if not parts or any(part in (".", "..") for part in parts):
                raise BuildError("Unsafe dist.zip entry: " + name)
            mode = (item.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise BuildError("dist.zip contains a link or special file: " + name)
            normalized.append((item, parts))
        strip_dist = all(parts[0] == "dist" for _, parts in normalized)
        planned = []
        files = set()
        directories = set()
        for item, parts in normalized:
            relative = parts[1:] if strip_dist else parts
            if not relative:
                if not item.is_dir():
                    raise BuildError("dist.zip root must be a directory: " + item.filename)
                continue
            if relative in files or relative in directories:
                raise BuildError("Duplicate dist.zip entry: " + item.filename)
            (directories if item.is_dir() else files).add(relative)
            planned.append((item, relative))
        for path in files:
            if any(path[:index] in files for index in range(1, len(path))):
                raise BuildError("dist.zip has a file/directory conflict: " + "/".join(path))
        for path in directories:
            if any(path[:index] in files for index in range(1, len(path))):
                raise BuildError("dist.zip has a file/directory conflict: " + "/".join(path))
        if not files:
            raise BuildError("dist.zip contains no regular files")
        extracted = 0
        destination.mkdir()
        for item, relative in planned:
            target = destination.joinpath(*relative)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, open(target, "xb") as sink:
                if reporter is None:
                    shutil.copyfileobj(source, sink, 4 * 1024 * 1024)
                else:
                    from progress.stream import CountingReader
                    reader = CountingReader(source,
                        lambda current, _total, _label, base=extracted:
                        reporter.progress(base + current, total, "Extracting dist.zip"),
                        item.file_size, item.filename)
                    shutil.copyfileobj(reader, sink, 4 * 1024 * 1024)
            extracted += item.file_size


def quick_build(artifact, project, version, config_path=None, *,
                tag=None, output=None, image_format="docker", platform="linux/amd64",
                source_date_epoch=0, cache_dir=None, profile_name=None,
                reporter=None, cache_stats=None, build_info=None,
                verify_cache=False):
    """根据应用包、显式主题名和版本号准备临时 Dockerfile，再调用通用构建器。"""
    artifact = Path(artifact).resolve()
    if not artifact.is_file():
        raise BuildError("Application package not found: " + str(artifact))
    config_path = _config_path(config_path)
    settings, _ = load_settings(config_path, required=True)
    profile = load_profile(config_path, profile_name)
    front, project, version = _validate_request(artifact, project, version)
    suffix = "front" if front else "back"
    tag = tag or "{}_{}:{}".format(project, suffix, version)
    output = Path(output).resolve() if output is not None else (
        profile.get("outputDir") or artifact.parent) / "{}_{}-{}.tar".format(project, suffix, version)
    output = output.resolve()
    if output == artifact or output == config_path or output == profile.get("baseTar"):
        raise BuildError("Output must differ from the inputs")
    if output.exists():
        raise BuildError("Output already exists: " + str(output))
    if cache_dir is None:
        cache_dir = cache_directory(settings, "layerCache",
                                    Path(tempfile.gettempdir()) / "pyimagebuilder-layer-cache")
    epoch = parse_epoch(source_date_epoch)
    try:
        temporary_context = tempfile.TemporaryDirectory(prefix="pyimagebuilder-fast-",
                                                        dir=artifact.parent)
    except OSError:
        temporary_context = tempfile.TemporaryDirectory(prefix="pyimagebuilder-fast-")
    with temporary_context as temporary:
        prepare_started = time.monotonic()
        if reporter is not None:
            reporter.phase_start("Preparing application package")
        context = Path(temporary) / "context"
        context.mkdir()
        if front:
            _extract_dist(artifact, context / "dist", reporter)
            source = "dist"
            destination = profile["deployDir"] + "/" + project + "f"
        else:
            target_war = context / "app.war"
            try:
                os.link(artifact, target_war)
                if reporter is not None:
                    reporter.log("WAR staged with a hard link")
            except OSError:
                if reporter is None:
                    shutil.copyfile(artifact, target_war)
                else:
                    from progress.stream import CountingReader
                    total = artifact.stat().st_size
                    with open(artifact, "rb") as source_stream, open(target_war, "xb") as sink:
                        shutil.copyfileobj(CountingReader(source_stream, reporter.progress,
                                                          total, "Copying WAR"), sink, 4 * 1024 * 1024)
            source = "app.war"
            destination = profile["deployDir"] + "/" + project + ".war"
        lines = ["FROM " + profile["baseImage"],
                 "COPY --chown={} {} {}".format(profile["owner"], source, destination)]
        if profile.get("serverConfig") is not None:
            shutil.copyfile(profile["serverConfig"], context / "tongweb.xml")
            lines.append("COPY --chown={} tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml".format(
                profile["owner"]))
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if reporter is not None:
            reporter.record_timing("Prepare WAR/dist package", time.monotonic() - prepare_started)
            reporter.phase_success("Preparing application package")
            reporter.phase_start("Resolving base image")
        base_tar = _base_archive(profile, platform, reporter)
        if reporter is not None:
            reporter.phase_success("Resolving base image")
        if output == base_tar:
            raise BuildError("Output must differ from the cached base tar")
        result = build(dockerfile, context, base_tar, None, tag, output,
                       enable_run=False, cache_dir=cache_dir, cache_stats=cache_stats,
                       image_format=image_format, source_date_epoch=epoch,
                       target_platform=platform, reporter=reporter, build_info=build_info,
                       trusted_base_cache=profile.get("baseTar") is None,
                       verify_cache=verify_cache)
    return result, tag


def _init(argv):
    parser = argparse.ArgumentParser(prog="fast.py init", description="Add a fast profile to config.json")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-tar", type=Path, help="Existing Docker save base image tar")
    source.add_argument("--base-url", help="Artifactory UI URL or image reference; downloaded on first build")
    parser.add_argument("--base-image", help="FROM reference; inferred from --base-url when omitted")
    parser.add_argument("--flavor", required=True, choices=tuple(DEFAULTS))
    parser.add_argument("--owner", help="Default: tomcat:tomcat or tongadmin:tonggrp")
    parser.add_argument("--deploy-dir", help="Default deploy directory for the flavor")
    parser.add_argument("--server-config", type=Path, help="Optional TongWeb tongweb.xml")
    parser.add_argument("--output-dir", type=Path, help="Default output directory")
    parser.add_argument("--base-cache-dir", type=Path, help="Directory for downloaded base tar")
    parser.add_argument("--username", help="Artifactory username; never store password in the profile")
    parser.add_argument("--password-env", default="PYIMAGEBUILDER_ARTIFACTORY_PASSWORD")
    parser.add_argument("--ca-file", type=Path, help="Custom HTTPS CA bundle")
    parser.add_argument("--download-workers", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", default="default", help="Name of the new fast profile")
    args = parser.parse_args(argv)
    config = (Path(args.config).resolve() if args.config is not None else
              Path(os.environ["PYIMAGEBUILDER_CONFIG"]).resolve()
              if os.environ.get("PYIMAGEBUILDER_CONFIG") else Path.cwd() / "config.json")
    _validate_atom(args.profile, "profile")
    if config.exists():
        load_settings(config, required=True)
        data = json.loads(config.read_text(encoding="utf-8-sig"))
    else:
        data = {"schemaVersion": 2, "repositories": {}, "cache": {},
                "fast": {"profiles": {}}}
    data.setdefault("cache", {}).setdefault("imageStore", "./data/image-store")
    fast_section = data.setdefault("fast", {})
    profiles = fast_section.setdefault("profiles", {})
    if args.profile in profiles:
        raise BuildError("Fast profile already exists: " + args.profile)
    if args.base_tar is not None and args.base_image is None:
        raise BuildError("--base-tar requires --base-image")
    if args.base_url is not None:
        from artifactory_download import artifact_directory_url, image_reference_from_directory
        base_url = args.base_url if "://" in args.base_url else "https://" + args.base_url
        directory_url = artifact_directory_url(base_url)
        inferred = image_reference_from_directory(directory_url)
        if args.base_image is not None and args.base_image != inferred:
            raise BuildError("--base-image does not match the Artifactory URL image reference")
        base_image = inferred
    else:
        base_image = args.base_image
    profile = {"flavor": args.flavor, "baseImage": base_image,
               "owner": args.owner or DEFAULTS[args.flavor]["owner"],
               "deployDir": args.deploy_dir or DEFAULTS[args.flavor]["deployDir"]}
    if args.base_tar is not None:
        profile["baseTar"] = str(args.base_tar.resolve())
    else:
        from urllib.parse import urlsplit
        profile["baseUrl"] = directory_url
        host = urlsplit(directory_url).netloc
        connection = data.setdefault("repositories", {}).setdefault(host, {})
        if connection.get("source", "artifactory") != "artifactory":
            raise BuildError("Repository host is already configured as a standard Registry: " + host)
        connection["source"] = "artifactory"
        if args.username is not None:
            connection["username"] = args.username
        connection["passwordEnv"] = args.password_env
        connection["workers"] = args.download_workers
        if args.base_cache_dir is not None:
            data.setdefault("cache", {})["imageStore"] = str(args.base_cache_dir.resolve())
        if args.ca_file is not None:
            connection["caFile"] = str(args.ca_file.resolve())
    if args.server_config is not None:
        profile["serverConfig"] = str(args.server_config.resolve())
    if args.output_dir is not None:
        profile["outputDir"] = str(args.output_dir.resolve())
    profiles[args.profile] = profile
    fast_section.setdefault("defaultProfile", args.profile)
    config.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.parent,
                                     prefix=".fast-profile-", delete=False) as stream:
        pending = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        load_profile(pending, args.profile)
        os.replace(pending, config)
    finally:
        unlink_missing(pending)
    print("Saved fast profile {} in {}".format(args.profile, config))
    return 0


def main(argv=None):
    """独立脚本与 main.py 共用的 Fast 入口，支持快速打包和 profile 初始化。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    reporter = None
    try:
        if argv and argv[0] == "init":
            return _init(argv[1:])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("artifact", type=Path, help="A WAR or dist.zip")
        parser.add_argument("project", help="Explicit project name, e.g. sfcx")
        parser.add_argument("version", help="Explicit image version, e.g. 1.3.4")
        parser.add_argument("--config", type=Path, help="Shared config.json path")
        parser.add_argument("--profile", help="Fast profile name from config.json")
        parser.add_argument("-t", "--tag", help="Override generated project_back/front:version tag")
        parser.add_argument("-o", "--output", type=Path, help="Override generated tar path")
        parser.add_argument("--format", choices=("docker", "oci", "both"), default="docker")
        parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
        parser.add_argument("--source-date-epoch", default="0")
        parser.add_argument("--cache-dir", type=Path)
        parser.add_argument("--verify-cache", action="store_true")
        parser.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--no-color", action="store_true")
        args = parser.parse_args(argv)
        from progress import make_reporter
        reporter = make_reporter(args.progress, args.quiet, args.verbose, args.debug, args.no_color)
        requested_tag = args.tag or "{}_{}:{}".format(
            args.project, "front" if args.artifact.name.lower() == "dist.zip" else "back", args.version)
        reporter.start(requested_tag, args.platform, mode="fast")
        stats, info = {}, {}
        result, tag = quick_build(args.artifact, args.project, args.version, args.config,
                                  tag=args.tag, output=args.output,
                                  image_format=args.format, platform=args.platform,
                                  source_date_epoch=args.source_date_epoch,
                                  cache_dir=args.cache_dir, profile_name=args.profile,
                                  reporter=reporter, cache_stats=stats, build_info=info,
                                  verify_cache=args.verify_cache)
        from main import _archive_digest
        digest, size = _archive_digest(result, reporter)
        reporter.success(image=tag, platform=args.platform, output=str(result),
                         digest=digest, size=size, layers=info.get("layers"),
                         cache="{} hit(s), {} miss(es), {} stored".format(
                             stats["hits"], stats["misses"], stats["stored"]),
                         oci_output=info.get("oci_output"))
        return 0
    except (BuildError, OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        if reporter is not None:
            reporter.fail(exc)
        else:
            print("Fast build failed: {}".format(exc), file=sys.stderr)
        if "args" in locals() and args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
