"""用回环 HTTP 验证网页读取同一台账及浏览器访问边界。"""

import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

import build_records
import build_records_web
from web_workbench import Workbench
from settings import load_settings
from test_phase1 import make_base


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
        self.assertEqual(serve.call_args[0], (self.directory, 0, "hisq", None, 8))
        self.assertIn("settings", serve.call_args[1])

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


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "schemaVersion": 2, "cache": {"imageStore": "store", "layerCache": "cache"},
            "repositories": {"harbor.example": {"username": "not-for-browser", "passwordEnv": "NO_EXPOSE"}},
            "fast": {"defaultProfile": "tomcat", "profiles": {"tomcat": {
                "baseImage": "example/base:1", "baseTar": str(make_base(self.root)),
                "flavor": "tomcat", "owner": "0:0", "outputDir": "deliveries"}}},
        }), encoding="utf-8")
        settings, _ = load_settings(config)
        self.workspace = Workbench(settings, config, self.root / "records")
        self.addCleanup(self.workspace.close)
        self.server = build_records_web.make_server(self.workspace.directory, 0, workspace=self.workspace)
        self.addCleanup(self.server.server_close)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.worker.start()
        self.addCleanup(self.worker.join, 2)
        self.addCleanup(self.server.shutdown)

    def http(self, route, data=None, origin=True, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "http://127.0.0.1:{}".format(self.server.server_port) if origin else "http://external.example"
        try:
            path = (self.workspace.prefix if authenticated else "") + route
            connection.request("POST" if data is not None else "GET", path,
                               None if data is None else urlencode(data), headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def run_job(self, action, data):
        status, headers, body = self.http("/submit/" + action, data)
        self.assertEqual(status, 303, body)
        route = headers["Location"][len(self.workspace.prefix):]
        identity = route.split("=", 1)[1]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.workspace.jobs.snapshot(identity)
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "success", job["logs"])
        return route, identity

    def test_workbench_runs_build_and_serves_only_registered_unchanged_output(self):
        context = self.root / "ctx"
        context.mkdir()
        (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        output = self.root / "web.tar"
        route, identity = self.run_job("build", {"context": str(context), "tag": "web/test:1",
                                                "output": str(output), "offline": "1", "no-cache": "1"})
        self.assertEqual(self.http(route)[0], 200)
        self.assertIn(b"web/test:1", self.http("/history")[2])
        download = "/download?id=" + identity + "&file=0"
        self.assertEqual(self.http(download)[2], output.read_bytes())
        output.write_bytes(b"not the registered file")
        self.assertEqual(self.http(download)[0], 409)

    def test_fast_then_manage_local_image(self):
        war = self.root / "app.war"
        war.write_bytes(b"war")
        output = self.root / "fast.tar"
        self.run_job("fast", {"package": str(war), "project": "hisq", "version": "1",
                              "profile": "tomcat", "output": str(output)})
        self.run_job("load", {"archive": str(output)})
        self.assertIn(b"hisq_back:1", self.http("/images")[2])
        target = self.root / "saved.tar"
        _, identity = self.run_job("save", {"reference": "hisq_back:1", "output": str(target), "source": "local"})
        self.assertEqual(self.http("/download?id=" + identity + "&file=0")[2], target.read_bytes())
        self.run_job("verify", {"archive": str(target)})
        self.run_job("remove", {"reference": "hisq_back:1", "source": "local", "confirm": "hisq_back:1"})
        self.assertNotIn(b"hisq_back:1", self.http("/images")[2])
        self.assertIn(b"hisq_back:1", self.http("/history")[2])

    def test_fast_preview_matches_default_and_reuse_survives_a_new_workbench(self):
        import zipfile
        package = self.root / "dist.zip"
        with zipfile.ZipFile(package, "w") as bundle:
            bundle.writestr("dist/index.html", "index")
        fields = {"package": str(package), "project": "hisq", "version": "2.0.0",
                  "profile": "tomcat", "format": "both"}
        status, _, body = self.http("/preview-fast", fields)
        self.assertEqual(status, 200, body)
        plan = json.loads(body)
        output = self.root / "deliveries" / "hisq_front-2.0.0.tar"
        secondary = output.with_name("hisq_front-2.0.0.oci.tar")
        self.assertEqual(plan["outputs"], [str(output), str(secondary)])
        self.assertEqual(plan["tag"], "hisq_front:2.0.0")
        self.assertIn("hisqf", plan["deploy"])
        self.assertFalse(output.parent.exists())
        output.parent.mkdir()
        secondary.write_bytes(b"existing")
        self.assertEqual(json.loads(self.http("/preview-fast", fields)[2])["conflicts"], [str(secondary)])
        self.assertEqual(self.http("/submit/fast", fields)[0], 400)
        secondary.unlink()
        _, identity = self.run_job("fast", fields)
        self.assertTrue(output.is_file())
        self.assertTrue(secondary.is_file())
        fresh = Workbench(self.workspace.settings, self.workspace.config, self.workspace.directory)
        self.addCleanup(fresh.close)
        archived = fresh.get_job(identity)
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["state"], "success")
        self.assertEqual(archived["logs"], [])
        self.assertNotIn("output", archived["form"])
        self.assertIn(b'value="2.0.0"', self.http("/reuse?id=" + identity)[2])

    def test_history_checks_content_even_after_equal_size_replacement(self):
        import hashlib
        archive = self.root / "prior.tar"
        archive.write_bytes(b"original")
        record = build_records.record_build(
            self.workspace.directory, "hisq:1", "linux/amd64", "docker", archive,
            digest="sha256:" + hashlib.sha256(b"original").hexdigest())
        route = "/history-download?id=" + record.stem + "&file=0"
        self.assertEqual(self.http(route)[2], b"original")
        archive.write_bytes(b"replaced")
        self.assertEqual(self.http(route)[0], 409)
        self.assertEqual(self.http("/history-download?id=../config&file=0")[0], 404)
        archive.unlink()
        self.assertIn("文件已移走或删除", self.http("/history")[2].decode())

    def test_space_preview_lists_candidates_without_removing_files(self):
        store = self.workspace.settings["cache"]["imageStore"]
        store.mkdir()
        cached = store / "old.tar"
        cached.write_bytes(b"12345")
        delivery = self.root / "release.tar"
        delivery.write_bytes(b"1234567")
        for path in (cached, delivery):
            os.utime(path, (1000, 1000))
        build_records.record_build(self.workspace.directory, "release:1", "linux/amd64", "docker", delivery)
        status, _, page = self.http("/space?kind=images&days=30")
        self.assertEqual(status, 200, page)
        self.assertIn(b"old.tar", page)
        self.assertIn(b"5 B", page)
        self.assertIn(b"release.tar", self.http("/space?kind=deliveries&days=30")[2])
        self.assertTrue(cached.is_file())
        self.assertTrue(delivery.is_file())

    def test_config_redaction_session_and_origin_checks(self):
        self.assertEqual(self.http("/workbench", authenticated=False)[0], 404)
        self.assertEqual(self.http("/submit/df", {}, origin=False)[0], 403)
        self.assertEqual(self.http("/submit/df", {}, authenticated=False)[0], 404)
        self.assertEqual(self.http("/submit/unknown", {})[0], 400)
        self.assertEqual(self.http("/submit/remove", {"reference": "example/app:1", "confirm": "wrong"})[0], 400)
        self.assertEqual(self.workspace.jobs.snapshot(), [])
        config = self.http("/config")[2]
        self.assertIn(b"harbor.example", config)
        self.assertNotIn(b"not-for-browser", config)
        self.assertNotIn(b"NO_EXPOSE", config)
        self.assertEqual(self.http("/images")[0], 200)
