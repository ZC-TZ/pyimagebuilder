"""执行网页交付任务；源归档和目标由工作台确认后传入。"""

import hashlib
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

from cas_store import CASStore
from errors import BuildError
from file_publish import publication_transaction
from image_cli import (_select_tag, history_archive, load_archive, tag_archive,
                       verify_archive)
from image_reader import ImageArchiveReader, parse_image_json
from oci_writer import OCIImageWriter
from registry import parse_reference, push, push_index
from rootfs_archive import export_rootfs, flatten_image
from settings import cache_directory, load_settings, repository_settings
from network_tls import tls_options
from web_jobs import file_stamp, open_artifact


def push_destination(settings, value):
    """仅对已配置的标准 Registry 使用配置中的鉴权。"""
    reference = parse_reference(value)
    if reference.digest or ":" not in value.rsplit("/", 1)[-1]:
        raise BuildError("推送目标必须显式指定标签，例如 harbor.example/project/image:1")
    entry = repository_settings(settings, reference.full)
    if reference.registry not in settings.get("repositories", {}) or entry.get("source", "registry") != "registry":
        raise BuildError("推送目标必须是 config.json 中已配置的 Registry / Harbor")
    return reference.full


def _snapshot_archive(plan, path, snapshot):
    """验证实际写入私有快照的字节，后续操作始终从该快照读取。"""
    item = plan.get("record")
    expected_digest = item["sha256"] if item is not None else plan.get("digest")
    digest = hashlib.sha256() if expected_digest is not None else None
    if digest is not None:
        print("正在校验源归档 SHA-256…", flush=True)
    with open_artifact(path) as source, snapshot.open("xb") as target:
        stamp = file_stamp(os.fstat(source.fileno()))
        size = stamp[2]
        changed = size != item["bytes"] if item is not None else stamp != tuple(plan["stamp"])
        if changed:
            raise BuildError("源归档已变更，请重新选择")
        remaining = size
        while remaining:
            block = source.read(min(remaining, 4 * 1024 * 1024))
            if not block:
                raise BuildError("源归档在读取期间被截断")
            if digest is not None:
                digest.update(block)
            target.write(block)
            remaining -= len(block)
        if file_stamp(os.fstat(source.fileno())) != stamp:
            raise BuildError("源归档在读取期间被修改")
        if digest is not None and "sha256:" + digest.hexdigest() != expected_digest:
            raise BuildError("源归档 SHA-256 不匹配")
    return size


def _verify_oci(path, tag, platform):
    writer = OCIImageWriter()
    snapshot = writer.verify(path, tag)
    index = json.loads(snapshot["index.json"])
    actual = index["manifests"][0]["platform"]
    if actual["os"] + "/" + actual["architecture"] != platform:
        raise BuildError("OCI 归档平台与记录不符")
    return {"archive": str(path), "tag": tag, "platform": platform, "format": "oci"}


def _deliver_index(plan, settings, snapshot):
    from multiarch import verify
    tag = plan["tag"]
    if plan["action"] == "verify":
        verify(snapshot, tag)
        return {"tag": tag, "platforms": ["linux/amd64", "linux/arm64"], "result": "校验通过"}

    destination = push_destination(settings, plan["destination"])
    connection = repository_settings(settings, destination)
    password = (os.environ.get(connection.get("passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD"))
                if connection.get("username") is not None else None)
    print("正在推送 amd64 和 arm64 到 " + destination, flush=True)
    result = push_index(snapshot, destination, connection.get("username"), password,
                        connection.get("caFile"), connection.get("insecureHttp", False),
                        connection.get("authHost"), overwrite=False, expected_tag=tag,
                        **tls_options(connection.get("verifyTls", True)))
    return {"reference": destination, "digest": result, "platforms": ["linux/amd64", "linux/arm64"]}


def _track_delivery_outputs(published, result):
    """外层事务只回收实际登记过的新归档和报告。"""
    for key in ("output", "report"):
        path = result.get(key)
        if path and path != "-":
            published.track_observed(path)


