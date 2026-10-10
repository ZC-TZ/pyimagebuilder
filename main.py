#!/usr/bin/env python3
"""Python 镜像构建器统一入口：构建、拉取、Fast 打包与镜像管理。"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from builder import build
from errors import BuildError
from reproducible import parse_epoch
from build_args import arguments
from run_mounts import secrets
from settings import cache_directory, load_settings
from progress import make_reporter


def _archive_digest(path, reporter):
    total = path.stat().st_size
    done = 0
    digest = hashlib.sha256()
    reporter.phase_start("Verifying output digest")
    with reporter.timed("Hash final output archive"):
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
                done += len(block)
                reporter.progress(done, total, "Hashing archive")
    reporter.phase_success("Verifying output digest")
    return "sha256:" + digest.hexdigest(), total


def _wsl_path(path, mount_root):
    value = Path(path).resolve()
    drive = value.drive
    if len(drive) != 2 or drive[1] != ":":
        raise BuildError("WSL bridge requires a drive-letter Windows path: " + str(value))
    remainder = value.as_posix()[2:].lstrip("/")
    return "{}/{}/{}".format(mount_root.rstrip("/"), drive[0].lower(), remainder)


def _run_wsl(args, trusted_base_cache=False):
    if platform.system() != "Windows":
        raise BuildError("--wsl is only for the Windows launcher")
    if not args.run:
        raise BuildError("--wsl requires --run")
    executable = shutil.which("wsl.exe")
    if executable is None:
        raise BuildError("wsl.exe not found; install/configure a Linux WSL environment first")
    command = [executable]
    if args.wsl_distro:
        command += ["--distribution", args.wsl_distro]
    if getattr(args, "wsl_user", None):
        command += ["--user", args.wsl_user]
    elif getattr(args, "run_sandbox", "hardened") != "rootless":
        command += ["--user", "root"]
    command += ["--exec", args.wsl_python,
                _wsl_path(__file__, args.wsl_mount_root),
                "--dockerfile", _wsl_path(args.dockerfile, args.wsl_mount_root),
                "--context", _wsl_path(args.context, args.wsl_mount_root),
                "--tag", args.tag,
                "--output", _wsl_path(args.output, args.wsl_mount_root),
                "--format", getattr(args, "format", "docker"),
                "--platform", getattr(args, "platform", "linux/amd64"),
                "--source-date-epoch", str(getattr(args, "source_date_epoch", 0)),
                "--progress", getattr(args, "progress", "auto"),
                "--workspace", args.workspace or "/tmp",
                "--run", "--run-network", args.run_network,
                "--run-sandbox", getattr(args, "run_sandbox", "hardened")]
    if getattr(args, "oci_output", None) is not None:
        command += ["--oci-output", _wsl_path(args.oci_output, args.wsl_mount_root)]
    if getattr(args, "target", None) is not None:
        command += ["--target", args.target]
    if getattr(args, "allow_emulated_run", False):
        command.append("--allow-emulated-run")
    for item in getattr(args, "build_arg", []):
        command += ["--build-arg", item]
    for item in getattr(args, "secret", []):
        identifier, _, path = item.partition("=")
        command += ["--secret", identifier + "=" + _wsl_path(path, args.wsl_mount_root)]
    if getattr(args, "attest", False):
        command.append("--attest")
    for flag in ("quiet", "verbose", "debug", "no_color"):
        if getattr(args, flag, False):
            command.append("--" + flag.replace("_", "-"))
    if getattr(args, "sign_key", None) is not None:
        command += ["--sign-key", _wsl_path(args.sign_key, args.wsl_mount_root)]
    if args.no_cache:
        command.append("--no-cache")
    elif args.cache_dir is not None:
        command += ["--cache-dir", _wsl_path(args.cache_dir, args.wsl_mount_root)]
    else:
        command += ["--cache-dir", "/tmp/pyimagebuilder-layer-cache"]
    if getattr(args, "verify_cache", False):
        command.append("--verify-cache")
    if trusted_base_cache:
        command.append("--trusted-base-cache")
    if args.base_tar is not None:
        command += ["--base-tar", _wsl_path(args.base_tar, args.wsl_mount_root)]
        return subprocess.call(command)
    if args.base_map is None:
        return subprocess.call(command)
    mapping_path = args.base_map.resolve()
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("Cannot read Windows base map: " + str(exc)) from exc
    if not isinstance(mapping, dict) or not mapping:
        raise BuildError("Windows base map must be a nonempty mapping")
    translated = {}
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise BuildError("Windows base map keys must be strings")
        def translate(item):
            if isinstance(item, dict) and item.get("type") == "cas":
                return dict(item, store=_wsl_path(item["store"], args.wsl_mount_root))
            if not isinstance(item, str) or not item:
                raise BuildError("Windows base map paths must be nonempty strings")
            path = Path(item)
            if not path.is_absolute():
                path = mapping_path.parent / path
            return _wsl_path(path, args.wsl_mount_root)
        if isinstance(value, dict) and value.get("type") == "cas":
            translated[key] = translate(value)
        elif isinstance(value, dict):
            translated[key] = {platform: translate(path) for platform, path in value.items()}
        else:
            translated[key] = translate(value)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-wsl-map-") as temporary:
        linux_map = Path(temporary) / "base-map.json"
        linux_map.write_text(json.dumps(translated), encoding="utf-8")
        return subprocess.call(command + ["--base-map", _wsl_path(linux_map, args.wsl_mount_root)])


def main(argv=None):
    """分派 Docker 风格子命令，或从本地上下文构建单平台镜像归档。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pull":
        from image_store import pull_main
        return pull_main(argv[1:])
    if argv and argv[0] == "fast":
        from fast import main as fast_main
        return fast_main(argv[1:])
    if argv and argv[0] == "cas":
        from cas_store import main as cas_main
        return cas_main(argv[1:])
    if argv and argv[0] in ("export", "import", "flatten"):
        from rootfs_archive import main as rootfs_main
        return rootfs_main(argv)
    if argv and argv[0] in ("inspect", "verify", "history", "images", "cache",
                            "tag", "manifest", "push", "load", "save", "rmi",
                            "image", "system"):
        from image_cli import main as image_main
        return image_main(argv)
    if argv and argv[0] in ("analyze", "optimize"):
        from optimizer import main as optimizer_main
        command = argv.pop(0)
        return optimizer_main([command] + argv)
    if argv and argv[0] == "build":
        argv.pop(0)
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Commands: build (default), pull, fast, cas, load, save, export, import, flatten, rmi, images, "
               "image prune, system df, inspect, verify, history, cache, tag, "
               "manifest inspect/create/push, push, analyze, optimize. "
               "Use 'main.py COMMAND --help' for command options.")
    parser.add_argument("context_path", nargs="?", type=Path,
                        help="Build context, as in docker build [OPTIONS] PATH")
    parser.add_argument("--config", type=Path, help="Shared config.json path")
    parser.add_argument("-f", "--file", "--dockerfile", dest="dockerfile", type=Path,
                        help="Dockerfile path; defaults to <context>/Dockerfile")
    parser.add_argument("--context", type=Path,
                        help="Build context (legacy spelling; use positional PATH for new commands)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base-tar", type=Path, help="Local Docker save base image")
    source.add_argument("--base-map", type=Path, help="JSON mapping FROM names to local tar files")
    parser.add_argument("-t", "--tag", help="Output image repository:tag")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output .tar file (local export; not Docker's Buildx output syntax)")
    parser.add_argument("--check", action="store_true",
                        help="Validate Dockerfile syntax and local sources without building")
    parser.add_argument("--format", choices=("docker", "oci", "both"), default="docker",
                        help="Docker archive, OCI image-layout archive, or both")
    parser.add_argument("--oci-output", type=Path,
                        help="OCI tar path with --format both; defaults to <output-stem>.oci.tar")
    parser.add_argument("--source-date-epoch",
                        help="UTC Unix timestamp for reproducible output; defaults to SOURCE_DATE_EPOCH or 0")
    parser.add_argument("--target", help="Build through the named or numeric stage; defaults to the last stage")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                        default="linux/amd64", help="Target platform for FROM and scratch")
    parser.add_argument("--attest", action="store_true", help="Write SPDX SBOM and SLSA provenance sidecars")
    parser.add_argument("--sign-key", type=Path, help="Sign provenance with an offline Ed25519 private seed; implies --attest")
    parser.add_argument("--run", action="store_true", help="Execute RUN in an isolated Linux rootfs")
    parser.add_argument("--network", "--run-network", dest="run_network",
                        choices=("none", "host"), default="none")
    parser.add_argument("--run-sandbox", choices=("legacy", "hardened", "rootless"),
                        default="hardened", help="RUN isolation; rootless requires Linux user namespaces")
    parser.add_argument("--allow-emulated-run", action="store_true",
                        help="Allow foreign RUN only with preconfigured F-mode binfmt_misc/QEMU")
    parser.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE",
                        help="Build-time ARG value; do not use for secrets")
    parser.add_argument("--secret", action="append", default=[], metavar="ID=/absolute/file",
                        help="File for RUN --mount=type=secret,id=ID")
    parser.add_argument("--workspace", help="Native Linux temporary filesystem for RUN; defaults to /tmp")
    parser.add_argument("--cache-dir", type=Path, help="Persistent local layer cache directory")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache reads and writes")
    parser.add_argument("--verify-cache", action="store_true",
                        help="Fully hash cached layers and recheck base archive layers")
    parser.add_argument("--trusted-base-cache", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pull", action="store_true", help="Refresh external FROM images before building")
    parser.add_argument("--offline", action="store_true", help="Use only explicit local base tar/map or cached images")
    parser.add_argument("--pull-source", choices=("registry", "artifactory"))
    parser.add_argument("--image-store", type=Path, help="Local cache for pulled base image tar files")
    parser.add_argument("--username", help="Repository username for automatic pulls")
    parser.add_argument("--password-env", help="Environment variable containing pull password")
    parser.add_argument("--ca-file", type=Path, help="Custom HTTPS CA bundle for pulls")
    parser.add_argument("--insecure-http", action="store_true", help="Use HTTP for a configured private repository")
    parser.add_argument("--auth-host", help="Trusted Registry Bearer token service host")
    parser.add_argument("--pull-workers", type=int, help="Artifactory download workers")
    parser.add_argument("--hermetic", action="store_true",
                        help="Build only from a verified local input lock and snapshot; rejects RUN")
    parser.add_argument("--input-lock", type=Path,
                        help="Input lock created by hermetic.py; required with --hermetic")
    parser.add_argument("--wsl", action="store_true", help="On Windows, run this Python builder inside WSL")
    parser.add_argument("--wsl-distro", help="Optional WSL distribution name")
    parser.add_argument("--wsl-user", help="WSL account for RUN; rootless defaults to distribution's non-root user")
    parser.add_argument("--wsl-python", default="python3", help="Python executable inside WSL")
    parser.add_argument("--wsl-mount-root", default="/mnt", help="WSL Windows-drive mount root")
    parser.add_argument("--progress", choices=("auto", "plain", "json"), default="auto")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        try:
            if args.context_path is not None and args.context is not None:
                raise BuildError("Specify build context once: positional PATH or --context")
            context = args.context_path or args.context
            if context is None:
                raise BuildError("Build context required: use positional PATH or --context")
            from build_check import check
            result = check(args.dockerfile or context / "Dockerfile", context,
                           args.platform, args.target, arguments(args.build_arg))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (BuildError, OSError, ValueError) as exc:
            print("Build check failed: {}".format(exc), file=sys.stderr)
            return 1
    if args.tag is None or args.output is None:
        parser.error("build requires -t/--tag and -o/--output (unless --check is used)")
    reporter = make_reporter(args.progress, args.quiet, args.verbose, args.debug, args.no_color)
    reporter.start(args.tag, args.platform)
    temporary_map = None
    try:
        settings, _ = load_settings(args.config)
        if not args.hermetic:
            args.image_store = args.image_store or cache_directory(settings, "imageStore", None)
            args.cache_dir = args.cache_dir or cache_directory(settings, "layerCache", None)
        if args.context_path is not None and args.context is not None:
            raise BuildError("Specify build context once: positional PATH or --context")
        args.context = args.context_path or args.context
        if args.context is None:
            raise BuildError("Build context required: use positional PATH or --context")
        args.dockerfile = args.dockerfile or args.context / "Dockerfile"
        if args.hermetic and args.source_date_epoch is None:
            raise BuildError("--hermetic requires an explicit --source-date-epoch")
        if args.hermetic and args.input_lock is None:
            raise BuildError("--hermetic requires --input-lock")
        if args.input_lock is not None and not args.hermetic:
            raise BuildError("--input-lock requires --hermetic")
        args.source_date_epoch = parse_epoch(args.source_date_epoch if args.source_date_epoch is not None
                                             else os.environ.get("SOURCE_DATE_EPOCH", "0"))
        parsed_args = arguments(args.build_arg)
        parsed_secrets = secrets(args.secret)
        if args.output.exists():
            raise BuildError("Output already exists: " + str(args.output))
        if args.format == "both":
            secondary = args.oci_output or args.output.with_name(args.output.stem + ".oci.tar")
            if secondary.exists():
                raise BuildError("OCI output already exists: " + str(secondary))
        if args.pull and args.offline:
            raise BuildError("--pull and --offline cannot be combined")
        if args.pull and (args.base_tar is not None or args.base_map is not None):
            raise BuildError("--pull cannot refresh an explicitly supplied local base tar/map")
        if args.hermetic:
            if (args.run or args.wsl or args.secret or args.cache_dir is not None or
                    args.attest or args.sign_key is not None or args.allow_emulated_run or
                    args.run_network != "none" or args.workspace is not None or args.pull or
                    args.username is not None or args.password_env is not None or
                    args.ca_file is not None or args.insecure_http or args.auth_host is not None):
                raise BuildError("--hermetic rejects RUN, WSL, secrets, cache, attestation, "
                                 "emulation, network, and custom workspace options")
            from hermetic import build_locked
            result, report = build_locked(
                args.input_lock, args.dockerfile, args.context, args.base_tar, args.base_map,
                args.tag, args.output, target_platform=args.platform, target_stage=args.target,
                image_format=args.format, oci_output=args.oci_output,
                source_date_epoch=args.source_date_epoch, build_args=parsed_args)
            digest, size = _archive_digest(result, reporter)
            reporter.success(image=args.tag, platform=args.platform, output=str(result),
                             digest=digest, size=size, hermetic_report=str(report))
            return 0
        managed_base = args.base_tar is None and args.base_map is None
        if managed_base:
            from image_store import pull_base_map
            temporary_map = tempfile.TemporaryDirectory(prefix="pyimagebuilder-base-map-")
            map_path = Path(temporary_map.name) / "base-map.json"
            reporter.phase_start("Resolving base images")
            args.base_map = pull_base_map(
                args.dockerfile, target_platform=args.platform, target_stage=args.target,
                build_args=parsed_args, source=args.pull_source, store=args.image_store,
                refresh=args.pull, offline=args.offline,
                username=args.username, password_env=args.password_env,
                ca_file=args.ca_file, insecure_http=args.insecure_http,
                auth_host=args.auth_host, workers=args.pull_workers,
                destination=map_path, settings=settings, reporter=reporter)
            reporter.phase_success("Resolving base images")
        if args.wsl:
            return _run_wsl(args, managed_base)
        chosen_cache = None if args.no_cache else (args.cache_dir or
            Path(tempfile.gettempdir()) / "pyimagebuilder-layer-cache")
        stats = {}
        build_info = {}
        result = build(args.dockerfile, args.context, args.base_tar, args.base_map,
                       args.tag, args.output, args.run, args.run_network, args.workspace,
                       chosen_cache, stats, args.target, args.format, args.oci_output,
                       args.source_date_epoch, args.attest, args.sign_key, args.run_sandbox,
                       args.platform, args.allow_emulated_run, parsed_args, parsed_secrets,
                       reporter=reporter, build_info=build_info,
                       allow_remote_add=not args.offline,
                       trusted_base_cache=managed_base or args.trusted_base_cache,
                       verify_cache=args.verify_cache)
    except (BuildError, OSError, RuntimeError, ValueError) as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if temporary_map is not None:
            temporary_map.cleanup()
    try:
        digest, size = _archive_digest(result, reporter)
        reporter.success(image=args.tag, platform=build_info.get("platform", args.platform), output=str(result),
                         digest=digest, size=size, layers=build_info.get("layers"),
                         cache=("disabled" if args.no_cache else
                                "{} hit(s), {} miss(es), {} stored".format(
                                    stats["hits"], stats["misses"], stats["stored"])),
                         oci_output=build_info.get("oci_output"),
                         sbom=str(result) + ".spdx.json" if args.attest else None,
                         provenance=str(result) + ".provenance.json" if args.attest else None,
                         signature=str(result) + ".dsse.json" if args.sign_key else None)
    except OSError as exc:
        reporter.fail(exc)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
