"""执行网页交付任务；源归档和目标由工作台确认后传入。"""

import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from cas_store import CASStore
from errors import BuildError
from image_cli import (_select_tag, history_archive, load_archive, tag_archive,
                       verify_archive)
from image_reader import ImageArchiveReader
from oci_writer import OCIImageWriter
from registry import parse_reference, push, push_index
from settings import cache_directory, load_settings, repository_settings
from web_artifacts import verified_artifact
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


def _stamp(path):
    with open_artifact(path) as stream:
        return file_stamp(os.fstat(stream.fileno()))


def _check_source(plan, path):
    item = plan.get("record")
    if item is not None:
        print("正在校验历史归档 SHA-256…", flush=True)
        state, stream = verified_artifact(item)
        if stream is None:
            raise BuildError("历史归档无法通过摘要校验：" + state)
        with stream:
            stamp = file_stamp(os.fstat(stream.fileno()))
    else:
        stamp = tuple(plan["stamp"])
    if _stamp(path) != stamp:
        raise BuildError("源归档已变更，请重新选择")
    return stamp


def _verify_oci(path, tag, platform):
    writer = OCIImageWriter()
    snapshot = writer.verify(path, tag)
    index = json.loads(snapshot["index.json"])
    actual = index["manifests"][0]["platform"]
    if actual["os"] + "/" + actual["architecture"] != platform:
        raise BuildError("OCI 归档平台与记录不符")
    return {"archive": str(path), "tag": tag, "platform": platform, "format": "oci"}


