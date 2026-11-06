"""通过真实 localhost TLS 验证兼容范围；生产运行不依赖 OpenSSL 命令。"""

import contextlib
import gzip
import hashlib
import io
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import tarfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download
import fast
import main
import registry
import web_repository
from errors import BuildError
from network_tls import tls_context, tls_options
from settings import load_settings, repository_settings, repository_url


class TransportConfigurationTests(unittest.TestCase):
    def test_tls_boolean_schema_and_default(self):
        self.assertEqual(tls_options(), {})
        self.assertEqual(tls_options(False), {"verify_tls": False})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for value in (False, True, "false", 0, None):
                data = {"schemaVersion": 2, "repositories": {"repo.example": {"verifyTls": value}}}
                path.write_text(json.dumps(data), encoding="utf-8")
                if type(value) is bool:
                    self.assertIs(load_settings(path)[0]["repositories"]["repo.example"]["verifyTls"], value)
                else:
                    with self.assertRaisesRegex(BuildError, "verifyTls"):
                        load_settings(path)
                    with self.assertRaises(BuildError):
                        tls_context(verify_tls=value)

    def test_unique_profile_protocol_is_inferred_without_mutating_config(self):
        entry = {"source": "artifactory"}
        settings = {"repositories": {"repo.example": entry}, "fast": {"profiles": {
            "tomcat": {"baseUrl": "http://repo.example/repo/tomcat:9"}}}}
        self.assertTrue(repository_settings(settings, "repo.example/repo/tomcat:9")["insecureHttp"])
        self.assertNotIn("insecureHttp", entry)
        self.assertFalse(repository_settings(settings, "https://repo.example/repo/tomcat:9")["insecureHttp"])
        entry["insecureHttp"] = False
        self.assertFalse(repository_settings(settings, "http://repo.example/repo/tomcat:9")["insecureHttp"])
        self.assertEqual(repository_url("http://repo.example/a/b:1?x=2", entry),
                         "https://repo.example/a/b:1?x=2")

    def test_conflicting_templates_require_explicit_protocol_for_bare_reference(self):
        settings = {"repositories": {"repo.example": {"source": "artifactory"}},
                    "fast": {"profiles": {"a": {"baseUrl": "http://repo.example/a:1"},
                                            "b": {"baseUrl": "https://repo.example/b:1"}}}}
        with self.assertRaisesRegex(BuildError, "insecureHttp"):
            repository_settings(settings, "repo.example")
        self.assertTrue(repository_settings(settings, "http://repo.example/a:1")["insecureHttp"])
        settings["repositories"]["repo.example"]["insecureHttp"] = False
        self.assertFalse(repository_settings(settings, "repo.example")["insecureHttp"])

    def test_fast_normalizes_template_using_shared_repository_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            data = {"schemaVersion": 2, "repositories": {"repo.example": {
                "source": "artifactory", "insecureHttp": False, "verifyTls": False}},
                "fast": {"profiles": {"tomcat": {"flavor": "tomcat",
                    "baseImage": "repo.example/repo/tomcat:9",
                    "baseUrl": "http://repo.example/repo/tomcat:9"}}}}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = fast.load_profile(path, "tomcat")
            self.assertEqual(profile["baseUrl"], "https://repo.example/artifactory/repo/tomcat/9/")
            self.assertIs(profile["verifyTls"], False)


