#!/usr/bin/env python3
"""无需第三方依赖的 OCI/Docker Registry v2 客户端，支持私有 Harbor。"""

import argparse
import base64
import copy
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import sys
import tarfile
import tempfile
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import parse_http_list

from errors import ArchiveError, BuildError
from compat import unlink_missing
from cas_store import docker_archive_tag
from image_reader import (BaseImage, ImageArchiveReader, archive_members, digest_hex,
                          image_history, image_json_bytes, same_json_value, sha256_file)
from image_writer import ImageArchiveWriter
from file_publish import publish_new_file
from oci_writer import (CONFIG_TYPE, INDEX_TYPE, LAYER_TYPE, MANIFEST_TYPE,
                        OCIImageWriter)
from platforms import architecture, normalize_platform


DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_GZIP_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
ACCEPT_MANIFEST = ", ".join((MANIFEST_TYPE, DOCKER_MANIFEST, INDEX_TYPE, DOCKER_INDEX))
MAX_JSON = 16 * 1024 * 1024
BUFFER = 4 * 1024 * 1024
REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class Reference:
    """保存已校验的 Registry 主机、仓库路径和 tag/digest 引用。"""
    registry: str
    repository: str
    version: str
    digest: bool = False

    @property
    def full(self):
        """重建带 tag 或 digest 分隔符的完整镜像引用。"""
        separator = "@" if self.digest else ":"
        return self.registry + "/" + self.repository + separator + self.version


