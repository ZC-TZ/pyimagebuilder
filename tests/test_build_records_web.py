"""用回环 HTTP 验证网页读取同一台账及浏览器访问边界。"""

import http.client
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

import build_records
import build_records_web


class BuildRecordsWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "records"
        self.server = build_records_web.make_server(self.directory, port=0)
        self.addCleanup(self.server.server_close)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/", method="GET", headers=None):
        with_connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            with_connection.request(method, path, headers=headers or {})
            result = with_connection.getresponse()
            return result.status, dict(result.getheaders()), result.read().decode("utf-8")
        finally:
            with_connection.close()

    def record(self, tag, project, path="/exports/example.tar"):
        build_records.record_build(self.directory, tag, "linux/amd64", "docker", path,
                                   mode="fast", size=25, project=project, version="1", profile="tomcat")

    def test_refresh_reads_new_records_and_filters_without_requiring_tar(self):
        self.assertIn("没有符合条件的记录", self.request()[2])
        self.record("other_back:1", "other")
        self.record("hisq_front:1", "hisq", "/exports/hisq_front-1.tar")
        status, headers, page = self.request("/?" + urlencode({"project": "hisq", "tag": "hisq_front:1"}))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("hisq_front-1.tar", page)
        self.assertIn("tomcat", page)
        self.assertNotIn("other_back:1", page)
        self.assertIn("other_back:1", self.request()[2])

    def test_pagination_keeps_filters_and_handles_page_end(self):
        for number in range(26):
            self.record("app:{}".format(number), "hisq")
        self.record("other:1", "other")
        first = self.request("/?project=hisq")[2]
        self.assertEqual(first.count("<article>"), 25)
        self.assertIn('href="/?project=hisq&amp;page=2"', first)
        last = self.request("/?project=hisq&page=2")[2]
        self.assertEqual(last.count("<article>"), 1)
        self.assertIn("上一页", last)
        self.assertNotIn("下一页", last)

    def test_escapes_record_content_and_does_not_expose_files_or_allow_foreign_host(self):
        text = '"><script>alert(1)</script>'
        self.record("hisq:1", text, "/exports/" + text)
        status, headers, page = self.request("/?" + urlencode({"project": text}))
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertEqual(self.request(headers={"Host": "localhost:{}".format(self.server.server_port)})[0], 200)
        self.assertEqual(self.request(headers={"Host": "rebind.example"})[0], 403)
        self.assertEqual(self.request("/exports/secret.tar")[0], 404)
        self.assertEqual(self.request("/?page=0")[0], 400)
        self.assertEqual(self.request(method="POST")[0], 501)

    def test_web_flag_uses_same_record_directory_and_passes_options(self):
        with patch.object(build_records, "records_directory", return_value=self.directory), \
                patch.object(build_records_web, "serve", return_value=0) as serve:
            self.assertEqual(build_records.main(["--web", "--port", "0", "--project", "hisq", "--limit", "8"]), 0)
        serve.assert_called_once_with(self.directory, 0, "hisq", None, 8)

    def test_browser_reset_before_request_line_does_not_report_server_error(self):
        finished = threading.Event()
        shutdown_request = self.server.shutdown_request

        def closed(request):
            shutdown_request(request)
            finished.set()

        with patch.object(self.server, "handle_error") as errors, \
                patch.object(self.server, "shutdown_request", side_effect=closed):
            with socket.create_connection(self.server.server_address, timeout=5) as browser:
                linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
                browser.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                browser.sendall(b"GET /")
            self.assertTrue(finished.wait(5))
            errors.assert_not_called()
        self.assertEqual(self.request()[0], 200)
