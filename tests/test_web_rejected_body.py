"""确定性检查拒绝上传/表单时的未读请求体收尾，不依赖 Windows 重置连接的概率。"""

import io
import json
import socket
import sys
import tempfile
import unittest
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_records_web import make_server
from errors import BuildError
from web_uploads import FastUploads


class BodyStream(io.BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.requests = []

    def read1(self, size):
        self.requests.append(size)
        return self.read(size)


class RejectedBodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = SimpleNamespace(prefix="/session/test", uploads=FastUploads())
        self.workspace.preview_fast = Mock(side_effect=BuildError("invalid preview"))
        self.addCleanup(self.workspace.uploads.close)
        self.server = make_server(Path(temp.name), 0, workspace=self.workspace)
        self.addCleanup(self.server.server_close)

    def request(self, route, raw=b"abc", content_type="application/octet-stream", size=None):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.server = self.server
        handler.command = "POST"
        handler.path = self.workspace.prefix + route
        handler.headers = HTTPMessage()
        host = "127.0.0.1:" + str(self.server.server_port)
        for key, value in (("Host", host), ("Origin", "http://" + host),
                           ("Content-Type", content_type), ("Content-Length", str(len(raw) if size is None else size))):
            handler.headers[key] = value
        handler.rfile = BodyStream(raw)
        handler.wfile = io.BytesIO()
        handler.request = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_invalid_filename_drains_small_upload_before_json_reply(self):
        handler = self.request("/upload-fast?name=other.zip")
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 3)
        handler.send_response.assert_called_once_with(400)
        self.assertIn("error", json.loads(handler.wfile.getvalue()))
        self.assertFalse(self.workspace.uploads.entries)

    def test_invalid_upload_headers_and_route_also_drain_small_body(self):
        for route, kind in (("/upload-fast?name=x.war", "text/plain"),
                            ("/upload-path?kind=unknown&name=x.tar", "application/octet-stream"),
                            ("/not-an-endpoint", "application/octet-stream"),
                            ("/preview-fast", "application/json")):
            with self.subTest(route=route):
                handler = self.request(route, content_type=kind)
                handler.do_POST()
                self.assertEqual(handler.rfile.tell(), 3)

    def test_partial_upload_only_drains_remaining_bytes(self):
        handler = self.request("/upload-fast?name=x.war", b"abcdef")

        def incomplete(source, *args):
            source.read1(2)
            raise BuildError("upload rejected after partial read")

        self.workspace.uploads.receive = incomplete
        handler.do_POST()
        self.assertEqual(handler.rfile.requests, [2, 4])
        self.assertEqual(handler.rfile.tell(), 6)
        handler.send_response.assert_called_once_with(400)

    def test_complete_body_is_not_read_again_after_processing_error(self):
        handler = self.request("/preview-fast", b"project=x", "application/x-www-form-urlencoded")
        self.workspace.preview_fast = Mock(side_effect=BuildError("invalid project"))
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 9)
        self.assertEqual(handler.rfile.requests, [])
        handler.request.settimeout.assert_not_called()

    def test_large_or_ambiguous_request_is_not_drained(self):
        for size in (32769, 2 ** 31, "bad"):
            with self.subTest(size=size):
                handler = self.request("/upload-fast?name=other.zip", size=size)
                handler.do_POST()
                self.assertEqual(handler.rfile.tell(), 0)
                self.assertEqual(handler.rfile.requests, [])
        handler = self.request("/upload-fast?name=other.zip")
        handler.headers["Transfer-Encoding"] = "chunked"
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 0)
        handler = self.request("/upload-fast?name=other.zip")
        handler.headers["Content-Length"] = "3"
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 0)

    def test_drain_timeout_preserves_error_reply_and_restores_timeout(self):
        handler = self.request("/upload-fast?name=other.zip")
        handler.rfile.read1 = Mock(side_effect=socket.timeout("body unavailable"))
        handler.do_POST()
        handler.send_response.assert_called_once_with(400)
        self.assertEqual(handler.request.settimeout.call_args[0], (5,))


if __name__ == "__main__":
    unittest.main()