def parse_reference(value):
    """解析完整 Registry 引用，拒绝非法主机、仓库路径和标签。"""
    if not isinstance(value, str) or "/" not in value:
        raise BuildError("Registry reference must be host[:port]/project/image:tag")
    registry, remainder = value.split("/", 1)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?", registry) is None:
        raise BuildError("Invalid registry host")
    parsed = urlsplit("https://" + registry)
    if (not registry or parsed.netloc != registry or not parsed.hostname or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise BuildError("Invalid registry host")
    try:
        parsed.port
    except ValueError as exc:
        raise BuildError("Invalid registry port") from exc
    if "@" in remainder:
        repository, version = remainder.rsplit("@", 1)
        digest_hex(version)
        is_digest = True
    else:
        last = remainder.rsplit("/", 1)[-1]
        if ":" in last:
            repository, version = remainder.rsplit(":", 1)
        else:
            repository, version = remainder, "latest"
        if TAG.fullmatch(version) is None:
            raise BuildError("Invalid registry tag")
        is_digest = False
    if REPOSITORY.fullmatch(repository) is None:
        raise BuildError("Invalid registry repository path")
    return Reference(registry, repository, version, is_digest)


def _json(raw, label):
    if len(raw) > MAX_JSON:
        raise BuildError(label + " exceeds 16 MiB")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BuildError("Invalid {} JSON: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise BuildError(label + " must be a JSON object")
    return value


def _descriptor(value, types):
    if not isinstance(value, dict) or value.get("mediaType") not in types:
        raise BuildError("Unsupported Registry descriptor media type")
    if type(value.get("size")) is not int or value["size"] < 0:
        raise BuildError("Invalid Registry descriptor size")
    digest_hex(value.get("digest"))
    return value


def _check_blob(path, descriptor):
    if path.stat().st_size != descriptor["size"] or sha256_file(path) != digest_hex(descriptor["digest"]):
        raise BuildError("Registry blob size/digest mismatch: " + descriptor["digest"])


def _challenge(value):
    scheme, _, parameters = value.partition(" ")
    if scheme.lower() != "bearer":
        raise BuildError("Unsupported Registry authentication challenge")
    fields = {}
    for item in parse_http_list(parameters):
        key, separator, raw = item.strip().partition("=")
        if separator:
            fields[key.lower()] = raw.strip().strip('"')
    if not fields.get("realm"):
        raise BuildError("Bearer challenge has no realm")
    return fields


class RegistryClient:
    """管理一个仓库的 V2 鉴权，以及受作用域限制的 manifest/blob 请求。"""
    def __init__(self, reference, username=None, password=None, ca_file=None,
                 insecure_http=False, auth_host=None, timeout=60):
        if (username is None) != (password is None):
            raise BuildError("Registry username and password must be provided together")
        self.reference = parse_reference(reference) if isinstance(reference, str) else reference
        self.scheme = "http" if insecure_http else "https"
        self.origin = self.scheme + "://" + self.reference.registry
        self.username = username
        self.password = password
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self.auth_hosts = {self.reference.registry.lower()}
        if auth_host:
            self.auth_hosts.add(auth_host.lower())
        self.tokens = {}
        self.manifest_raw = None

    def _url(self, suffix):
        return self.origin + "/v2/" + self.reference.repository + suffix

    def _single(self, method, url, headers, body, sink, allowed, max_stream_bytes=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or
                parsed.username or parsed.password or parsed.fragment or
                (parsed.scheme == "http" and self.scheme != "http")):
            raise BuildError("Unsafe Registry URL")
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": self.timeout}
        if parsed.scheme == "https":
            kwargs["context"] = self.ssl_context
        connection = connection_type(parsed.netloc, **kwargs)
        try:
            connection.request(method, urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                               body=body, headers=headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            status = response.status
            if status in allowed and sink is not None:
                if (max_stream_bytes is not None and
                        response_headers.get("content-length", "").isdecimal() and
                        int(response_headers["content-length"]) > max_stream_bytes):
                    raise BuildError("Registry blob exceeds descriptor size")
                with open(sink, "wb") as output:
                    total = 0
                    for block in iter(lambda: response.read(BUFFER), b""):
                        total += len(block)
                        if max_stream_bytes is not None and total > max_stream_bytes:
                            raise BuildError("Registry blob exceeds descriptor size")
                        output.write(block)
                        callback = getattr(self, "progress", None)
                        if callback is not None:
                            callback(total, max_stream_bytes, "Downloading Registry blob")
                return status, response_headers, b""
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise BuildError("Registry response exceeds 16 MiB")
            return status, response_headers, raw
        except (OSError, http.client.HTTPException) as exc:
            raise BuildError("Registry transport failed: {}".format(exc)) from exc
        finally:
            connection.close()

    def _basic(self):
        if self.username is None or self.password is None:
            return None
        value = (self.username + ":" + self.password).encode("utf-8")
        return "Basic " + base64.b64encode(value).decode("ascii")

    def _token(self, header, scope):
        challenge = _challenge(header)
        realm = challenge["realm"]
        parsed = urlsplit(realm)
        if (parsed.scheme != "https" and self.scheme != "http") or \
                parsed.netloc.lower() not in self.auth_hosts:
            raise BuildError("Bearer token realm is not a trusted HTTPS auth host")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if challenge.get("service"):
            query["service"] = challenge["service"]
        query["scope"] = challenge.get("scope") or scope
        token_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                               urlencode(query), ""))
        headers = {}
        if self._basic():
            headers["Authorization"] = self._basic()
        status, _, raw = self._single("GET", token_url, headers, None, None, {200})
        if status != 200:
            raise BuildError("Registry token request failed: HTTP {}".format(status))
        token_response = _json(raw, "Registry token")
        token = token_response.get("token") or token_response.get("access_token")
        if not isinstance(token, str) or not token:
            raise BuildError("Registry token response has no token")
        self.tokens[scope] = token

    def request(self, method, url, scope, allowed=(200,), headers=None,
                body=None, sink=None, redirects=5, max_stream_bytes=None):
        """发送 Registry 请求，处理 Bearer challenge，并只跟随允许的重定向。"""
        headers = dict(headers or {})
        current = url
        for _ in range(redirects + 1):
            origin = urlsplit(current).netloc.lower() == self.reference.registry.lower()
            sent = dict(headers)
            if origin:
                if scope in self.tokens:
                    sent["Authorization"] = "Bearer " + self.tokens[scope]
                elif self._basic():
                    sent["Authorization"] = self._basic()
            position = body.tell() if hasattr(body, "tell") else None
            status, response_headers, raw = self._single(method, current, sent, body,
                                                          sink, set(allowed), max_stream_bytes)
            if status == 401 and origin and "www-authenticate" in response_headers:
                self._token(response_headers["www-authenticate"], scope)
                if position is not None:
                    body.seek(position)
                elif body is not None and not isinstance(body, bytes):
                    raise BuildError("Registry upload body cannot be retried after authentication")
                status, response_headers, raw = self._single(
                    method, current, {**headers, "Authorization": "Bearer " + self.tokens[scope]},
                    body, sink, set(allowed), max_stream_bytes)
            if status in (301, 302, 303, 307, 308) and method in ("GET", "HEAD"):
                location = response_headers.get("location")
                if not location:
                    raise BuildError("Registry redirect has no Location")
                current = urljoin(current, location)
                continue
            if status not in allowed:
                raise BuildError("Registry {} failed: HTTP {}".format(method, status))
            return status, response_headers, raw
        raise BuildError("Too many Registry redirects")

    def _scope(self, action):
        return "repository:{}:{}".format(self.reference.repository, action)

    def manifest(self, reference):
        """获取 manifest/index，返回 (对象, media_type, 原始字节摘要, 字节数)。

        按 digest 请求时核对请求摘要；响应提供 Docker-Content-Digest 时核对该头。
        按 tag 请求且响应未提供摘要头时，仍返回本地计算的原始响应摘要。
        最近一次成功响应的原始字节保存在 manifest_raw，供 CAS 原样入库。
        """
        url = self._url("/manifests/" + quote(reference, safe=":"))
        _, headers, raw = self.request("GET", url, self._scope("pull"),
                                       headers={"Accept": ACCEPT_MANIFEST})
        computed = "sha256:" + hashlib.sha256(raw).hexdigest()
        if reference.startswith("sha256:") and computed != reference.lower():
            raise BuildError("Registry manifest digest mismatch")
        remote = headers.get("docker-content-digest")
        if remote and remote.lower() != computed:
            raise BuildError("Registry manifest digest header mismatch")
        value = _json(raw, "Registry manifest")
        media_type = value.get("mediaType") or headers.get("content-type", "").split(";", 1)[0]
        self.manifest_raw = raw
        return value, media_type, computed, len(raw)

    def blob(self, descriptor, destination):
        """下载 descriptor 对应的原始 blob 字节，并核对长度和摘要。

        blob 可以是 config、未压缩 tar 或压缩层；此处不解压、不验证 DiffID。
        直接写入 destination，失败可能留下部分文件，由调用方清理临时目录。
        """
        _descriptor(descriptor, (CONFIG_TYPE, DOCKER_CONFIG, LAYER_TYPE,
                                 OCI_GZIP_LAYER, DOCKER_GZIP_LAYER))
        url = self._url("/blobs/" + quote(descriptor["digest"], safe=":"))
        self.request("GET", url, self._scope("pull"), sink=destination,
                     max_stream_bytes=descriptor["size"])
        _check_blob(Path(destination), descriptor)

    def upload_file(self, path, descriptor):
        """仅在 Registry 尚无相同 digest 时上传 blob；上传前校验本地文件。"""
        _check_blob(Path(path), descriptor)
        digest = descriptor["digest"]
        url = self._url("/blobs/" + quote(digest, safe=":"))
        status, _, _ = self.request("HEAD", url, self._scope("pull,push"), allowed=(200, 404))
        if status == 200:
            return
        _, response_headers, _ = self.request("POST", self._url("/blobs/uploads/"),
                                               self._scope("pull,push"), allowed=(202,))
        location = response_headers.get("location")
        if not location:
            raise BuildError("Registry upload response has no Location")
        upload_url = urljoin(self.origin, location)
        parsed = urlsplit(upload_url)
        if (parsed.scheme != self.scheme or
                parsed.netloc.lower() != self.reference.registry.lower() or
                not parsed.path.startswith("/v2/" + self.reference.repository + "/blobs/uploads/")):
            raise BuildError("Registry upload Location leaves the requested repository")
        query = parsed.query + ("&" if parsed.query else "") + urlencode({"digest": digest})
        upload_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        with open(path, "rb") as stream:
            _, result, _ = self.request("PUT", upload_url, self._scope("pull,push"),
                                        allowed=(201,), headers={
                                            "Content-Type": "application/octet-stream",
                                            "Content-Length": str(descriptor["size"])},
                                        body=stream)
        remote = result.get("docker-content-digest")
        if remote and remote.lower() != digest.lower():
            raise BuildError("Registry upload digest confirmation mismatch")


def _selected_manifest(client, target_platform="linux/amd64"):
    target_architecture = architecture(target_platform)
    value, media_type, digest, _ = client.manifest(client.reference.version)
    if media_type in (INDEX_TYPE, DOCKER_INDEX):
        entries = value.get("manifests")
        if not isinstance(entries, list):
            raise BuildError("Invalid Registry image index")
        selected = [item for item in entries if isinstance(item, dict) and
                    isinstance(item.get("platform"), dict) and
                    item["platform"].get("os") == "linux" and
                    item["platform"].get("architecture") == target_architecture]
        if len(selected) != 1:
            raise BuildError("Registry index must have exactly one " + target_platform + " image")
        child = _descriptor(selected[0], (MANIFEST_TYPE, DOCKER_MANIFEST))
        value, media_type, digest, size = client.manifest(child["digest"])
        if size != child["size"] or media_type != child["mediaType"]:
            raise BuildError("Registry index child descriptor mismatch")
    if media_type not in (MANIFEST_TYPE, DOCKER_MANIFEST) or value.get("schemaVersion") != 2:
        raise BuildError("Only OCI/Docker schema 2 image manifests are supported")
    return value, digest


def pull(reference, output, username=None, password=None, ca_file=None,
         insecure_http=False, auth_host=None, image_format="docker", oci_output=None,
         target_platform="linux/amd64", reporter=None, workers=0, cas_store=None,
         cas_only=False, cas_reference=None):
    """拉取一个目标平台，验证层并导出 Docker/OCI 归档或直接写入 CAS。

    并发下载后解压并核对 DiffID；按 manifest 原始顺序组装，不按完成先后排列。
    提供 cas_store 时同时保存原始压缩 blob 与已验证的未压缩层；ref 最后发布。
    cas_only=True 不生成输出 tar，返回的 output 仅保持 API 的路径返回约定。
    """
    target_platform = normalize_platform(target_platform)
    client = RegistryClient(reference, username, password, ca_file, insecure_http, auth_host)
    if reporter is not None:
        client.progress = reporter.progress
    output = Path(output).resolve()
    if output.exists():
        raise BuildError("Output already exists: " + str(output))
    if output.suffix.lower() != ".tar":
        raise BuildError("Registry pull output must end with .tar")
    if image_format not in ("docker", "oci", "both"):
        raise BuildError("Pull format must be docker, oci, or both")
    if workers < 0:
        raise BuildError("Pull workers cannot be negative")
    if cas_only and cas_store is None:
        raise BuildError("CAS-only pull requires a CAS store")
    if oci_output is not None and image_format != "both":
        raise BuildError("--oci-output requires --format both")
    second = (Path(oci_output).resolve() if oci_output else
              output.with_name(output.stem + ".oci.tar")) if image_format == "both" else None
    if second is not None and (second == output or second.exists() or second.suffix.lower() != ".tar"):
        raise BuildError("OCI output already exists or conflicts with primary output")
    output.parent.mkdir(parents=True, exist_ok=True)
    if second is not None:
        second.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-pull-") as temporary:
        workspace = Path(temporary)
        with reporter.timed("Registry auth and manifest") if reporter is not None else nullcontext():
            manifest, manifest_digest = _selected_manifest(client, target_platform)
        manifest_raw = getattr(client, "manifest_raw", None)
        if cas_store is not None or manifest_raw is not None:
            # manifest 身份取决于响应字节；重新 dump 即使语义相同，
            # 也会改变空白、转义与 digest，不能用于保存远端镜像身份。
            if (not isinstance(manifest_raw, bytes) or
                    "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != manifest_digest or
                    not same_json_value(_json(manifest_raw, "selected Registry manifest"), manifest)):
                raise BuildError("Registry client did not retain verified selected manifest bytes")
        config_descriptor = _descriptor(manifest.get("config"), (CONFIG_TYPE, DOCKER_CONFIG))
        config_path = workspace / "config.json"
        with reporter.timed("Download Registry config") if reporter is not None else nullcontext():
            client.blob(config_descriptor, config_path)
        config_raw = config_path.read_bytes()
        config = _json(config_raw, "image config")
        if config.get("os") != "linux" or config.get("architecture") != architecture(target_platform):
            raise BuildError("Registry image config does not match " + target_platform)
        rootfs = config.get("rootfs")
        if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
            raise BuildError("Registry image config rootfs.type must be layers")
        diff_ids = rootfs.get("diff_ids")
        descriptors = manifest.get("layers")
        if not isinstance(diff_ids, list) or not isinstance(descriptors, list) or len(diff_ids) != len(descriptors):
            raise BuildError("Registry image config/layer count mismatch")
        image_history(config, len(diff_ids))
        for diff_id in diff_ids:
            digest_hex(diff_id)
        count = min(workers or 4, len(descriptors)) if descriptors else 1
        for descriptor in descriptors:
            _descriptor(descriptor, (LAYER_TYPE, OCI_GZIP_LAYER, DOCKER_GZIP_LAYER))
        if reporter is not None:
            reporter.log("Registry layers: {} download worker(s)".format(count))
            download_progress = {}
            progress_lock = threading.Lock()
            worker_state = threading.local()
            expected_bytes = sum(descriptor["size"] for descriptor in descriptors)

            def aggregate_progress(current, _total, _label):
                """在锁内汇总各下载线程的累计字节，避免并发进度相互覆盖。"""
                index = worker_state.index
                with progress_lock:
                    download_progress[index] = current
                    completed_bytes = sum(download_progress.values())
                reporter.progress(completed_bytes, expected_bytes, "Downloading Registry layers")

            client.progress = aggregate_progress

        def download_layer(index, descriptor):
            compressed = workspace / ("download-{}.blob".format(index))
            if reporter is not None:
                worker_state.index = index
            client.blob(descriptor, compressed)
            return index

        with reporter.timed("Download Registry layers") if reporter is not None else nullcontext():
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [executor.submit(download_layer, index, descriptor)
                           for index, descriptor in enumerate(descriptors)]
                for future in as_completed(futures):
                    index = future.result()
                    if reporter is not None:
                        reporter.log("Downloaded Registry layer {}/{}".format(index + 1, len(descriptors)))

        def materialize_layer(index, descriptor, diff_id):
            """解压或移动传输 blob，核对 DiffID 与 tar 格式，返回原 manifest 位置及层路径。"""
            compressed = workspace / ("download-{}.blob".format(index))
            layer = workspace / ("layer-{}.tar".format(index))
            if descriptor["mediaType"] == LAYER_TYPE:
                os.replace(compressed, layer)
            else:
                try:
                    with gzip.open(compressed, "rb") as source, open(layer, "wb") as target:
                        shutil.copyfileobj(source, target, BUFFER)
                except (OSError, EOFError, zlib.error) as exc:
                    raise BuildError("Invalid compressed Registry layer: " + str(exc)) from exc
            if sha256_file(layer) != digest_hex(diff_id) or not tarfile.is_tarfile(layer):
                raise BuildError("Registry layer DiffID or tar format mismatch")
            return index, layer

        # 并发任务完成顺序不固定，必须按 manifest 位置保存结果，
        # 保持 rootfs 应用顺序与 config.diff_ids 一致。
        layers = [None] * len(descriptors)
        with reporter.timed("Decompress and verify Registry layers") if reporter is not None else nullcontext():
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [executor.submit(materialize_layer, index, descriptor, diff_id)
                           for index, (descriptor, diff_id) in enumerate(zip(descriptors, diff_ids))]
                for future in as_completed(futures):
                    index, layer = future.result()
                    layers[index] = layer
                    if reporter is not None:
                        reporter.log("Verified Registry layer {}/{}".format(index + 1, len(descriptors)))
        local_tag = docker_archive_tag(client.reference.full)
        stored_reference = cas_reference or client.reference.full

        def publish_cas():
            """先保存已验证的未压缩层，再导入传输 blob，所有数据落盘后发布 ref。

            保留未压缩层是为了让后续构建直接复用，避免重新解压传输 blob。
            """
            for layer, diff_id in zip(layers, diff_ids):
                cas_store.store_decoded_layer(layer, diff_id)
            raw_layers = [workspace / ("download-{}.blob".format(index))
                          if descriptor["mediaType"] != LAYER_TYPE else layers[index]
                          for index, descriptor in enumerate(descriptors)]
            cas_store.import_registry(
                manifest_raw,
                config_path, raw_layers, stored_reference, target_platform,
                source="registry", replace=True)

        if cas_only:
            publish_cas()
            return output
        products = []
        if image_format in ("docker", "both"):
            partial = workspace / "image.tar"
            writer = ImageArchiveWriter()
            with reporter.timed("Write pulled Docker archive") if reporter is not None else nullcontext():
                image = BaseImage(config, layers, [local_tag], manifest, config_descriptor["digest"], config_raw,
                                  manifest_raw=manifest_raw, manifest_digest=manifest_digest)
                writer.write_image(partial, image, local_tag)
            with reporter.timed("Verify pulled Docker archive") if reporter is not None else nullcontext():
                writer.verify(partial, local_tag)
            products.append((partial, output))
        if image_format in ("oci", "both"):
            partial = workspace / "image.oci.tar"
            writer = OCIImageWriter()
            with reporter.timed("Write pulled OCI archive") if reporter is not None else nullcontext():
                image = BaseImage(config, layers, [local_tag], manifest, config_descriptor["digest"], config_raw,
                                  manifest_raw=manifest_raw, manifest_digest=manifest_digest)
                writer.write_image(partial, image, local_tag)
            with reporter.timed("Verify pulled OCI archive") if reporter is not None else nullcontext():
                writer.verify(partial, local_tag)
            products.append((partial, second or output))
        if cas_store is not None:
            publish_cas()
        published = []
        try:
            for partial, destination in products:
                publish_new_file(partial, destination)
                published.append(destination)
        except Exception:
            for destination in published:
                unlink_missing(destination)
            raise
    return output


def _oci_archive(path, workspace, tag):
    path = Path(path)
    try:
        with tarfile.open(path, "r:*") as archive:
            members = archive_members(archive)
            marker = members.get("oci-layout")
            if marker is not None:
                index_member = members["index.json"]
                if not index_member.isfile() or index_member.size > MAX_JSON:
                    raise ArchiveError("OCI index exceeds 16 MiB")
                with archive.extractfile(index_member) as stream:
                    index = _json(stream.read(), "OCI index")
                existing_tag = index["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"]
                OCIImageWriter().verify(path, existing_tag)
                return path
    except (tarfile.TarError, KeyError, IndexError, TypeError) as exc:
        raise ArchiveError("Invalid image archive: " + str(exc)) from exc
    image = ImageArchiveReader(path, workspace / "base").read(tag)
    output = workspace / "converted.oci.tar"
    writer = OCIImageWriter()
    writer.write_image(output, image, tag)
    writer.verify(output, tag)
    return output


def _check_push_tag(client, overwrite):
    if client.reference.digest:
        raise BuildError("Push destination must use a mutable tag")
    manifest_url = client._url("/manifests/" + quote(client.reference.version, safe=""))
    status, _, _ = client.request("HEAD", manifest_url, client._scope("pull,push"),
                                  allowed=(200, 404), headers={"Accept": ACCEPT_MANIFEST})
    if status == 200 and not overwrite:
        raise BuildError("Registry tag already exists; pass --overwrite to replace it")


def _prepare_remote_manifest(client, archive, manifest_descriptor, workspace, prefix, members=None):
    # 调用方已经校验布局；继续沿用规范化索引，不能再次按原始成员名读取。
    members = archive_members(archive) if members is None else members
    manifest_descriptor = _descriptor(manifest_descriptor, (MANIFEST_TYPE,))
    manifest_path = "blobs/sha256/" + digest_hex(manifest_descriptor["digest"])
    with archive.extractfile(members[manifest_path]) as stream:
        manifest_raw = stream.read()
    manifest = _json(manifest_raw, "OCI manifest")
    config = _descriptor(manifest.get("config"), (CONFIG_TYPE,))
    layers = [_descriptor(item, (LAYER_TYPE,)) for item in manifest.get("layers", [])]
    prepared = []
    for index, descriptor in enumerate([config] + layers):
        name = "blobs/sha256/" + digest_hex(descriptor["digest"])
        member = members[name]
        if member.size != descriptor["size"]:
            raise ArchiveError("OCI blob size mismatch")
        source = archive.extractfile(member)
        if source is None:
            raise ArchiveError("Unreadable OCI blob: " + name)
        destination = workspace / ("{}-{}.blob".format(prefix, index))
        with source, open(destination, "wb") as target:
            if index == 0:
                shutil.copyfileobj(source, target, BUFFER)
            else:
                with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0) as compressed:
                    shutil.copyfileobj(source, compressed, BUFFER)
        prepared_descriptor = copy.deepcopy(descriptor)
        if index != 0:
            prepared_descriptor.update(mediaType=OCI_GZIP_LAYER,
                                       digest="sha256:" + sha256_file(destination),
                                       size=destination.stat().st_size)
            # 注解属于描述信息；data/urls 指向旧 blob，不可随压缩后的新内容继承。
            prepared_descriptor.pop("data", None)
            prepared_descriptor.pop("urls", None)
        prepared.append((destination, prepared_descriptor))
    if layers:
        manifest["layers"] = [item[1] for item in prepared[1:]]
        manifest_raw = image_json_bytes(manifest, label="remote image manifest")
    # 零层镜像没有压缩变化，manifest 直接使用原字节，digest 也必须保持。
    for destination, descriptor in prepared:
        client.upload_file(destination, descriptor)
    return manifest_raw


