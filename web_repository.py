"""只读检查工作台中已配置的 Registry 或 Artifactory。"""

import os
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from artifactory_download import artifact_directory_url, make_opener, request
from errors import BuildError
from registry import ACCEPT_MANIFEST, MAX_JSON, RegistryClient, _json, parse_reference


def diagnostic_plan(settings, host, reference):
    if host not in settings.get("repositories", {}):
        raise BuildError("请选择配置中已有的仓库")
    parsed = parse_reference(reference or host + "/pyimagebuilder/probe:latest")
    if parsed.registry != host:
        raise BuildError("镜像引用必须属于选中的仓库")
    if (settings["repositories"][host].get("source") == "artifactory" and reference and
            (parsed.digest or ":" not in reference.rsplit("/", 1)[-1])):
        raise BuildError("Artifactory 镜像请填写显式标签")
    return {"host": host, "reference": parsed.full if reference else ""}


def _http_error(status, image):
    if status == 401:
        return BuildError("HTTP 401：仓库鉴权失败，请检查账户和密码环境变量")
    if status == 403:
        return BuildError("HTTP 403：没有读取权限，请检查仓库或项目授权")
    if status == 404:
        return BuildError("HTTP 404：" + ("镜像标签或仓库路径不存在" if image else "仓库 API 路径不存在"))
    return BuildError("仓库返回 HTTP {}".format(status))


def _transport_error(exc):
    current = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return BuildError("TLS 证书校验失败，请检查仓库证书和 CA 配置")
        if isinstance(current, ssl.SSLError):
            return BuildError("TLS 连接失败，请检查 HTTPS 设置")
        if isinstance(current, socket.gaierror):
            return BuildError("仓库域名解析失败")
        if isinstance(current, (socket.timeout, TimeoutError)):
            return BuildError("连接或读取仓库超时")
        if isinstance(current, FileNotFoundError):
            return BuildError("仓库 CA 文件不存在")
        current = current.reason if isinstance(current, URLError) else current.__cause__
    match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc))
    if match:
        return _http_error(int(match.group(1)), True)
    return BuildError("无法连接仓库或完成鉴权，请检查网络、CA 和仓库配置")


def diagnose(settings, host, reference=""):
    plan = diagnostic_plan(settings, host, reference)
    entry = settings["repositories"][host]
    kind = entry.get("source", "registry")
    username = entry.get("username")
    default_env = "PYIMAGEBUILDER_{}_PASSWORD".format(kind.upper())
    password = os.environ.get(entry.get("passwordEnv", default_env)) if username is not None else None
    if username is not None and password is None:
        raise BuildError("工作台启动环境缺少配置指定的仓库密码变量")
    insecure = entry.get("insecureHttp", False)
    try:
        if kind == "registry":
            client = RegistryClient(plan["reference"] or host + "/pyimagebuilder/probe:latest", username,
                                    password, entry.get("caFile"), insecure, entry.get("authHost"), timeout=10)
            if reference:
                target = client._url("/manifests/" + quote(client.reference.version, safe=":"))
                status, _, raw = client.request("GET", target, client._scope("pull"),
                                                allowed=(200, 401, 403, 404), headers={"Accept": ACCEPT_MANIFEST})
            else:
                status, _, raw = client.request("GET", client.origin + "/v2/", "", allowed=(200, 401, 403, 404))
        else:
            scheme = "http://" if insecure else "https://"
            origin = scheme + host
            target = (artifact_directory_url(scheme + plan["reference"]) + "manifest.json"
                      if reference else origin + "/artifactory/api/system/ping")
            opener = make_opener(origin, username, password, entry.get("caFile"))
            with request(opener, target, timeout=10) as response:
                status, raw = response.status, response.read(MAX_JSON + 1)
    except HTTPError as exc:
        exc.close()
        raise _http_error(exc.code, bool(reference)) from None
    except (BuildError, OSError, ValueError) as exc:
        raise _transport_error(exc) from None
    if status != 200:
        raise _http_error(status, bool(reference))
    if not reference and raw.lstrip().startswith(b"<"):
        raise BuildError("仓库地址返回网页，请检查仓库 API 地址或反向代理配置")
    if reference:
        try:
            metadata = _json(raw, "remote manifest")
            if not isinstance(metadata, dict) or not (
                    isinstance(metadata.get("manifests"), list) or isinstance(metadata.get("config"), dict)):
                raise BuildError("Invalid remote manifest")
        except (BuildError, RecursionError):
            raise BuildError("仓库已连通，但返回的镜像元数据格式不正确") from None
    return {"仓库": host, "类型": kind, "镜像": plan["reference"] or "未指定",
            "结果": "镜像元数据可读取" if reference else "仓库 API 可访问",
            "范围": "只读检查，未测试推送权限"}
