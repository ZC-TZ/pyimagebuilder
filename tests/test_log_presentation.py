"""验证展示改造保留文本、机器字段和有界日志窗口，不由应用文字推断执行结果。"""

import io
import json
import unittest
from collections import deque

from progress import make_reporter
from progress.events import BuildEvent
from progress.presentation import clock, terminal_width, timing_lines
from progress.renderer_plain import PlainRenderer
from progress.renderer_tty import TTYRenderer
from web_jobs import Jobs
from web_log import event_kind, log_lines, log_metadata, log_record, pull_flow, pull_view


class LogPresentationTests(unittest.TestCase):
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