def _put_manifest(client, reference, raw, media_type):
    url = client._url("/manifests/" + quote(reference, safe=":"))
    _, response_headers, _ = client.request("PUT", url, client._scope("pull,push"),
        allowed=(201, 202), headers={"Content-Type": media_type,
                                    "Content-Length": str(len(raw))}, body=raw)
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    remote = response_headers.get("docker-content-digest")
    if remote and remote.lower() != actual:
        raise BuildError("Registry manifest digest confirmation mismatch")
    return actual


def push(archive_path, reference, username=None, password=None, ca_file=None,
         insecure_http=False, auth_host=None, overwrite=False):
    """上传单平台 OCI/Docker 归档，并发布 Registry manifest。"""
    client = RegistryClient(reference, username, password, ca_file, insecure_http, auth_host)
    _check_push_tag(client, overwrite)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-push-") as temporary:
        path = _oci_archive(archive_path, Path(temporary), client.reference.full)
        with tarfile.open(path, "r:") as archive:
            members = archive_members(archive)
            with archive.extractfile(members["index.json"]) as stream:
                index_raw = stream.read()
                index = _json(index_raw, "OCI index")
            manifest_raw = _prepare_remote_manifest(client, archive, index["manifests"][0],
                                                     Path(temporary), "single", members)
            return _put_manifest(client, client.reference.version, manifest_raw, MANIFEST_TYPE)