def execute(plan):
    action = plan["action"]
    if action == "lock-inputs":
        from build_args import arguments
        from hermetic import write_lock
        sources = {key.replace("-", "_"): Path(path) for key, path in plan["sources"].items()}
        output = write_lock(Path(plan["output"]), Path(plan["dockerfile"]), Path(plan["context"]),
                            tag=plan["tag"], target_platform=plan["platform"], target_stage=plan["target"],
                            image_format=plan["format"], source_date_epoch=plan["epoch"],
                            build_args=arguments(plan["build_args"]), **sources)
        return {"tag": plan["tag"], "output": str(output)}
    if action == "attest-verify":
        from attest import verify_bundle
        inputs = {name.replace("-", "_"): path for name, path in plan["inputs"].items()}
        verify_bundle(inputs["public_key"], inputs["provenance"], inputs["envelope"], inputs["sbom"],
                      inputs.get("docker_archive"), inputs.get("oci_archive"))
        return {"签名": "有效", "镜像归档摘要": "匹配", "SBOM 摘要": "匹配"}
    if action == "multiarch":
        from multiarch import combine
        archives = plan["archives"]
        output = combine(archives["amd64"], archives["arm64"], plan["output"], plan["tag"], plan["epoch"],
                         expected_manifests=plan["manifests"])
        return {"tag": plan["tag"], "output": str(output), "platforms": ["linux/amd64", "linux/arm64"]}
    if action == "profile-test":
        from web_maintenance import test_profile
        return test_profile(Path(plan["config"]), plan["form"], plan["platform"])
    settings, _ = load_settings(plan.get("config"))
    if action == "repo-test":
        from web_repository import diagnose
        return diagnose(settings, plan["host"], plan["reference"])
    if action == "cache-clean":
        from web_maintenance import cache_root, clean_cache
        root = cache_root(settings)
        if str(root) != plan["cache"]:
            raise BuildError("缓存目录已变化，请重新预览")
        return clean_cache(root, plan["days"], plan["fingerprint"])
    if action == "image-clean":
        from web_maintenance import clean_old_images, image_store_root
        root = image_store_root(settings)
        if str(root) != plan["root"]:
            raise BuildError("镜像库位置已变化，请重新预览")
        return clean_old_images(root, plan["keys"], plan["fingerprint"])
    store = cache_directory(settings, "imageStore", None)
    if plan["format"] == "cas":
        return _deliver_cas(plan, settings, store)
    source = Path(plan["archive"])
    with publication_transaction() as published, \
            tempfile.TemporaryDirectory(prefix="pyimagebuilder-deliver-") as temporary:
        workspace = Path(temporary)
        snapshot = workspace / "source.tar"
        size = _snapshot_archive(plan, source, snapshot)
        if plan["format"] == "oci-index" and action != "compress":
            return _deliver_index(plan, settings, snapshot)
        result = _deliver_archive(plan, settings, store, snapshot, size, workspace)
        if result.get("archive") == str(snapshot):
            result["archive"] = str(source)
        _track_delivery_outputs(published, result)
        return result


