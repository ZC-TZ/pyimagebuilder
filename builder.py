"""协调基础镜像读取、Dockerfile 执行、层缓存与归档发布；RUN 需要 Linux。"""

import copy
from contextlib import nullcontext
import json
import platform
import re
import tempfile
from pathlib import Path
from dataclasses import asdict, dataclass, replace

from cache import CACHE_VERSION, LayerCache, cache_key
from dockerfile_parser import parse, valid_stop_signal
from errors import BaseImageNotFound, BuildError, UnsupportedInstruction
from image_config import ImageConfig
from image_reader import BaseImage, ImageArchiveReader
from cas_store import CASStore
from image_writer import ImageArchiveWriter
from file_publish import publish_new_file
from oci_writer import OCIImageWriter
from layer import LayerBuilder, container_path
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer
from reproducible import parse_epoch
from platforms import architecture, host_architecture, normalize_platform, require_foreign_binfmt
from attest import artifact_paths, create_provenance, read_private_key, sign_envelope
from sbom import canonical_json, create_sbom
from build_args import expand
from run_mounts import prepare as prepare_run_mounts, locked_caches
from compat import unlink_missing


@dataclass
class StageSnapshot:
    """保存已完成阶段的配置、层、文件索引和 ARG，供后续 FROM/COPY --from 复用。"""
    config: dict
    layers: list
    rootfs: RootFSIndex
    state_key: str
    build_args: dict = None
    config_raw: bytes = None
    manifest_raw: bytes = None


def _resolve_base(from_reference, base_tar, base_map, target_platform="linux/amd64"):
    if base_tar is not None:
        if isinstance(base_tar, dict):
            return _validate_cas_base(base_tar, from_reference, target_platform)
        return Path(base_tar).resolve()
    if base_map is None:
        raise BaseImageNotFound("Provide --base-tar or --base-map for FROM " + from_reference)
    mapping_path = Path(base_map).resolve()
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("Cannot read base map: " + str(exc)) from exc
    if not isinstance(mapping, dict) or from_reference not in mapping:
        raise BaseImageNotFound("No local base image mapped for FROM " + from_reference)
    value = mapping[from_reference]
    if isinstance(value, dict) and value.get("type") != "cas":
        value = value.get(target_platform)
    if isinstance(value, dict):
        return _validate_cas_base(value, from_reference, target_platform)
    if not isinstance(value, str) or not value:
        raise BuildError("Base map must contain a path for FROM {} on {}".format(
            from_reference, target_platform))
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = mapping_path.parent / candidate
    return candidate.resolve()


def _validate_cas_base(value, reference, platform):
    if (value.get("type") != "cas" or value.get("reference") != reference or
            value.get("platform") != platform or
            value.get("source") not in ("local", "registry", "artifactory") or
            not isinstance(value.get("store"), str)):
        raise BuildError("Invalid CAS base mapping for FROM {} on {}".format(reference, platform))
    return value