def push_index(archive_path, reference, username=None, password=None, ca_file=None,
               insecure_http=False, auth_host=None, overwrite=False):
    """上传各平台 manifest，再以一个 tag 发布多平台 OCI index。"""
    from multiarch import verify as verify_multiarch

    client = RegistryClient(reference, username, password, ca_file, insecure_http, auth_host)
    _check_push_tag(client, overwrite)
    try:
        with tarfile.open(archive_path, "r:") as archive:
            member = archive_members(archive)["index.json"]
            if not member.isfile() or member.size > MAX_JSON:
                raise ArchiveError("Missing or oversized OCI index")
            with archive.extractfile(member) as stream:
                index_raw = stream.read()
                index = _json(index_raw, "OCI index")
            old_tag = index["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"]
    except (tarfile.TarError, KeyError, IndexError, TypeError) as exc:
        raise ArchiveError("Invalid multi-platform OCI archive: " + str(exc)) from exc
    verify_multiarch(archive_path, old_tag)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-push-index-") as temporary:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive_members(archive)
            published = []
            for number, entry in enumerate(index["manifests"]):
                raw = _prepare_remote_manifest(client, archive, entry, Path(temporary),
                                               "platform-{}".format(number), members)
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                _put_manifest(client, digest, raw, MANIFEST_TYPE)
                updated = copy.deepcopy(entry)
                updated.update(mediaType=MANIFEST_TYPE, digest=digest, size=len(raw))
                if digest != entry["digest"]:
                    updated.pop("data", None)
                    updated.pop("urls", None)
                updated.setdefault("annotations", {}).update({
                    "org.opencontainers.image.ref.name": client.reference.full,
                    "io.containerd.image.name": client.reference.full})
                published.append(updated)
            remote_index = copy.deepcopy(index)
            remote_index["manifests"] = published
            # 无内容或引用变更时保留 index 原字节；变更时保留扩展字段再生成新身份。
            result = (index_raw if same_json_value(remote_index, index) else
                      image_json_bytes(remote_index, label="remote image index"))
            return _put_manifest(client, client.reference.version, result, INDEX_TYPE)