def _deliver_cas(plan, settings, store):
    from image_store import default_store, docker_archive_tag
    root = Path(store or default_store()).resolve()
    if str(root) != plan["store"]:
        raise BuildError("镜像库位置已变化，请重新选择")
    cas = CASStore(root)
    tag, platform, source = plan["tag"], plan["platform"], plan["source"]
    action = plan["action"]
    with publication_transaction() as published, cas.ref_lock(tag, platform, source):
        raw = cas.snapshot_ref(tag, platform, source)
        if raw is None or parse_image_json(raw, "CAS delivery ref").get("manifest") != plan["manifest"]:
            raise BuildError("镜像标签已变更，请重新选择")
        if action == "verify":
            reference = cas.resolve(tag, platform, source)
            return {"tag": tag, "platform": platform, "source": source,
                    "manifest": reference["manifest"], "layers": len(reference["layers"])}
        if action == "save":
            output = cas.export_docker(tag, plan["output"], platform, source)
            result = {"tag": tag, "platform": platform, "source": source, "output": str(output)}
        elif action == "rootfs-export":
            result = export_rootfs(tag, plan["output"], platform=platform, store=root, kind=source, stored=True)
        elif action == "flatten":
            result = flatten_image(tag, plan["output"], platform=platform, store=root, kind=source,
                                   changes=plan["changes"], stored=True)
        elif action == "slim":
            result = _slim(plan, tag, store=root, kind=source, stored=True)
        elif action in ("history", "analyze", "optimize"):
            with tempfile.TemporaryDirectory(prefix="pyimagebuilder-delivery-cas-") as temporary:
                workspace = Path(temporary)
                archive = cas.export_docker(tag, workspace / "source.tar", platform, source)
                result = _deliver_archive(dict(plan, format="docker", tag=docker_archive_tag(tag)),
                                          settings, store, archive, archive.stat().st_size, workspace)
                _track_delivery_outputs(published, result)
                return result
        else:
            raise BuildError("仅有 CAS 数据的镜像请先导出归档")
        _track_delivery_outputs(published, result)
        return result


def _deliver_archive(plan, settings, store, path, size, workspace):
    action, tag, platform = plan["action"], plan["tag"], plan["platform"]
    is_oci = plan["format"] == "oci"
    if action == "compress":
        return _compress(plan, path)
    if action == "slim":
        return _slim(plan, path, tag=tag)
    if action == "verify":
        return _verify_oci(path, tag, platform) if is_oci else verify_archive(path, tag, platform)
    if action == "history" and not is_oci:
        return history_archive(path, tag, platform)
    if action == "history":
        from image_reader import image_history
        snapshot = OCIImageWriter().verify(path, tag)
        index = json.loads(snapshot["index.json"])
        descriptor = index["manifests"][0]
        if descriptor["platform"]["os"] + "/" + descriptor["platform"]["architecture"] != platform:
            raise BuildError("OCI 归档平台与记录不符")
        manifest = json.loads(snapshot["blobs/sha256/" + descriptor["digest"].split(":", 1)[1]])
        config = json.loads(snapshot["blobs/sha256/" + manifest["config"]["digest"].split(":", 1)[1]])
        history = image_history(config, len(manifest["layers"]))
        return {"tag": tag, "format": "oci", "history": list(reversed(history or []))}
    if action == "analyze":
        from optimizer import analyze_image
        return analyze_image(path, tag, platform=platform)
    if action == "optimize":
        return _optimize(plan, path)
    if action == "rootfs-export":
        return export_rootfs(path, plan["output"], tag=tag, platform=platform)
    if action == "flatten":
        result = flatten_image(path, plan["output"], tag=tag, platform=platform,
                               format="oci" if is_oci else "docker", changes=plan["changes"])
        result["before_bytes"] = size
        result["saved_bytes"] = result["before_bytes"] - result["after_bytes"]
        return result
    if action == "load":
        if is_oci:
            from image_store import default_store
            return CASStore(store or default_store()).import_oci(path, tag, platform, "local")
        return load_archive(path, store, tag, platform)
    if action == "tag" and not is_oci:
        return tag_archive(path, plan["new_tag"], plan["output"], tag, platform=platform)
    if action == "push":
        destination = push_destination(settings, plan["destination"])
        connection = repository_settings(settings, destination)
        password = os.environ.get(connection.get("passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD"))
        archive = path
        if is_oci:
            _verify_oci(path, tag, platform)
        else:
            print("正在为选中的标签和平台准备 OCI 归档…", flush=True)
            _select_tag(path, tag)
            selected = ImageArchiveReader(path, workspace / "layers").read(tag, platform)
            archive = workspace / "delivery.oci.tar"
            OCIImageWriter().write_image(archive, selected, tag)
        print("正在推送到 " + destination, flush=True)
        digest = push(archive, destination, connection.get("username"), password,
                      connection.get("caFile"), connection.get("insecureHttp", False),
                      connection.get("authHost"), overwrite=False,
                      **tls_options(connection.get("verifyTls", True)))
        return {"reference": destination, "digest": digest}
    raise BuildError("该归档不支持所选操作")


