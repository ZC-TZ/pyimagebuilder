"""用回环 HTTP 验证网页读取同一台账及浏览器访问边界。"""

import http.client
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import build_records
import build_records_web
from web_workbench import Workbench
from settings import load_settings
from test_phase1 import make_base
from test_phase11 import RegistryHandler
from web_delivery import library_items


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

    def test_deeply_nested_build_record_returns_a_ledger_error_page(self):
        self.record("hisq:1", "hisq")
        record, = self.directory.glob("*.json")
        record.write_text("[" * 8000 + "0" + "]" * 8000, encoding="utf-8")
        with patch.object(build_records_web.sys, "stderr", new=io.StringIO()) as terminal:
            status, _, page = self.request()
        self.assertEqual(status, 500)
        self.assertIn("暂时无法读取台账", page)
        self.assertIn("nested too deeply", terminal.getvalue())

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
        return self.run_request("/submit/" + action, data)

    def run_request(self, endpoint, data, expected="success"):
        status, headers, body = self.http(endpoint, data)
        self.assertEqual(status, 303, body)
        route = headers["Location"][len(self.workspace.prefix):]
        identity = route.split("=", 1)[1]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.workspace.jobs.snapshot(identity)
            if job["state"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], expected, job["logs"])
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

    def test_job_progress_returns_only_new_lines_even_after_log_rotation(self):
        jobs = self.workspace.jobs
        with jobs.condition:
            identity = jobs.submit("fast", [], [], "hisq", {"project": "hisq"})
            jobs.queue.remove(identity)
            job = jobs.jobs[identity]
            for number in range(250):
                jobs._append_log(job, "line {}".format(number))
            job["state"] = "running"

        body = self.http("/job?id=" + identity)[2]
        self.assertNotIn(b'http-equiv="refresh"', body)
        self.assertIn(b'data-after="250"', body)
        self.assertIn(b"/job.js", body)
        with jobs.condition:
            jobs._append_log(job, "<script>literal log</script>")
            job.update(state="success", status="Done", code=0)

        url = "/job-progress?id=" + identity + "&after="
        self.assertEqual(self.http(url + "250", authenticated=False)[0], 404)
        self.assertEqual(self.http(url + "250&after=1")[0], 400)
        status, headers, raw = self.http(url + "250")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        progress = json.loads(raw)
        self.assertEqual((progress["start"], progress["end"]), (1, 251))
        self.assertEqual(progress["lines"], ["<script>literal log</script>"])
        self.assertEqual(progress["status"], "Done")
        self.assertFalse(progress["running"])
        self.assertIn("复用参数", progress["details"])
        self.assertEqual(json.loads(self.http(url + "251")[2])["lines"], [])
        finished = self.http("/job?id=" + identity)[2].decode("utf-8")
        self.assertIn('<span id="job-log-omitted">1</span>', finished)

    def test_collect_preserves_long_structured_log_and_drains_oversized_line(self):
        jobs = self.workspace.jobs
        with jobs.condition:
            identity = jobs.submit("build", [], [], "test")
            jobs.queue.remove(identity)
            job = jobs.jobs[identity]
            job["state"] = "running"
        long_message = json.dumps({"type": "step_log", "elapsed": 1, "message": "构" * 21500},
                                  ensure_ascii=True).encode("ascii")
        embedded_event = b'{"type":"step_log","elapsed":1,"message":"embedded"}'
        stream = io.BytesIO(long_message + b"\n" + b"x" * (1024 * 1024) + embedded_event +
                            b"\n" + b"  next physical line\n")
        jobs._collect(job, Mock(stdout=stream, wait=Mock(return_value=0)))
        logs = jobs.snapshot(identity)["logs"]
        self.assertEqual(len(logs), 3)
        self.assertTrue(logs[0].startswith("step_log: 构"))
        self.assertTrue(logs[0].endswith("…"))
        self.assertTrue(logs[1].endswith("…"))
        self.assertEqual(logs[2], "  next physical line")

    def test_progress_cannot_overwrite_cancelling_status(self):
        jobs = self.workspace.jobs
        with jobs.condition:
            identity = jobs.submit("build", [], [], "test")
            jobs.queue.remove(identity)
            job = jobs.jobs[identity]
            job.update(state="running", interrupt=True, status="Preparing")
        stream = io.BytesIO(b'{"type":"heartbeat","elapsed":1,"message":"Building"}\n')

        def finish():
            progress = self.workspace.job_progress(jobs.snapshot(identity), 0)
            self.assertTrue(progress["running"])
            self.assertEqual(progress["status"], "正在取消")
            return 130

        jobs._collect(job, Mock(stdout=stream, wait=finish))
        result = jobs.snapshot(identity)
        self.assertEqual(result["state"], "cancelled")
        self.assertNotEqual(result["status"], "正在取消")

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

    def built_delivery(self, kind="docker", suffix=""):
        context = self.root / ("delivery-context" + suffix)
        context.mkdir()
        (context / "Dockerfile").write_text("FROM scratch\nCOPY contents /contents\n", encoding="utf-8")
        (context / "contents").write_bytes(b"contents for registry")
        output = self.root / ("release" + suffix + ".tar")
        self.run_job("build", {"context": str(context), "tag": "hisq_front:1.0.0", "output": str(output),
                               "offline": "1", "no-cache": "1", "format": kind})
        row, = [row for row in build_records.list_builds(self.workspace.directory)
                if row["artifacts"][0]["path"] == str(output)]
        return output, {"scope": "history", "id": row["id"], "file": "0"}

    def test_history_actions_select_archive_and_library_row_without_path_input(self):
        output, source = self.built_delivery()
        menu = self.http("/delivery?" + urlencode(source))[2]
        self.assertIn(b"release.tar", menu)
        self.assertIn(b"hisq_front:1.0.0", menu)
        self.assertIn("产物操作", self.http("/history")[2].decode())
        self.run_request("/delivery/prepare", dict(source, action="verify"))
        self.run_request("/delivery/prepare", dict(source, action="load", archive=str(self.root / "unrelated.tar")))
        row, = library_items(self.workspace.settings)["images"]
        item, = row["delivery_items"]
        selector = {"scope": "library", "id": item["id"]}
        self.assertIn(item["id"].encode(), self.http("/images")[2])
        changed = self.root / "retagged.tar"
        _, job = self.run_request("/delivery/prepare", dict(selector, action="tag", output=str(changed),
                                    **{"new-tag": "hisq_front:1.0.1"}))
        self.assertEqual(self.http("/download?id=" + job + "&file=0")[2], changed.read_bytes())
        from image_cli import verify_archive
        self.assertEqual(verify_archive(changed, "hisq_front:1.0.1")["tag"], "hisq_front:1.0.1")
        data = output.read_bytes()
        output.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        _, failed = self.run_request("/delivery/prepare", dict(source, action="verify"), expected="failed")
        self.assertIn("SHA-256", "\n".join(self.workspace.jobs.snapshot(failed)["logs"]))

    def test_oci_actions_verify_import_and_export_cas(self):
        _, source = self.built_delivery("oci")
        self.run_request("/delivery/prepare", dict(source, action="verify"))
        self.assertEqual(self.http("/delivery/prepare", dict(source, action="tag"))[0], 400)
        self.run_request("/delivery/prepare", dict(source, action="load"))
        row, = library_items(self.workspace.settings)["images"]
        item, = row["delivery_items"]
        self.assertEqual(item["format"], "cas")
        target = self.root / "from-cas.tar"
        _, job = self.run_request("/delivery/prepare", {
            "scope": "library", "id": item["id"], "action": "save", "output": str(target)})
        self.assertEqual(self.http("/download?id=" + job + "&file=0")[2], target.read_bytes())

    def test_push_requires_fixed_single_use_confirmation_and_does_not_overwrite(self):
        class LocalRegistry(RegistryHandler):
            blobs, manifests, token_requests = {}, {}, 0

        registry = ThreadingHTTPServer(("127.0.0.1", 0), LocalRegistry)
        self.addCleanup(registry.server_close)
        thread = threading.Thread(target=registry.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(registry.shutdown)
        host = "127.0.0.1:{}".format(registry.server_port)
        entry = {"username": "robot$test", "passwordEnv": "PIB_WEB_TEST_PASSWORD", "insecureHttp": True}
        config = json.loads(self.workspace.config.read_text(encoding="utf-8"))
        config["repositories"][host] = entry
        self.workspace.config.write_text(json.dumps(config), encoding="utf-8")
        self.workspace.settings["repositories"][host] = entry
        _, source = self.built_delivery()
        destination = host + "/p/app:delivery"
        fields = dict(source, action="push", destination=destination)
        self.assertEqual(self.http("/delivery/prepare", dict(fields, destination="unknown.test/p/app:1"))[0], 400)

        def prepare():
            status, _, page = self.http("/delivery/prepare", fields)
            self.assertEqual(status, 200, page)
            self.assertIn(destination.encode(), page)
            self.assertIn(b"hisq_front:1.0.0", page)
            return re.search(rb'name="token" value="([^"]+)"', page).group(1).decode("ascii")

        token = prepare()
        self.assertEqual(LocalRegistry.token_requests, 0)
        self.assertEqual(self.http("/delivery/confirm", {"token": token, "destination": host + "/p/app:other"})[0], 400)
        with patch.dict(os.environ, {"PIB_WEB_TEST_PASSWORD": "secret"}):
            self.run_request("/delivery/confirm", {"token": token})
            self.assertEqual(self.http("/delivery/confirm", {"token": token})[0], 400)
            pushed = LocalRegistry.manifests["delivery"]
            self.run_request("/delivery/confirm", {"token": prepare()}, expected="failed")
        self.assertEqual(LocalRegistry.manifests, {"delivery": pushed})
        _, source = self.built_delivery("oci", "-oci")
        destination = host + "/p/app:delivery-oci"
        fields = dict(source, action="push", destination=destination)
        with patch.dict(os.environ, {"PIB_WEB_TEST_PASSWORD": "secret"}):
            self.run_request("/delivery/confirm", {"token": prepare()})
        self.assertEqual(set(LocalRegistry.manifests), {"delivery", "delivery-oci"})

    def confirmation_token(self, result):
        status, _, page = result
        self.assertEqual(status, 200, page)
        return re.search(rb'name="token" value="([^"]+)"', page).group(1).decode("ascii")

    def test_confirmation_remains_usable_when_queue_rejects_submission(self):
        from concurrent.futures import ThreadPoolExecutor
        from errors import BuildError
        token = self.workspace.delivery.offer({"action": "cache-clean"})
        with patch.object(self.workspace, "submit_delivery", side_effect=BuildError("Web job queue is full")):
            self.assertEqual(self.http("/cache/confirm", {"token": token})[0], 400)
        with patch.object(self.workspace, "submit_delivery", return_value="a" * 32) as submit:
            start = threading.Barrier(2)

            def confirm():
                start.wait(5)
                return self.http("/cache/confirm", {"token": token})[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(confirm), pool.submit(confirm)
                self.assertEqual(sorted((first.result(), second.result())), [303, 400])
            submit.assert_called_once_with({"action": "cache-clean"})

    def test_unreadable_job_summary_does_not_break_the_task_list(self):
        journal = self.workspace.jobs.journal
        good, broken = "a" * 32, "b" * 32
        journal.write({"id": good, "action": "fast", "state": "success", "label": "kept",
                       "form": {"project": "kept"}})
        (journal.directory / (broken + ".json")).write_text(
            '{"form":' + "[" * 8000 + "0" + "]" * 8000 + "}", encoding="utf-8")
        self.assertEqual(self.http("/job?id=" + broken)[0], 404)
        status, _, body = self.http("/jobs")
        self.assertEqual(status, 200)
        self.assertIn(b"kept", body)

    def test_fast_preview_and_submit_report_unresolvable_package_path(self):
        package = self.root / "loop.war"
        fields = {"package": str(package), "project": "hisq", "version": "1", "profile": "tomcat"}
        real_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == package:
                raise RuntimeError("Symlink loop")
            return real_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            status, _, body = self.http("/preview-fast", fields)
            self.assertEqual(status, 400)
            self.assertIn("Symlink loop", json.loads(body)["error"])
            self.assertEqual(self.http("/submit/fast", fields)[0], 400)
        self.assertFalse(self.workspace.jobs.snapshot())

    def test_cache_cleanup_rechecks_candidates_and_refuses_active_builds(self):
        from cache import LayerCache
        from cache_activity import cache_activity
        root = self.workspace.settings["cache"]["layerCache"]
        cache = LayerCache(root)
        cache.store("a" * 64, None, None)
        entry = cache.entries / ("a" * 64 + ".json")
        os.utime(entry, (1000, 1000))
        preview = "/space?kind=cache&days=30"
        old = self.confirmation_token(self.http(preview))
        self.assertTrue(entry.is_file())
        entry.write_bytes(entry.read_bytes() + b" ")
        os.utime(entry, (1000, 1000))
        self.run_request("/cache/confirm", {"token": old}, expected="failed")
        self.assertTrue(entry.is_file())
        token = self.confirmation_token(self.http(preview))
        with cache_activity(root):
            self.run_request("/cache/confirm", {"token": token}, expected="failed")
        self.assertTrue(entry.is_file())
        token = self.confirmation_token(self.http(preview))
        self.run_request("/cache/confirm", {"token": token})
        self.assertFalse(entry.exists())
        self.assertEqual(self.http("/cache/confirm", {"token": token})[0], 400)

    def test_template_test_preview_and_confirm_reject_stale_configuration(self):
        original = self.workspace.config.read_bytes()
        base = self.workspace.profiles["tomcat"]["baseTar"]
        fields = {"mode": "new", "name": "tomcat-local", "flavor": "tomcat",
                  "baseImage": "example/base:1", "baseTar": base, "owner": "0:0",
                  "outputDir": "another-output", "default": "1"}
        self.assertEqual(self.http("/profile")[0], 200)
        self.run_request("/profile/test", fields)
        self.assertEqual(self.workspace.config.read_bytes(), original)
        token = self.confirmation_token(self.http("/profile/preview", fields))
        self.workspace.config.write_bytes(original + b"\n")
        self.assertEqual(self.http("/profile/confirm", {"token": token})[0], 400)
        token = self.confirmation_token(self.http("/profile/preview", fields))
        self.assertEqual(self.http("/profile/confirm", {"token": token})[0], 303)
        value = json.loads(self.workspace.config.read_text(encoding="utf-8"))
        self.assertEqual(value["fast"]["defaultProfile"], "tomcat-local")
        self.assertEqual(value["repositories"], json.loads(original)["repositories"])
        self.assertIn(b"tomcat-local", self.http("/workbench")[2])
        token = self.confirmation_token(self.http("/profile/preview", dict(fields, mode="edit", owner="1:1")))
        self.assertEqual(self.http("/profile/confirm", {"token": token})[0], 303)
        self.assertEqual(self.workspace.profiles["tomcat-local"]["owner"], "1:1")

    def test_remote_template_probes_only_verified_manifest_and_config(self):
        import base64
        import hashlib
        from http.server import BaseHTTPRequestHandler
        data = json.dumps({"os": "linux", "architecture": "amd64",
                           "rootfs": {"type": "layers", "diff_ids": []}}).encode()
        digest = hashlib.sha256(data).hexdigest()
        manifest = json.dumps({"config": {"digest": "sha256:" + digest, "size": len(data)}, "layers": []}).encode()
        requests = []

        class Artifactory(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                body = {"/artifactory/repo/image/1/manifest.json": manifest,
                        "/artifactory/repo/image/1/sha256__" + digest: data}.get(self.path)
                expected = "Basic " + base64.b64encode(b"test:secret").decode("ascii")
                self.send_response(401 if self.headers.get("Authorization") != expected else (200 if body else 404))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        remote = ThreadingHTTPServer(("127.0.0.1", 0), Artifactory)
        self.addCleanup(remote.server_close)
        thread = threading.Thread(target=remote.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(remote.shutdown)
        host = "127.0.0.1:{}".format(remote.server_port)
        config = json.loads(self.workspace.config.read_text(encoding="utf-8"))
        config["repositories"][host] = {"source": "artifactory", "username": "test", "passwordEnv": "PIB_PROFILE_TEST"}
        original = json.dumps(config).encode()
        self.workspace.config.write_bytes(original)
        fields = {"mode": "new", "name": "remote", "flavor": "tomcat", "baseImage": host + "/repo/image:1",
                  "baseUrl": "http://" + host + "/artifactory/repo/image/1/"}
        with patch.dict(os.environ, {"PIB_PROFILE_TEST": "secret"}):
            self.run_request("/profile/test", fields)
        self.assertEqual(requests, ["/artifactory/repo/image/1/manifest.json",
                                    "/artifactory/repo/image/1/sha256__" + digest])
        self.assertEqual(self.workspace.config.read_bytes(), original)

    def test_archive_analysis_history_optimization_flatten_and_logs(self):
        original, source = self.built_delivery()
        self.run_request("/delivery/prepare", dict(source, action="load"))
        self.assertIn(b"release", self.http("/delivery?" + urlencode(source))[2])
        match = self.http("/images?search=hisq&platform=linux%2Famd64&source=local")[2]
        self.assertIn(b"hisq_front:1.0.0", match)
        self.assertNotIn(b"hisq_front:1.0.0", self.http("/images?source=artifactory")[2])
        _, history = self.run_request("/delivery/prepare", dict(source, action="history"))
        status, headers, raw = self.http("/job-log?id=" + history)
        self.assertEqual(status, 200)
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertIn(b'"history"', raw)
        _, analysis = self.run_request("/delivery/prepare", dict(source, action="analyze"))
        self.assertIn("archive_bytes", self.workspace.jobs.snapshot(analysis)["metrics"])
        for action in ("optimize", "flatten"):
            output = self.root / (action + ".tar")
            route, identity = self.run_request("/delivery/prepare", dict(source, action=action, output=str(output)))
            metrics = self.workspace.jobs.snapshot(identity)["metrics"]
            self.assertEqual(metrics["before_bytes"], original.stat().st_size)
            self.assertEqual(metrics["after_bytes"], output.stat().st_size)
            self.assertIn("体积差", self.http(route)[2].decode("utf-8"))

    def test_advanced_build_attest_and_multiarch_outputs(self):
        from attest import keygen
        context = self.root / "advanced-context"
        context.mkdir()
        (context / "Dockerfile").write_text("FROM example/base:1\nARG NOTE\nLABEL test=$NOTE\n", encoding="utf-8")
        base_map = self.root / "map.json"
        base_map.write_text(json.dumps({"example/base:1": self.workspace.profiles["tomcat"]["baseTar"]}), encoding="utf-8")
        private = self.root / "signing.key"
        keygen(private, self.root / "signing.pub")
        archive = self.root / "signed.tar"
        _, identity = self.run_job("build", {"context": str(context), "output": str(archive), "tag": "advanced:1",
            "base-map": str(base_map), "epoch": "100", "build-args": "NOTE=web-build", "sign-key": str(private),
            "offline": "1", "no-cache": "1"})
        from image_cli import inspect_archive
        self.assertEqual(inspect_archive(archive)["config"]["Labels"]["test"], "web-build")
        job = self.workspace.jobs.snapshot(identity)
        self.assertEqual(len(job["files"]), 4)
        self.assertNotIn("sign-key", job["form"])
        self.assertNotIn("build-args", job["form"])
        (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        archives = {}
        for arch in ("amd64", "arm64"):
            output = self.root / (arch + ".oci.tar")
            self.run_job("build", {"context": str(context), "output": str(output), "tag": "multi:1",
                                  "platform": "linux/" + arch, "format": "oci", "offline": "1", "no-cache": "1"})
            archives[arch] = str(output)
        output = self.root / "combined.oci.tar"
        self.run_job("multiarch", dict(archives, tag="multi:1", output=str(output), epoch="100"))
        import tarfile
        with tarfile.open(output, "r:") as bundle:
            index = json.load(bundle.extractfile("index.json"))
        self.assertEqual({item["platform"]["architecture"] for item in index["manifests"]}, {"amd64", "arm64"})

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