def main(argv=None):
    """独立脚本入口：pull、push 和 push-index。"""
    from settings import load_settings, repository_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Shared config.json path")
    parser.add_argument("--username", help="Harbor user or robot account")
    parser.add_argument("--password-env",
                        help="Environment variable containing password/token; never pass it on the command line")
    parser.add_argument("--ca-file", type=Path, help="Private CA bundle for HTTPS verification")
    parser.add_argument("--insecure-http", action="store_true", help="Explicitly allow plain HTTP for a trusted test registry")
    parser.add_argument("--auth-host", help="Additional trusted HTTPS token service host")
    actions = parser.add_subparsers(dest="action", required=True)
    download = actions.add_parser("pull", help="Pull to Docker save or OCI tar")
    download.add_argument("--config", dest="command_config", type=Path)
    download.add_argument("reference")
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--format", choices=("docker", "oci", "both"), default="docker")
    download.add_argument("--oci-output", type=Path)
    download.add_argument("--workers", type=int, default=0,
                          help="Concurrent layer downloads; 0 uses up to 4")
    download.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                          default="linux/amd64")
    upload = actions.add_parser("push", help="Push Docker save or OCI tar")
    upload.add_argument("--config", dest="command_config", type=Path)
    upload.add_argument("reference")
    upload.add_argument("--archive", type=Path, required=True)
    upload.add_argument("--overwrite", action="store_true", help="Allow replacing an existing Registry tag")
    multi = actions.add_parser("push-index", help="Push a two-platform OCI index tar")
    multi.add_argument("--config", dest="command_config", type=Path)
    multi.add_argument("reference")
    multi.add_argument("--archive", type=Path, required=True)
    multi.add_argument("--overwrite", action="store_true", help="Allow replacing an existing Registry tag")
    args = parser.parse_args(argv)
    try:
        settings, _ = load_settings(args.command_config or args.config)
        connection = repository_settings(settings, args.reference)
        username = args.username or connection.get("username")
        password_env = args.password_env or connection.get(
            "passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD")
        password = os.environ.get(password_env)
        if (username is None) != (password is None):
            raise BuildError("Set both --username and the password environment variable")
        common = (username, password, args.ca_file or connection.get("caFile"),
                  args.insecure_http or connection.get("insecureHttp", False),
                  args.auth_host or connection.get("authHost"))
        if args.action == "pull":
            pull(args.reference, args.output, *common, args.format, args.oci_output,
                 args.platform, workers=args.workers)
            print("Pulled {} to {}".format(args.reference, args.output))
        elif args.action == "push":
            digest = push(args.archive, args.reference, *common, args.overwrite)
            print("Pushed {} ({})".format(args.reference, digest))
        else:
            digest = push_index(args.archive, args.reference, *common, args.overwrite)
            print("Pushed multi-platform {} ({})".format(args.reference, digest))
        return 0
    except (BuildError, OSError) as exc:
        print("Registry operation failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