def _optimize(plan, source):
    """让较长的优化任务报告阶段、候选层决定和最终复核，收尾失败也回滚产物。"""
    from optimizer import optimize_image
    from progress import make_reporter
    reporter = make_reporter("json", stream=sys.stdout)
    try:
        with publication_transaction() as published:
            reporter.start(plan["tag"], plan["platform"], mode="optimize")
            result = optimize_image(source, plan["output"], plan["tag"], reporter=reporter,
                                    platform=plan["platform"])
            _track_delivery_outputs(published, result)
            reporter.success(output=result["output"], size=result["after_bytes"])
        return result
    except BaseException as exc:
        reporter.fail(exc)
        raise
    finally:
        reporter.close()


def _compress(plan, source):
    """压缩已经验证来源摘要的私有快照，记录原路径，报告与归档共同回滚。"""
    from compress import compress_archive
    from progress import make_reporter
    reporter = make_reporter("json", stream=sys.stdout)
    try:
        with publication_transaction() as published:
            reporter.start(plan["tag"], plan["platform"], mode="compress")
            result = compress_archive(source, plan["output"], compress_level=plan["compress_level"],
                                      upload_limit=plan["upload_limit"], report=plan["report"],
                                      reporter=reporter, source_label=plan["archive"])
            _track_delivery_outputs(published, result)
            reporter.success(output=result["output"], size=result["after_bytes"])
        return result
    except BaseException as exc:
        reporter.fail(exc)
        raise
    finally:
        reporter.close()


def _slim(plan, source, **kwargs):
    """完整决策写报告文件，任务日志只写摘要，避免大报告挤掉实际错误。"""
    from slim import slim_image
    from progress import make_reporter
    options = {key: plan[key] for key in ("deletes", "keeps", "auto_protect", "analyze_only", "report",
                                         "gzip_output", "compress_level", "upload_limit")}
    reporter = make_reporter("json", stream=sys.stdout)
    try:
        reporter.start(plan["tag"], plan["platform"], mode="slim")
        with publication_transaction() as published:
            result = slim_image(source, plan["output"], platform=plan["platform"], reporter=reporter,
                                **options, **kwargs)
            _track_delivery_outputs(published, result)
            decision = result.pop("decision")
            result.update(removed_paths=len(decision["removed_paths"]),
                          protected_paths=len(decision["protected_paths"]),
                          removed_unique_bytes=decision["removed_unique_bytes"])
            if result["under_upload_limit"] is False:
                print("警告：解压后归档仍达到上传限制，gzip 不能绕过该限制。", flush=True)
            reporter.success(image=result["tag"], output=result["output"], size=result.get("after_bytes"))
    except BaseException as exc:
        reporter.fail(exc)
        raise
    finally:
        reporter.close()
    return result


def main():
    try:
        plan = json.loads(sys.argv[1])
        with publication_transaction() as published:
            result = execute(plan)
            _track_delivery_outputs(published, result)
            metrics = {key: result[key] for key in ("before_bytes", "after_bytes", "saved_bytes",
                       "uncompressed_archive_bytes", "upload_limit", "removed_paths", "protected_paths", "removed_unique_bytes",
                       "archive_bytes", "hidden_file_bytes", "duplicate_groups", "deleted", "bytes")
                       if type(result.get(key)) is int}
            if isinstance(result.get("large_layers"), list):
                metrics["large_layers"] = result["large_layers"][:50]
            if metrics:
                print(json.dumps({"web_metrics": metrics}))
            print(json.dumps(result, ensure_ascii=plan["action"] not in ("repo-test", "attest-verify"), indent=2), flush=True)
        return 0
    except (BuildError, OSError, ValueError, KeyError, TypeError, RuntimeError, tarfile.TarError) as exc:
        print("交付任务失败：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