def _deliver_index(plan, settings):
    from multiarch import verify
    item = plan["record"]
    path = Path(item["path"])
    tag = plan["tag"]
    if plan["action"] == "verify":
        _, stream = verified_artifact(item)
        if stream is None:
            raise BuildError("多架构归档已移走或摘要不匹配")
        with stream:
            verify(path, tag, fileobj=stream)
        return {"tag": tag, "platforms": ["linux/amd64", "linux/arm64"], "result": "校验通过"}

    destination = push_destination(settings, plan["destination"])
    connection = repository_settings(settings, destination)
    password = (os.environ.get(connection.get("passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD"))
                if connection.get("username") is not None else None)
    print("正在核对多架构归档摘要…", flush=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-deliver-index-") as temporary:
        snapshot = Path(temporary) / "multi.oci.tar"
        digest = hashlib.sha256()
        remaining = item["bytes"]
        with open_artifact(path) as source, snapshot.open("xb") as target:
            if os.fstat(source.fileno()).st_size != remaining:
                raise BuildError("多架构归档大小已变更")
            while remaining:
                block = source.read(min(remaining, 4 * 1024 * 1024))
                if not block:
                    raise BuildError("多架构归档在读取期间被截断")
                digest.update(block)
                target.write(block)
                remaining -= len(block)
        if "sha256:" + digest.hexdigest() != item["sha256"]:
            raise BuildError("多架构归档摘要不匹配")
        print("正在推送 amd64 和 arm64 到 " + destination, flush=True)
        result = push_index(snapshot, destination, connection.get("username"), password,
                            connection.get("caFile"), connection.get("insecureHttp", False),
                            connection.get("authHost"), overwrite=False, expected_tag=tag)
    return {"reference": destination, "digest": result, "platforms": ["linux/amd64", "linux/arm64"]}


def execute(plan):
    action = plan["action"]
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
    if plan["format"] == "oci-index":
        return _deliver_index(plan, settings)
    store = cache_directory(settings, "imageStore", None)
    tag, platform = plan["tag"], plan["platform"]
    if plan["format"] == "cas":
        from image_store import default_store
        cas = CASStore(store or default_store())
        if action == "verify":
            reference = cas.resolve(tag, platform, plan["source"])
            return {"tag": tag, "platform": platform, "source": plan["source"],
                    "manifest": reference["manifest"], "layers": len(reference["layers"])}
        if action == "save":
            output = cas.export_docker(tag, plan["output"], platform, plan["source"])
            return {"tag": tag, "platform": platform, "source": plan["source"], "output": str(output)}
        if action == "flatten":
            from rootfs_archive import flatten_image
            return flatten_image(tag, plan["output"], platform=platform, store=store, kind=plan["source"])
        if action in ("history", "analyze", "optimize"):
            with tempfile.TemporaryDirectory(prefix="pyimagebuilder-delivery-cas-") as temporary:
                archive = cas.export_docker(tag, Path(temporary) / "source.tar", platform, plan["source"])
                from image_store import docker_archive_tag
                return execute(dict(plan, archive=str(archive), format="docker", tag=docker_archive_tag(tag),
                                    stamp=_stamp(archive)))
        raise BuildError("仅有 CAS 数据的镜像请先导出归档")
    path = Path(plan["archive"])
    stamp = _check_source(plan, path)
    is_oci = plan["format"] == "oci"
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
    if action in ("analyze", "optimize"):
        from optimizer import analyze_image, optimize_image
        if is_oci:
            _verify_oci(path, tag, platform)
        else:
            verify_archive(path, tag, platform)
        if _stamp(path) != stamp:
            raise BuildError("源归档已变更")
        if action == "analyze":
            return analyze_image(path, tag)
        return optimize_image(path, plan["output"], tag)
    if action == "flatten":
        from rootfs_archive import flatten_image
        result = flatten_image(path, plan["output"], tag=tag, platform=platform,
                               format="oci" if is_oci else "docker")
        result.update(before_bytes=stamp[2], after_bytes=Path(plan["output"]).stat().st_size)
        result["saved_bytes"] = result["before_bytes"] - result["after_bytes"]
        return result
    if action == "load":
        if is_oci:
            from image_store import default_store
            return CASStore(store or default_store()).import_oci(path, tag, platform, "local")
        return load_archive(path, store, tag, platform)
    if action == "tag" and not is_oci:
        # 当前改标签仅支持单镜像 Docker tar；先核对用户选中的平台。
        verify_archive(path, tag, platform)
        if _stamp(path) != stamp:
            raise BuildError("源归档已变更")
        return tag_archive(path, plan["new_tag"], plan["output"], tag)
    if action == "push":
        destination = push_destination(settings, plan["destination"])
        connection = repository_settings(settings, destination)
        password = os.environ.get(connection.get("passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD"))
        with tempfile.TemporaryDirectory(prefix="pyimagebuilder-deliver-") as temporary:
            workspace = Path(temporary)
            archive = workspace / "delivery.oci.tar"
            if is_oci:
                # 网络发布使用私有快照，上传中替换原路径不会改变已确认的内容。
                with open_artifact(path) as source, archive.open("xb") as target:
                    if file_stamp(os.fstat(source.fileno())) != stamp:
                        raise BuildError("源归档已变更，未推送")
                    shutil.copyfileobj(source, target, 4 * 1024 * 1024)
                    if file_stamp(os.fstat(source.fileno())) != stamp:
                        raise BuildError("源归档已变更，未推送")
                _verify_oci(archive, tag, platform)
            else:
                print("正在为选中的标签和平台准备 OCI 归档…", flush=True)
                _select_tag(path, tag)
                selected = ImageArchiveReader(path, workspace / "layers").read(tag, platform)
                OCIImageWriter().write_image(archive, selected, tag)
            if _stamp(path) != stamp:
                raise BuildError("源归档已变更，未推送")
            print("正在推送到 " + destination, flush=True)
            digest = push(archive, destination, connection.get("username"), password,
                          connection.get("caFile"), connection.get("insecureHttp", False),
                          connection.get("authHost"), overwrite=False)
        return {"reference": destination, "digest": digest}
    raise BuildError("该归档不支持所选操作")


def main():
    try:
        plan = json.loads(sys.argv[1])
        result = execute(plan)
        metrics = {key: result[key] for key in ("before_bytes", "after_bytes", "saved_bytes",
                   "archive_bytes", "hidden_file_bytes", "duplicate_groups", "deleted", "bytes")
                   if type(result.get(key)) is int}
        if isinstance(result.get("large_layers"), list):
            metrics["large_layers"] = result["large_layers"][:50]
        if metrics:
            print(json.dumps({"web_metrics": metrics}))
        print(json.dumps(result, ensure_ascii=plan["action"] not in ("repo-test", "attest-verify"), indent=2))
        return 0
    except (BuildError, OSError, ValueError, KeyError, TypeError, RuntimeError, tarfile.TarError) as exc:
        print("交付任务失败：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
