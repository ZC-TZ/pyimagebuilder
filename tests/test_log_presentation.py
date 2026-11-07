"""验证展示改造保留文本、机器字段和有界日志窗口，不由应用文字推断执行结果。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from progress import make_reporter
from progress.events import BuildEvent
from progress.presentation import clock, terminal_width, timing_lines
from progress.renderer_plain import PlainRenderer
from progress.renderer_json import JsonRenderer
from progress.renderer_tty import TTYRenderer
from web_jobs import Jobs
from web_log import event_kind, log_lines, log_metadata, log_record, pull_flow, pull_view


class LogPresentationTests(unittest.TestCase):
    def test_real_build_succeeds_with_ascii_stdout_and_unicode_output_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"application")
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /app/payload\n", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="ascii",
                       PYIMAGEBUILDER_BUILD_RECORDS_DIR=str(root / "records"))
            for mode in ("plain", "json"):
                with self.subTest(mode=mode):
                    target = root / ("镜像-\U0001f4e6-" + mode + ".tar")
                    result = subprocess.run([sys.executable, "main.py", "build", str(context), "--offline",
                                             "--no-cache", "-t", "example:1", "-o", str(target),
                                             "--progress=" + mode], cwd=str(Path(__file__).resolve().parents[1]),
                                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", errors="replace"))
                    text = result.stdout.decode("ascii")
                    self.assertTrue(target.is_file())
                    if mode == "json":
                        final = json.loads(text.splitlines()[-1])
                        self.assertEqual(final["type"], "build_success")
                        self.assertEqual(final["details"]["output"], str(target))
                    else:
                        self.assertIn("Build succeeded", text)

    def test_encoding_fallback_does_not_swallow_output_io_failures(self):
        class Disconnected:
            encoding = "ascii"

            def write(self, value):
                raise BrokenPipeError("output disconnected")

        for renderer in (PlainRenderer(Disconnected()), JsonRenderer(Disconnected()),
                         TTYRenderer(Disconnected(), color=False)):
            with self.subTest(renderer=type(renderer).__name__):
                with self.assertRaises(BrokenPipeError):
                    renderer.render(BuildEvent("phase_start", 0, message="Preparing build"))

    def test_json_roundtrip_on_strict_streams_and_line_separator_characters(self):
        message = "准备镜像 \U0001f4e6 path-\udcff \u2028 next \u2029 end"
        event = BuildEvent("step_log", 1, message=message, details={"path": "nested-\ud800"})
        for encoding in ("ascii", "gbk", "utf-8"):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict")
                try:
                    JsonRenderer(stream).render(event)
                    stream.flush()
                    lines = raw.getvalue().decode(encoding).splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(json.loads(lines[0]), event.as_dict())
                finally:
                    stream.close()

    def test_human_logs_escape_only_characters_the_stream_cannot_encode(self):
        for encoding in ("ascii", "gbk", "utf-8"):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict")
                try:
                    renderer = PlainRenderer(stream, error_stream=stream)
                    renderer.render(BuildEvent("phase_start", 0, message="Preparing build"))
                    renderer.render(BuildEvent("build_failed", 1, message="path-\udcff \U0001f4e6"))
                    stream.flush()
                    text = raw.getvalue().decode(encoding)
                    self.assertIn("Build failed", text)
                    self.assertIn(r"path-\udcff", text)
                    if encoding == "utf-8":
                        self.assertIn("解析构建输入", text)
                        self.assertIn("\U0001f4e6", text)
                finally:
                    stream.close()

    def test_tty_fits_escaped_text_to_actual_terminal_width(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        try:
            renderer = TTYRenderer(stream, color=False)
            with patch("progress.renderer_tty.shutil.get_terminal_size", return_value=SimpleNamespace(columns=36)):
                renderer.render(BuildEvent("heartbeat", 0, message="Preparing build", duration=1))
            stream.flush()
            text = raw.getvalue().decode("ascii").lstrip("\r")
            self.assertLessEqual(len(text), 35)
            self.assertEqual(renderer.width, len(text))
        finally:
            stream.close()

    def collector_job(self):
        jobs = Jobs.__new__(Jobs)
        jobs.condition = threading.Condition()
        jobs.journal = Mock()
        job = {"logs": deque(maxlen=250), "log_meta": deque(maxlen=250), "log_end": 0,
               "delivery": False, "outputs": [], "state": "running", "status": ""}
        return jobs, job

    def test_invalid_event_times_cannot_stop_collection_or_hide_final_error(self):
        for elapsed in (10 ** 1000, float("nan"), float("inf"), -1, True, "2", None):
            with self.subTest(elapsed=repr(elapsed)[:40]):
                jobs, job = self.collector_job()
                events = [{"type": "layer_start", "elapsed": elapsed,
                           "details": {"group_id": "g", "layer_index": 1}},
                          {"type": "heartbeat", "elapsed": 4.0},
                          {"type": "build_failed", "elapsed": 5, "message": "original build failure"}]
                encoded = [json.dumps(event) for event in events]
                process = Mock(stdout=io.BytesIO(("\n".join(encoded) + "\n").encode()),
                               wait=Mock(return_value=1))
                jobs._collect(job, process)
                process.wait.assert_called_once_with()
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["logs"][0], encoded[0][:3999] + "…" if len(encoded[0]) > 4000 else encoded[0])
                self.assertIn("original build failure", "\n".join(job["logs"]))

    def test_final_timing_and_warning_include_cleanup(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                jobs, job = self.collector_job()
                now = [110]
                job["started"] = 100

                def cleanup():
                    now[0] = 125
                    if fails:
                        raise OSError("cleanup warning")

                job["cleanup"] = cleanup
                with patch("web_jobs.time.monotonic", side_effect=lambda: now[0]):
                    jobs._finish(job)
                self.assertEqual(job["elapsed"], 25)
                self.assertNotIn("started", job)
                if fails:
                    self.assertEqual(job["log_meta"][-1], {"kind": "warning", "elapsed": 25})

    def test_journal_warning_keeps_time_after_task_has_finished(self):
        jobs, job = self.collector_job()
        job["elapsed"] = 25
        jobs.journal.write.side_effect = OSError("journal unavailable")
        jobs._save(job)
        self.assertEqual(job["log_meta"][-1], {"kind": "warning", "elapsed": 25})

    def test_timing_table_sorts_and_aligns_by_display_columns(self):
        rows = list(timing_lines({"Wait for Artifactory layers": 2,
                                  "Download Artifactory files": 142.61, "custom operation": 0}))
        self.assertIn("下载基础镜像文件", rows[0])
        self.assertIn("等待剩余层", rows[1])
        self.assertIn("custom operation", rows[2])
        right_edges = [terminal_width(row[:row.index(value)]) + len(value)
                       for row, value in zip(rows, ("142.61", "2.00", "0.00"))]
        self.assertEqual(len(set(right_edges)), 1)
        self.assertNotIn("#", rows[-1])
        self.assertIn("剩余等待", rows[1])

    def test_only_lifecycle_phase_names_are_translated(self):
        stream = io.StringIO()
        renderer = PlainRenderer(stream)
        renderer.render(BuildEvent("phase_start", 0, message="Preparing build"))
        renderer.render(BuildEvent("step_log", 1, message="Preparing build"))
        self.assertIn("[+] 解析构建输入", stream.getvalue())
        self.assertIn("> Preparing build", stream.getvalue())

    def test_plain_heartbeat_is_quieter_and_verbose_can_expand_it(self):
        for verbose, expected in ((False, 2), (True, 7)):
            stream = io.StringIO()
            renderer = PlainRenderer(stream, verbose=verbose)
            for elapsed in range(7):
                renderer.render(BuildEvent("heartbeat", elapsed, message="Working", duration=elapsed))
            self.assertEqual(len(stream.getvalue().splitlines()), expected)

    def test_machine_events_and_timing_keys_are_unchanged(self):
        output = io.StringIO()
        reporter = make_reporter(progress="json", stream=output)
        self.addCleanup(reporter.close)
        for i in range(3):
            reporter.renderer.render(BuildEvent("heartbeat", i, message="Preparing build"))
        reporter.record_timing("Download Artifactory files", 2)
        reporter.success(image="test:1")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(sum(e["type"] == "heartbeat" for e in events), 3)
        self.assertEqual(events[-1]["details"]["timings"], {"Download Artifactory files": 2})

    def test_raw_application_output_is_not_classified_as_build_failure(self):
        self.assertEqual(event_kind({"type": "step_log", "message": "FAILED OK CACHED"}), "output")
        self.assertEqual(event_kind({"type": "layer_failed"}), "error")
        self.assertEqual(event_kind({"type": "step_cache_hit"}), "cache")

    def test_metadata_rotates_with_raw_text_and_line_numbers_keep_increasing(self):
        job = {"logs": deque(maxlen=3), "log_meta": deque(maxlen=3), "log_end": 0}
        for index in range(6):
            Jobs._append_log(job, "line " + str(index), "success" if index % 2 else "stage", index)
        self.assertEqual(list(job["logs"]), ["line 3", "line 4", "line 5"])
        self.assertEqual(log_metadata(job), [{"kind": "success", "time": "00:03"},
                                           {"kind": "stage", "time": "00:04"},
                                           {"kind": "success", "time": "00:05"}])
        html = log_lines(job)
        self.assertIn('data-line="4"', html)
        self.assertIn('data-line="6"', html)

    def test_log_rows_escape_content_and_reject_custom_css_classes(self):
        job = {"logs": ['<script>alert("test")</script>'], "log_end": 1,
               "log_meta": [{"kind": 'error" onclick="bad', "elapsed": 3}]}
        html = log_lines(job)
        self.assertIn('class="log-line log-output"', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_metadata_accepts_only_bounded_scalar_values(self):
        for value in ({}, [], float("nan"), float("inf"), 10 ** 1000, -1, True):
            self.assertEqual(log_record([], value), {"kind": "output", "elapsed": None})
            self.assertEqual(clock(value), "--:--")

    def test_legacy_logs_without_metadata_still_render(self):
        for metadata in (None, [], [{}]):
            job = {"logs": ["line one", "line two"], "log_end": 2, "log_meta": metadata}
            self.assertEqual(len(log_metadata(job)), 2)
            self.assertIn("line two", log_lines(job))
            self.assertIn('data-time="--:--"', log_lines(job))

    def test_pull_flow_distinguishes_stopped_work_from_ready_base_image(self):
        job = {"state": "failed", "pull": {"stage": "ingest", "store_total": 22, "store_completed": 7}}
        html = pull_flow(job)
        self.assertIn("flow-stopped", html)
        self.assertIn("未执行", html)
        job["pull"]["stage"] = "ready"
        html = pull_flow(job)
        self.assertNotIn("flow-stopped", html)
        self.assertEqual(html.count('class="flow-done"'), 5)

    def test_pull_view_does_not_retain_arbitrary_nested_event_data(self):
        raw = {"stage": "transfer", "image": "<script>", "files_total": 24,
               "files_completed": [], "prepare_total": 10 ** 1000, "extra": {"secret": "ignored"}}
        self.assertEqual(pull_view(raw), {"stage": "transfer", "image": "<script>", "files_total": 24})
        self.assertNotIn("<script>", pull_flow({"state": "running", "pull": raw}))

    def test_tty_failure_clears_transient_line_before_error_output(self):
        stream, errors = io.StringIO(), io.StringIO()
        renderer = TTYRenderer(stream, color=False)
        renderer.error_stream = errors
        renderer.render(BuildEvent("heartbeat", 1, message="Working"))
        renderer.render(BuildEvent("build_failed", 2, message="expected error", duration=2))
        self.assertEqual(renderer.width, 0)
        self.assertIn("Build failed", errors.getvalue())
        self.assertNotIn("\x1b", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