def build(dockerfile, context, base_tar, base_map, tag, output,
          enable_run=False, run_network="none", workspace_dir=None,
          cache_dir=None, cache_stats=None, target_stage=None,
          image_format="docker", oci_output=None, source_date_epoch=0,
          attest=False, sign_key=None, run_sandbox="hardened",
          target_platform="linux/amd64", allow_emulated_run=False,
          build_args=None, secret_sources=None, reporter=None, build_info=None,
          allow_remote_add=True, trusted_base_cache=False, verify_cache=False):
    """构建指定平台及目标阶段，并在校验成功后发布归档。

    调用方负责提供基础 tar、映射文件或 CAS 描述；此函数不处理 Registry 鉴权。
    文件系统指令产生新层，元数据指令只更新 config/history，不生成空 tar。
    输出路径必须尚不存在，失败时不能覆盖已有交付文件。

    Args:
        base_tar: 基础归档路径或 CAS 描述；多个外部基础镜像使用 base_map。
        base_map: 按镜像引用及平台查找基础镜像的 JSON 映射文件。
        cache_dir: 指令层缓存目录；None 表示不使用持久缓存。
        cache_stats: 可选的调用方字典，用于接收 hits/misses/stored 计数。
        source_date_epoch: 归档和层的固定时间戳，影响输出摘要及缓存键。

    Returns:
        主输出归档的绝对 Path；附属产物通过参数和 build_info 查询。

    Raises:
        BuildError: 输入、镜像结构或执行条件不满足构建要求。
        UnsupportedInstruction: 指令语义或宿主执行能力不受支持。
    """
    source_date_epoch = parse_epoch(source_date_epoch)
    target_platform = normalize_platform(target_platform)
    build_args = dict(build_args or {})
    secret_sources = dict(secret_sources or {})
    if run_sandbox not in ("legacy", "hardened", "rootless"):
        raise BuildError("RUN sandbox must be legacy, hardened, or rootless")
    dockerfile = Path(dockerfile).resolve()
    context = Path(context).resolve()
    output = Path(output).resolve()
    if not dockerfile.is_file():
        raise BuildError("Dockerfile not found: " + str(dockerfile))
    if not context.is_dir():
        raise BuildError("Build context not found: " + str(context))
    ignore_file = context / ".dockerignore"
    if ignore_file.is_file():
        try:
            rules = ignore_file.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise BuildError(".dockerignore must be UTF-8") from exc
        if any(line.strip() and not line.lstrip().startswith("#") for line in rules):
            raise UnsupportedInstruction(".dockerignore rules are not implemented; refusing an incorrect context")
    repository, separator, version = tag.rpartition(":")
    if (not separator or not repository or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", version)
            or any(char.isspace() for char in repository) or "@" in repository or repository.endswith("/")):
        raise BuildError("--tag must be a valid repository:tag")
    if output.suffix.lower() != ".tar":
        raise BuildError("--output must end with .tar")
    if image_format not in ("docker", "oci", "both"):
        raise BuildError("--format must be docker, oci, or both")
    if oci_output is not None and image_format != "both":
        raise BuildError("--oci-output is only valid with --format both")
    if image_format == "both":
        oci_output = (Path(oci_output).resolve() if oci_output is not None else
                      output.with_name(output.stem + ".oci.tar"))
        if oci_output == output or oci_output.suffix.lower() != ".tar":
            raise BuildError("OCI output must be a different .tar file")
        if oci_output.exists():
            raise BuildError("OCI output already exists: " + str(oci_output))
    if output.exists():
        raise BuildError("Output already exists: " + str(output))
    if sign_key is not None:
        attest = True
    sidecars = tuple(artifact_paths(output)) if attest else ()
    for destination in sidecars:
        if destination.exists():
            raise BuildError("Attestation output already exists: " + str(destination))
    seed = read_private_key(sign_key) if sign_key is not None else None
    if reporter is not None:
        reporter.phase_start("Preparing build")
    try:
        instructions = parse(dockerfile.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise BuildError("Dockerfile must be UTF-8") from exc
    stage_headers = [item for item in instructions if item.op == "FROM"]
    if target_stage is None:
        target_index = len(stage_headers) - 1
    elif str(target_stage).isdecimal() and int(target_stage) < len(stage_headers):
        target_index = int(target_stage)
    else:
        matches = [index for index, item in enumerate(stage_headers)
                   if item.value.alias and item.value.alias.lower() == str(target_stage).lower()]
        if not matches:
            raise BuildError("Unknown --target stage: " + str(target_stage))
        target_index = matches[0]
    selected = []
    seen_headers = -1
    for item in instructions:
        if item.op == "FROM":
            seen_headers += 1
            if seen_headers > target_index:
                break
        selected.append(item)
    step_numbers = {id(item): index for index, item in enumerate(selected, 1)}
    step_total = len(selected)
    if reporter is not None:
        reporter.log("Dockerfile parsed: {} instruction(s)".format(step_total))
        reporter.phase_success("Preparing build")
    if any(item.op == "RUN" for item in selected) and not enable_run:
        raise UnsupportedInstruction("RUN requires --run on Linux or --run --wsl on Windows")
    output.parent.mkdir(parents=True, exist_ok=True)
    if oci_output is not None:
        oci_output.parent.mkdir(parents=True, exist_ok=True)
    workspace_parent = Path(workspace_dir).resolve() if workspace_dir is not None else Path(tempfile.gettempdir()).resolve()
    if workspace_parent == context or context in workspace_parent.parents:
        raise BuildError("Temporary workspace must be outside the build context: " + str(workspace_parent))
    cache_root = Path(cache_dir).resolve() if cache_dir is not None else None
    if cache_root is not None and (cache_root == context or context in cache_root.parents):
        raise BuildError("Layer cache must be outside the build context: " + str(cache_root))
    cache = LayerCache(cache_root, verify=verify_cache) if cache_root is not None else None
    if cache_stats is not None:
        cache_stats.update(hits=0, misses=0, stored=0)
    workspace_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-", dir=workspace_parent) as temp:
        workspace = Path(temp)
        if reporter is not None and reporter.debug:
            reporter.log("Workspace: " + str(workspace))
        completed = []
        stage_names = {}
        stage_index = -1
        external_references = set()
        base_inputs = {}
        rootfs = None
        layers = None
        config = None
        layer_builder = None
        workdir = None
        shell = None
        user = None
        materialized = None
        state_key = None
        global_args = {"TARGETPLATFORM": target_platform,
                       "TARGETOS": "linux", "TARGETARCH": architecture(target_platform),
                       "TARGETVARIANT": ""}
        host_arch = host_architecture()
        if host_arch is not None:
            global_args.update(BUILDPLATFORM="linux/" + host_arch, BUILDOS="linux",
                               BUILDARCH=host_arch, BUILDVARIANT="")
        stage_args = {}
        stage_cmd_set = False

        def record(instruction, diff_id=None):
            """同步更新镜像 history 与后续缓存的父状态；元数据变化也会影响后续缓存键。"""
            nonlocal state_key
            config.record(instruction, diff_id)
            state_key = cache_key("state", state_key, instruction.raw, diff_id)

        def resolve_layer(instruction, target, produce, extra=None, independent=False):
            """按输入和父状态查找指令缓存；未命中时生成层并在校验后发布缓存。

            返回 (layer_path, diff_id)；未产生文件变化时二者均为 None。
            independent=True 用于 --link：不依赖基础 rootfs，但仍依赖展开后的 COPY/ADD 输入。
            """
            parent_state = ("linked-layer", target_platform) if independent else state_key
            key = cache_key("instruction", CACHE_VERSION, parent_state, instruction.raw, extra)
            if cache is not None:
                with reporter.timed("Layer cache lookup") if reporter is not None else nullcontext():
                    hit = cache.lookup(key)
                if hit is not None:
                    if reporter is not None:
                        reporter.cache_hit(hit.diff_id or "metadata")
                        if reporter.verbose:
                            reporter.log("Layer cache key: " + key)
                    if cache_stats is not None:
                        cache_stats["hits"] += 1
                    return hit.path, hit.diff_id
                if cache_stats is not None:
                    cache_stats["misses"] += 1
                if reporter is not None:
                    reporter.cache_miss()
            elif reporter is not None:
                reporter.cache_miss()
            with reporter.timed("COPY/ADD layer creation" if instruction.op in ("COPY", "ADD")
                                else "RUN layer creation" if instruction.op == "RUN"
                                else "Other layer creation") if reporter is not None else nullcontext():
                diff_id = produce(target)
            if reporter is not None and reporter.verbose and diff_id is not None:
                reporter.log("Layer DiffID: " + diff_id)
            if cache is not None:
                with reporter.timed("Layer cache store") if reporter is not None else nullcontext():
                    cache.store(key, diff_id, target if diff_id is not None else None)
                if cache_stats is not None:
                    cache_stats["stored"] += 1
            return (target if diff_id is not None else None), diff_id

        def accept_layer(path, diff_id):
            """把非空层同时应用到虚拟索引和已物化的 rootfs，保持后续指令视图一致。"""
            if diff_id is not None:
                rootfs.apply_layer(path)
                layers.append(path)
                if materialized is not None:
                    materialized.apply(path)

        planned = list(instructions)
        trigger_ids = set()
        for number, instruction in enumerate(planned):
            op = instruction.op
            if reporter is not None:
                if id(instruction) in trigger_ids:
                    reporter.phase_start("ONBUILD " + instruction.raw.splitlines()[0])
                elif id(instruction) in step_numbers:
                    reporter.step_start(step_numbers[id(instruction)], step_total,
                                        instruction.raw, instruction.line)
            if op == "ARG" and stage_index < 0:
                for name, separator, default in instruction.value:
                    if name in build_args:
                        global_args[name] = build_args[name]
                    elif separator:
                        global_args[name] = expand(default, global_args)
                if reporter is not None:
                    reporter.step_success()
                continue
            if op == "FROM":
                if stage_index >= 0:
                    stage_config, stage_raw = config.export()
                    completed.append(StageSnapshot(stage_config, list(layers), rootfs,
                                                   state_key, dict(stage_args), stage_raw,
                                                   config.manifest_raw if stage_raw is not None else None))
                stage_index += 1
                if stage_index > target_index:
                    break
                reference = expand(instruction.value.reference, global_args)
                if not reference:
                    raise BuildError("FROM resolved to an empty image reference")
                stage_platform = (expand(instruction.value.platform, global_args)
                                  if instruction.value.platform else target_platform)
                stage_platform = normalize_platform(stage_platform)
                stage_ref = stage_names.get(reference.lower())
                if stage_ref is None and reference.isdecimal():
                    index = int(reference)
                    if index >= len(completed):
                        raise BuildError("FROM references a stage that is not complete: " + reference)
                    stage_ref = index
                if stage_ref is not None:
                    previous = completed[stage_ref]
                    if previous.config.get("os") != "linux" or \
                            previous.config.get("architecture") != architecture(stage_platform):
                        raise BuildError("FROM stage platform does not match " + stage_platform)
                    rootfs = copy.deepcopy(previous.rootfs)
                    layers = list(previous.layers)
                    config = ImageConfig(previous.config, source_date_epoch, previous.config_raw,
                                         previous.manifest_raw)
                    state_key = cache_key("stage-base", previous.state_key, source_date_epoch)
                    inherited_args = dict(previous.build_args or {})
                elif reference.lower() == "scratch":
                    rootfs = RootFSIndex()
                    layers = []
                    config = ImageConfig({"architecture": architecture(stage_platform), "os": "linux",
                                          "rootfs": {"type": "layers", "diff_ids": []},
                                          "config": {}, "history": []}, source_date_epoch)
                    state_key = cache_key("scratch-base", stage_platform, source_date_epoch)
                    inherited_args = {}
                else:
                    external_references.add(reference)
                    if base_tar is not None and len(external_references) > 1:
                        raise BuildError("Multiple external FROM images require --base-map")
                    base_path = _resolve_base(reference, base_tar, base_map, stage_platform)
                    is_cas = isinstance(base_path, dict)
                    if reporter is not None:
                        reporter.log("Base CAS: " + reference if is_cas else "Base archive: " + str(base_path))
                    if not is_cas and (base_path == output or base_path == oci_output):
                        raise BuildError("Output must differ from base archive")
                    with reporter.timed("Read and index base CAS" if is_cas else
                                        "Read and index base archive") if reporter is not None else nullcontext():
                        if is_cas:
                            cas = CASStore(base_path["store"])
                            base = cas.open_base(reference, stage_platform, base_path["source"])
                            base_inputs[reference + "@" + stage_platform] = {
                                "sha256": base.source_manifest["CASManifest"].split(":", 1)[1]}
                        else:
                            base_inputs[reference + "@" + stage_platform] = base_path
                            base_store = (cache_root / "base-layers") if (trusted_base_cache and
                                          not verify_cache and cache_root is not None) else None
                            reader = ImageArchiveReader(base_path, workspace / ("base-{}".format(stage_index)),
                                                        trusted_layer_store=base_store)
                            if reporter is None:
                                base = reader.read(reference, stage_platform)
                            else:
                                base = reader.read(reference, stage_platform,
                                                   progress=lambda current, total, label:
                                                   reporter.progress(current, total, label))
                        rootfs = RootFSIndex()
                        for layer_index, layer_path in enumerate(base.layers, 1):
                            rootfs.apply_layer(layer_path)
                            if reporter is not None:
                                reporter.progress(layer_index, len(base.layers), "Applying base layers", unit="layers")
                    layers = list(base.layers)
                    config = ImageConfig(base.config, source_date_epoch, base.original_config_bytes(),
                                         base.original_manifest_bytes())
                    state_key = cache_key("base", reference, base.config,
                                          base.config["rootfs"]["diff_ids"], source_date_epoch)
                    inherited_args = {}
                if instruction.value.alias:
                    stage_names[instruction.value.alias.lower()] = stage_index
                inherited_triggers = config.runtime.get("OnBuild", [])
                if not isinstance(inherited_triggers, list) or any(not isinstance(item, str) for item in inherited_triggers):
                    raise BuildError("Base image OnBuild must be a list of Dockerfile instructions")
                triggers = []
                for trigger in inherited_triggers:
                    parsed_triggers = parse("FROM scratch\n" + trigger)
                    # 外部镜像触发器必须恰好解析为一条指令；heredoc 可以包含换行。
                    if len(parsed_triggers) != 2:
                        raise BuildError("Base image ONBUILD trigger must contain exactly one instruction: " + trigger)
                    parsed_trigger = parsed_triggers[1]
                    if parsed_trigger.op in ("FROM", "ONBUILD", "MAINTAINER"):
                        raise UnsupportedInstruction("Invalid base image ONBUILD trigger: " + trigger)
                    if parsed_trigger.op == "RUN" and not enable_run:
                        raise UnsupportedInstruction("Base image ONBUILD RUN requires --run")
                    triggers.append(replace(parsed_trigger, line=instruction.line))
                if triggers:
                    config.runtime.pop("OnBuild", None)
                    planned[number + 1:number + 1] = triggers
                    trigger_ids.update(id(item) for item in triggers)
                layer_builder = LayerBuilder(context, rootfs, source_date_epoch, reporter)
                workdir = container_path(config.runtime.get("WorkingDir") or "/")
                shell = config.runtime.get("Shell") or ["/bin/sh", "-c"]
                user = config.runtime.get("User") or ""
                materialized = None
                stage_args = inherited_args
                stage_cmd_set = False
                if reporter is not None:
                    reporter.step_success()
                continue
            variables = dict(stage_args)
            environment_names = set()
            for item in config.runtime.get("Env") or []:
                key, value = item.split("=", 1)
                variables[key] = value
                environment_names.add(key)
            if op == "ARG":
                for name, separator, default in instruction.value:
                    if name in build_args:
                        stage_args[name] = build_args[name]
                    elif separator:
                        stage_args[name] = expand(default, variables)
                    elif name in global_args:
                        stage_args[name] = global_args[name]
                    # 无覆盖值时保留当前阶段或父阶段的参数；未定义的参数仍保持未定义。
                    # 同一 ARG 中后续默认值可读取刚定义的参数，已有 ENV 的优先级不变。
                    if name in stage_args and name not in environment_names:
                        variables[name] = stage_args[name]
                record(instruction)
                state_key = cache_key("arg-value", state_key, stage_args)
            elif op == "ENV":
                config.set_env([(key, expand(value, variables)) for key, value in instruction.value])
                record(instruction)
            elif op == "LABEL":
                config.set_labels([(key, expand(value, variables)) for key, value in instruction.value])
                record(instruction)
            elif op == "HEALTHCHECK":
                health = instruction.value
                if health.command is None:
                    config.runtime["Healthcheck"] = {"Test": ["NONE"]}
                else:
                    test = (["CMD-SHELL", health.command.value] if health.command.shell_form
                            else ["CMD"] + list(health.command.value))
                    config.runtime["Healthcheck"] = {"Test": test, **dict(health.options)}
                record(instruction)
            elif op == "STOPSIGNAL":
                signal = expand(instruction.value, variables).upper()
                if not valid_stop_signal(signal):
                    raise BuildError("Invalid STOPSIGNAL after expansion")
                config.runtime["StopSignal"] = signal
                record(instruction)
            elif op == "MAINTAINER":
                config.value["author"] = instruction.value
                record(instruction)
            elif op == "ONBUILD":
                config.runtime.setdefault("OnBuild", []).append(instruction.value.raw)
                record(instruction)
            elif op == "EXPOSE":
                exposed = []
                for port in instruction.value:
                    value = expand(port, variables)
                    match = re.fullmatch(r"([0-9]{1,5})(?:/(tcp|udp|sctp))?", value, re.I)
                    if match is None or not 1 <= int(match.group(1)) <= 65535:
                        raise BuildError("Invalid EXPOSE port after expansion: " + value)
                    exposed.append("{}/{}".format(int(match.group(1)), (match.group(2) or "tcp").lower()))
                config.set_exposed(exposed)
                record(instruction)
            elif op == "VOLUME":
                volume_paths = [container_path(expand(path, variables)) for path in instruction.value]
                layer_path = workspace / ("instruction-{}.tar".format(number))
                layer_path, diff_id = resolve_layer(
                    instruction, layer_path,
                    lambda target: layer_builder.volume_layer(volume_paths, target))
                accept_layer(layer_path, diff_id)
                config.set_volumes(volume_paths)
                record(instruction, diff_id)
            elif op == "CMD":
                value = list(shell) + [instruction.value.value] if instruction.value.shell_form else list(instruction.value.value)
                config.set_command("Cmd", value)
                stage_cmd_set = True
                record(instruction)
            elif op == "ENTRYPOINT":
                value = list(shell) + [instruction.value.value] if instruction.value.shell_form else list(instruction.value.value)
                config.set_command("Entrypoint", value)
                if not stage_cmd_set:
                    config.runtime["Cmd"] = None
                record(instruction)
            elif op == "USER":
                user = expand(instruction.value, variables)
                if not user or user.startswith(":") or user.endswith(":") or user.count(":") > 1:
                    raise BuildError("Invalid USER after expansion")
                config.runtime["User"] = user
                record(instruction)
            elif op == "SHELL":
                shell = list(instruction.value)
                config.runtime["Shell"] = shell
                record(instruction)
            elif op == "WORKDIR":
                workdir = container_path(expand(instruction.value, variables), workdir)
                layer_path = workspace / ("instruction-{}.tar".format(number))
                layer_path, diff_id = resolve_layer(
                    instruction, layer_path,
                    lambda target: layer_builder.workdir_layer(workdir, target, user))
                accept_layer(layer_path, diff_id)
                config.set_workdir(workdir)
                record(instruction, diff_id)
            elif op in ("COPY", "ADD"):
                transfer = replace(instruction.value,
                    sources=tuple(expand(item, variables) for item in instruction.value.sources),
                    destination=expand(instruction.value.destination, variables),
                    from_stage=(expand(instruction.value.from_stage, variables)
                                if instruction.value.from_stage is not None else None),
                    exclude=tuple(expand(pattern, variables) for pattern in instruction.value.exclude),
                    chown=(expand(instruction.value.chown, variables)
                           if instruction.value.chown is not None else None),
                    chmod=(int(expand(instruction.value.chmod, variables), 8)
                           if isinstance(instruction.value.chmod, str) and
                           re.fullmatch(r"[0-7]{3,4}", expand(instruction.value.chmod, variables))
                           else instruction.value.chmod),
                    inline=(expand(instruction.value.inline, variables)
                            if instruction.value.inline is not None and instruction.value.inline_expand
                            else instruction.value.inline))
                if isinstance(transfer.chmod, str):
                    raise BuildError("Invalid COPY/ADD --chmod after expansion")
                layer_path = workspace / ("instruction-{}.tar".format(number))
                # --link 层不继承先前 rootfs 状态；复制输入不变时，
                # 替换基础镜像后仍可复用这个独立层。
                transfer_builder = (LayerBuilder(context, RootFSIndex(), source_date_epoch, reporter)
                                    if transfer.link else layer_builder)
                remote_url = next((item for item in transfer.sources
                                   if item.startswith(("http://", "https://"))), None)
                if remote_url is not None and (op != "ADD" or len(transfer.sources) != 1 or
                                               transfer.from_stage is not None or transfer.inline is not None):
                    raise UnsupportedInstruction("Remote ADD requires one HTTP(S) source")
                if remote_url is not None and not allow_remote_add:
                    raise BuildError("Remote ADD is disabled for this offline build")
                if transfer.checksum is not None and remote_url is None:
                    raise UnsupportedInstruction("ADD --checksum is only for remote HTTP(S) sources")
                if transfer.parents and remote_url is not None:
                    raise UnsupportedInstruction("Remote ADD --parents is unsupported")
                if transfer.link and transfer.chown is not None and not re.fullmatch(r"[0-9]+(?::[0-9]+)?", transfer.chown):
                    raise UnsupportedInstruction("COPY/ADD --link requires numeric --chown in this builder")
                source_stage = None
                if transfer.from_stage is not None:
                    reference = transfer.from_stage
                    index = stage_names.get(reference.lower())
                    if index is None and reference.isdecimal() and int(reference) < len(completed):
                        index = int(reference)
                    if index is None or index >= len(completed):
                        raise BuildError("COPY --from must name a completed stage: " + reference)
                    source_stage = completed[index]
                fingerprint = (transfer.checksum if remote_url is not None else
                               source_stage.state_key if source_stage is not None else
                               cache_key("inline", transfer.inline) if transfer.inline is not None else
                               transfer_builder.fingerprint_sources(transfer) if cache is not None else None)
                if transfer.link:
                    # 独立层虽不依赖基础 rootfs，仍依赖展开后的目标与权限参数、
                    # WORKDIR 和规范化时间戳，缓存键不能遗漏这些输入。
                    fingerprint = [fingerprint, asdict(transfer), workdir, source_date_epoch]
                if remote_url is not None:
                    def produce_remote(target):
                        from remote_add import fetch
                        remote_file = workspace / ("remote-{}".format(number))
                        filename, _digest = fetch(remote_url, remote_file, transfer.checksum, reporter)
                        return transfer_builder.remote_layer(transfer, remote_file, filename, workdir, target)
                    # 未固定摘要的远端 URL 内容可能变化，
                    # 即使 Dockerfile 和上下文完全相同也不能直接复用旧层。
                    if transfer.checksum is None:
                        if reporter is not None:
                            reporter.cache_miss()
                            reporter.log("Remote ADD without checksum: layer cache bypassed")
                        if cache_stats is not None:
                            cache_stats["misses"] += 1
                        diff_id = produce_remote(layer_path)
                    else:
                        layer_path, diff_id = resolve_layer(instruction, layer_path, produce_remote,
                                                           fingerprint, independent=transfer.link)
                else:
                    layer_path, diff_id = resolve_layer(
                        instruction, layer_path,
                        (lambda target: transfer_builder.inline_layer(transfer, workdir, target))
                        if transfer.inline is not None else
                        (lambda target: transfer_builder.transfer_from_rootfs(source_stage.rootfs,
                                                                             transfer, workdir, target))
                        if source_stage is not None else
                        (lambda target: transfer_builder.transfer_layer(transfer, workdir, target,
                                                                     add=(op == "ADD"))),
                        fingerprint, independent=transfer.link)
                accept_layer(layer_path, diff_id)
                record(instruction, diff_id)
            elif op == "RUN":
                run_command = instruction.value.command
                effective_network = (run_network if instruction.value.network in (None, "default")
                                     else instruction.value.network)
                if effective_network == "host" and run_network != "host":
                    raise UnsupportedInstruction("RUN --network=host requires CLI --network=host")
                if instruction.value.security == "insecure" and run_sandbox != "legacy":
                    raise UnsupportedInstruction("RUN --security=insecure requires CLI --run-sandbox=legacy")
                effective_sandbox = ("hardened" if instruction.value.security == "sandbox" and
                                     run_sandbox == "legacy" else run_sandbox)
                run_mounts = prepare_run_mounts(instruction.value.mounts, secret_sources,
                                                cache_root, workspace, "linux/" + config.value["architecture"])
                current_architecture = config.value["architecture"]
                if host_architecture() != current_architecture:
                    if not allow_emulated_run:
                        raise UnsupportedInstruction(
                            "Foreign-architecture RUN needs --allow-emulated-run and preconfigured binfmt_misc/QEMU")
                    if platform.system() != "Linux":
                        raise UnsupportedInstruction("Foreign-architecture RUN must execute on Linux/WSL")
                    require_foreign_binfmt(current_architecture)
                def execute_run(target):
                    """准备实际 rootfs、执行 RUN，再由 OverlayFS 导出本次文件差异。

                    临时 cache/secret 挂载目标不写入层；此函数只在指令缓存未命中时执行。
                    """
                    nonlocal materialized
                    from executor import RunExecutor
                    from overlay import OverlayManager

                    runner = (RunExecutor(effective_network) if effective_sandbox == "legacy" else
                              RunExecutor(effective_network, effective_sandbox))
                    if reporter is not None:
                        runner.log = reporter.log
                    if materialized is None:
                        materialized = (RootFSMaterializer(workspace / "rootfs", rootless=True)
                                        if effective_sandbox == "rootless" else
                                        RootFSMaterializer(workspace / "rootfs"))
                        for layer_index, existing_layer in enumerate(layers, 1):
                            materialized.apply(existing_layer)
                            if reporter is not None:
                                reporter.progress(layer_index, len(layers), "Preparing rootfs", unit="layers")
                    snapshot = workspace / ("run-{}".format(number))
                    snapshot.mkdir()
                    overlay = (OverlayManager(materialized.root, snapshot, rootless=True)
                               if effective_sandbox == "rootless" else
                               OverlayManager(materialized.root, snapshot))
                    overlay.source_date_epoch = source_date_epoch
                    env = {}
                    for item in config.runtime.get("Env") or []:
                        if isinstance(item, str) and "=" in item:
                            key, value = item.split("=", 1)
                            env[key] = value
                    env.setdefault("SOURCE_DATE_EPOCH", str(source_date_epoch))
                    env.update({key: value for key, value in stage_args.items() if key not in env})
                    if run_mounts:
                        overlay.excluded_paths = {target.lstrip("/") for _, _, target in run_mounts}
                        with locked_caches(run_mounts):
                            runner.execute(overlay, run_command, env, workdir, user, shell, run_mounts)
                    else:
                        runner.execute(overlay, run_command, env, workdir, user, shell)
                    return overlay.to_layer(target)

                layer_path = workspace / ("instruction-{}.tar".format(number))
                if any(item["type"] == "secret" for item in instruction.value.mounts):
                    diff_id = execute_run(layer_path)
                    if reporter is not None:
                        reporter.cache_miss()
                    if cache_stats is not None:
                        cache_stats["misses"] += 1
                else:
                    layer_path, diff_id = resolve_layer(
                        instruction, layer_path, execute_run,
                        ["network", effective_network, "sandbox", effective_sandbox,
                         "architecture", current_architecture, "emulation", allow_emulated_run,
                         "arguments", stage_args])
                accept_layer(layer_path, diff_id)
                record(instruction, diff_id)
            else:
                # 正常路径已由解析器拒绝未知指令；此处用于检测内部指令分派错误。
                raise BuildError("Internal parser error: " + op)
            if reporter is not None:
                if id(instruction) in trigger_ids:
                    reporter.phase_success("ONBUILD " + instruction.raw.splitlines()[0])
                else:
                    reporter.step_success("metadata only" if op in
                                          ("ARG", "ENV", "LABEL", "EXPOSE", "CMD", "ENTRYPOINT", "USER", "SHELL",
                                           "HEALTHCHECK", "STOPSIGNAL", "MAINTAINER", "ONBUILD")
                                          else "done")
        if stage_index < target_index:
            raise BuildError("Target stage was not built")
        products = []
        export_config, export_raw = config.export()
        export_image = (BaseImage(export_config, layers, [tag], {}, config_raw=export_raw,
                                  manifest_raw=config.manifest_raw)
                        if export_raw is not None else None)

        def write_product(writer, partial, progress=None):
            # 无变化构建走强制保留字节的转存接口；派生配置才允许重新序列化。
            if export_image is not None:
                writer.write_image(partial, export_image, tag, progress=progress)
            else:
                writer.write_new(partial, export_config, layers, tag, progress=progress)

        if reporter is not None:
            reporter.phase_start("Exporting image archive")
        if image_format in ("docker", "both"):
            partial = workspace / "docker-image.tar"
            writer = ImageArchiveWriter(source_date_epoch)
            with reporter.timed("Write Docker archive") if reporter is not None else nullcontext():
                if reporter is None:
                    write_product(writer, partial)
                else:
                    write_product(writer, partial,
                                 progress=lambda current, total, label:
                                 reporter.progress(current, total, "Writing Docker layer"))
            with reporter.timed("Verify Docker archive") if reporter is not None else nullcontext():
                writer.verify(partial, tag)
            products.append(("docker", partial, output))
        if image_format in ("oci", "both"):
            partial = workspace / "oci-image.tar"
            writer = OCIImageWriter(source_date_epoch)
            with reporter.timed("Write OCI archive") if reporter is not None else nullcontext():
                if reporter is None:
                    write_product(writer, partial)
                else:
                    write_product(writer, partial,
                                 progress=lambda current, total, label:
                                 reporter.progress(current, total, "Writing OCI layer"))
            with reporter.timed("Verify OCI archive") if reporter is not None else nullcontext():
                writer.verify(partial, tag)
            products.append(("oci", partial, oci_output if image_format == "both" else output))

        if attest:
            sbom_bytes = canonical_json(create_sbom(rootfs, export_config, tag, source_date_epoch))
            provenance_bytes = canonical_json(create_provenance(
                products, sbom_bytes, dockerfile, context, base_inputs, tag,
                str(target_stage) if target_stage is not None else None,
                enable_run, run_network, source_date_epoch, run_sandbox,
                target_platform, allow_emulated_run))
            sbom_part = workspace / "sbom.spdx.json"
            provenance_part = workspace / "provenance.json"
            sbom_part.write_bytes(sbom_bytes)
            provenance_part.write_bytes(provenance_bytes)
            products.extend((("sbom", sbom_part, sidecars[0]),
                             ("provenance", provenance_part, sidecars[1])))
            if seed is not None:
                envelope_part = workspace / "provenance.dsse.json"
                envelope_part.write_bytes(canonical_json(sign_envelope(provenance_bytes, seed)))
                products.append(("signature", envelope_part, sidecars[2]))

        published = []
        try:
            for _, partial, destination in products:
                publish_new_file(partial, destination)
                published.append(destination)
        except Exception:
            for destination in published:
                unlink_missing(destination)
            raise
        if reporter is not None:
            reporter.phase_success("Exporting image archive")
        if build_info is not None:
            build_info.update(layers=len(layers), platform="linux/" + config.value["architecture"],
                              oci_output=str(oci_output) if image_format == "both" else None)
    return output