class RepositoryTLSTests(unittest.TestCase):
    def setUp(self):
        cls = self
        cls.files = {}
        openssl = shutil.which("openssl")
        if openssl is None and Path("D:/PortableGit/usr/bin/openssl.exe").is_file():
            openssl = "D:/PortableGit/usr/bin/openssl.exe"
        if openssl is None:
            raise unittest.SkipTest("Local TLS fixture requires the OpenSSL test utility")
        cls.temporary = tempfile.TemporaryDirectory(prefix="pyib-test-tls-")
        cls.addCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        cls.cert, key = root / "localhost.pem", root / "localhost.key"
        # 使用配置文件声明 SAN，兼容旧 UOS 的 OpenSSL（可能没有 -addext）。
        config = root / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n"
                          "[dn]\nCN=localhost\n[extensions]\nbasicConstraints=critical,CA:TRUE\n"
                          "subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="ascii")
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-sha256", "-config", str(config),
                        "-days", "1", "-keyout", str(key), "-out", str(cls.cert)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.respond(False)

            def do_GET(self):
                self.respond(True)

            def respond(self, write_body):
                name = self.path.rsplit("/", 1)[-1]
                body = cls.files.get(name, b"OK")
                if cls.files and not name:
                    body = ("<html>" + "".join('<a href="{0}">{0}</a>'.format(key)
                                                for key in cls.files) + "</html>").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if write_body:
                    self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addCleanup(cls.server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert), str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01})
        cls.thread.start()
        cls.addCleanup(cls.thread.join, 2)
        cls.addCleanup(cls.server.shutdown)
        cls.host = "127.0.0.1:{}".format(cls.server.server_port)
        cls.url = "https://" + cls.host

    def test_artifactory_strict_default_and_explicit_compatibility(self):
        opener = artifactory_download.make_opener(self.url)
        with self.assertRaises(URLError):
            artifactory_download.request(opener, self.url, timeout=2)
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            opener = artifactory_download.make_opener(self.url, verify_tls=False)
        with artifactory_download.request(opener, self.url, timeout=2) as response:
            self.assertEqual(response.read(), b"OK")
        self.assertIn(self.host, warning.getvalue())

    def test_artifactory_trusted_ca_and_other_origin_remains_strict(self):
        opener = artifactory_download.make_opener(self.url, ca_file=self.cert)
        with artifactory_download.request(opener, self.url, timeout=2) as response:
            self.assertEqual(response.read(), b"OK")
        with contextlib.redirect_stderr(io.StringIO()):
            opener = artifactory_download.make_opener(self.url, verify_tls=False)
        foreign = "https://localhost:{}".format(self.server.server_port)
        # 直接复用 opener 也不能将来源例外带到另一个主机名。
        with self.assertRaises(URLError):
            opener.open(foreign, timeout=2)
        with self.assertRaisesRegex(ValueError, "origin"):
            artifactory_download.request(opener, foreign, timeout=2)

    def test_registry_strict_compatibility_and_foreign_origin(self):
        strict = registry.RegistryClient(self.host + "/test/base:1", timeout=2)
        with self.assertRaises(BuildError):
            strict.request("GET", self.url + "/v2/", "")
        with contextlib.redirect_stderr(io.StringIO()):
            client = registry.RegistryClient(self.host + "/test/base:1", timeout=2, verify_tls=False)
        self.assertEqual(client.request("GET", self.url + "/v2/", "")[0], 200)
        with self.assertRaises(BuildError):
            client.request("GET", "https://localhost:{}/v2/".format(self.server.server_port), "")
        with contextlib.redirect_stderr(io.StringIO()):
            http_client = registry.RegistryClient(self.host + "/test/base:1", timeout=2,
                                                 insecure_http=True, verify_tls=False)
        with self.assertRaises(BuildError):
            http_client.request("GET", self.url + "/v2/", "")
        trusted = registry.RegistryClient(self.host + "/test/base:1", ca_file=self.cert, timeout=2)
        self.assertEqual(trusted.request("GET", self.url + "/v2/", "")[0], 200)

    def test_web_diagnostics_use_same_tls_policy_for_both_sources(self):
        for source in ("registry", "artifactory"):
            settings = {"repositories": {self.host: {"source": source, "verifyTls": False}}}
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = web_repository.diagnose(settings, self.host)
            self.assertEqual(result["地址"], self.url)
            self.assertEqual(result["证书校验"], "关闭（仅此仓库）")

    def test_configured_https_pull_then_fast_build_from_cas(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            member = tarfile.TarInfo("base.txt")
            member.size = 4
            archive.addfile(member, io.BytesIO(b"base"))
        layer = stream.getvalue()
        compressed = gzip.compress(layer)
        config = json.dumps({"architecture": "amd64", "os": "linux", "config": {},
                             "rootfs": {"type": "layers", "diff_ids": [
                                 "sha256:" + hashlib.sha256(layer).hexdigest()]},
                             "history": [{"created_by": "base"}]}).encode()
        config_hash, layer_hash = hashlib.sha256(config).hexdigest(), hashlib.sha256(compressed).hexdigest()
        manifest = json.dumps({"schemaVersion": 2, "config": {"digest": "sha256:" + config_hash},
                               "layers": [{"digest": "sha256:" + layer_hash,
                                           "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}).encode()
        self.files.update({"manifest.json": manifest, "sha256__" + config_hash: config,
                           "sha256__" + layer_hash: compressed})
        root = Path(self.temporary.name)
        reference = self.host + "/repo/tomcat:9"
        settings = {"schemaVersion": 2, "cache": {"imageStore": str(root / "store")},
                    "repositories": {self.host: {"source": "artifactory", "verifyTls": False,
                                                  "insecureHttp": False}},
                    "fast": {"profiles": {"tomcat": {"flavor": "tomcat", "baseImage": reference,
                              "baseUrl": "http://" + reference, "owner": "1000:1000",
                              "outputDir": str(root / "images")}}}}
        path = root / "config.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        war = root / "hisq.war"
        war.write_bytes(b"war")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(["pull", reference, "--config", str(path)]), 0)
            output, tag = fast.quick_build(war, "hisq", "1.0.0", path, profile_name="tomcat")
        self.assertEqual(tag, "hisq_back:1.0.0")
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
