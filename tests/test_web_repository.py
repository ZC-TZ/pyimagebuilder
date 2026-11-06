"""验证仓库诊断实际连接协议、证书错误提示及凭据不进入诊断日志。"""

import contextlib
import io
import os
import ssl
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_repository
from errors import BuildError


class RepositoryDiagnosticTests(unittest.TestCase):
    def test_http_artifactory_ping_and_manifest_use_configured_http(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                paths.append(self.path)
                body = b'{"config":{},"layers":[]}' if self.path.endswith("manifest.json") else b"OK"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        host = "127.0.0.1:{}".format(server.server_port)
        settings = {"repositories": {host: {"source": "artifactory", "insecureHttp": True}}}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for reference in ("", host + "/repo/image:1"):
                result = web_repository.diagnose(settings, host, reference)
                self.assertEqual(result["地址"], "http://" + host)
        self.assertEqual(paths, ["/artifactory/api/system/ping", "/artifactory/repo/image/1/manifest.json"])
        self.assertIn("http://" + host, output.getvalue())

    def test_artifactory_tls_failure_does_not_fall_back_to_http_or_expose_password(self):
        host = "repository.example"
        settings = {"repositories": {host: {"source": "artifactory", "username": "test-login",
                                            "passwordEnv": "PIB_DIAGNOSTIC_SECRET"}},
                    "fast": {"profiles": {"test": {"baseUrl": "http://" + host + "/repo/image:1"}}}}
        output = io.StringIO()
        failure = URLError(ssl.SSLCertVerificationError("test-login:sensitive-password"))
        with patch.dict(os.environ, {"PIB_DIAGNOSTIC_SECRET": "sensitive-password"}), \
                patch.object(web_repository, "make_opener", return_value=Mock()) as opener, \
                patch.object(web_repository, "request", side_effect=failure) as request, \
                contextlib.redirect_stdout(output):
            with self.assertRaises(BuildError) as result:
                web_repository.diagnose(settings, host)
        self.assertEqual(opener.call_args[0][0], "https://" + host)
        self.assertEqual(request.call_count, 1)
        message = str(result.exception)
        self.assertIn("https://" + host, message)
        self.assertIn("insecureHttp=true", message)
        self.assertIn("caFile", message)
        self.assertNotIn("sensitive-password", message + output.getvalue())
        self.assertNotIn("test-login", message + output.getvalue())

    def test_registry_tls_failure_reports_actual_origin_and_keeps_https(self):
        host = "registry.example"
        client = Mock(origin="https://" + host)
        client.request.side_effect = ssl.SSLCertVerificationError("certificate rejected")
        with patch.object(web_repository, "RegistryClient", return_value=client) as constructor, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(BuildError) as result:
                web_repository.diagnose({"repositories": {host: {}}}, host)
        self.assertIs(constructor.call_args[0][4], False)
        self.assertEqual(client.request.call_count, 1)
        self.assertIn("https://" + host, str(result.exception))

    def test_missing_ca_path_is_distinguished_from_tls_validation_failure(self):
        host = "repository.example"
        with patch.object(web_repository, "make_opener", side_effect=FileNotFoundError("missing CA")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(BuildError) as result:
                web_repository.diagnose({"repositories": {host: {"source": "artifactory"}}}, host)
        self.assertIn("CA 文件不存在", str(result.exception))
        self.assertIn("https://" + host, str(result.exception))


if __name__ == "__main__":
    unittest.main()
