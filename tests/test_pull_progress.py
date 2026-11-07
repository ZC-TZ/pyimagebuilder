"""验证下载、并行准备、入库与引用就绪的显示边界和并发快照合并。"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import ArchiveError
from cas_store import CASStore
from progress import LayerProgress, make_reporter
from progress.events import BuildEvent
from progress.pull import PullProgress, format_pull_status
from progress.renderer_plain import PlainRenderer
from progress.renderer_tty import TTYRenderer, terminal_width
from test_artifactory_publication import artifact_files, download, layer_bytes, REFERENCE, PLATFORM, SOURCE
from test_layer_progress import fixture


class PullProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CASStore(self.root / "store")

    def reporter(self, mode="json"):
        output = io.StringIO()
        reporter = make_reporter(progress=mode, stream=output)
        self.addCleanup(reporter.close)
        return reporter, output

    def events(self, output):
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def transfer(self, files, reporter):
        import artifactory_download as artifacts

        def receive(_opener, _url, name, root, _size, _limiter, stats, _display):
            (root / name).write_bytes(files[name])
            stats.add(len(files[name]))

        with patch.object(artifacts, "probe_file_sizes", return_value={n: len(b) for n, b in files.items()}), \
                patch.object(artifacts, "download_one", side_effect=receive):
            artifacts.download_and_pack(object(), "https://example.test/repo/base/1/", list(files),
                self.root / "unused.tar", 2, REFERENCE, reporter=reporter,
                cas_store=self.store, cas_only=True)

    def test_milestones_distinguish_network_completion_and_reference_ready(self):
        reporter, output = self.reporter()
        inspect = self.store.inspect_ref

        def verify(*args, **kwargs):
            self.assertNotIn("ready", [e["details"]["stage"] for e in self.events(output)
                                      if e["type"] == "pull_status"])
            return inspect(*args, **kwargs)

        with patch.object(self.store, "inspect_ref", side_effect=verify):
            self.transfer(fixture(3), reporter)
        events = self.events(output)
        milestones = [e["details"]["stage"] for e in events if e["type"] == "pull_status" and
                      e["details"]["milestone"]]
        self.assertEqual(milestones, ["metadata", "transfer", "waiting", "ingest", "ready"])
        for operation in ("prepare", "store"):
            completed = [e for e in events if e["type"] == "layer_complete" and
                         e["details"]["operation"] == operation]
            self.assertEqual([e["details"]["layers_completed"] for e in completed], [1, 2, 3])
        ready = events[-1]["details"]
        self.assertEqual(ready["store_completed"], 3)
        self.assertIsNone(reporter._pull_state)
        self.assertFalse((self.root / "unused.tar").exists())

    def test_failed_reference_check_never_reports_ready_and_restores_old_ref(self):
        files = artifact_files(layer_bytes(b"old image"))
        download(files, self.root / "unused.tar", self.store)
        old = self.store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
        reporter, output = self.reporter()
        with patch.object(self.store, "inspect_ref", side_effect=ArchiveError("reference rejected")):
            with self.assertRaisesRegex(ArchiveError, "reference rejected"):
                self.transfer(artifact_files(layer_bytes(b"new image")), reporter)
        stages = [e["details"]["stage"] for e in self.events(output) if e["type"] == "pull_status"]
        self.assertNotIn("ready", stages)
        self.assertEqual(self.store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), old)

    def test_raw_blob_ingest_failure_is_visible_and_never_reports_ready(self):
        reporter, output = self.reporter()
        original = self.store._store_owned_blob

        def fail_raw(*args, **kwargs):
            if not kwargs.get("decoded"):
                raise ArchiveError("raw blob ingestion failed")
            return original(*args, **kwargs)

        with patch.object(self.store, "_store_owned_blob", side_effect=fail_raw):
            with self.assertRaisesRegex(ArchiveError, "raw blob ingestion failed"):
                self.transfer(fixture(1), reporter)
        events = self.events(output)
        self.assertTrue(any(e["type"] == "layer_failed" and e["details"]["operation"] == "store" for e in events))
        self.assertFalse(any(e["type"] == "pull_status" and e["details"]["stage"] == "ready" for e in events))
        self.assertFalse(self.store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_workspace_cleanup_failure_does_not_leave_a_ready_message(self):
        import artifactory_download as artifacts
        reporter, output = self.reporter()
        original = artifacts.tempfile.TemporaryDirectory

        class FailingWorkspace:
            def __init__(self, *args, **kwargs):
                self.inner = original(*args, **kwargs)

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *args):
                self.inner.__exit__(*args)
                raise OSError("workspace cleanup failed")

        with patch.object(artifacts.tempfile, "TemporaryDirectory", FailingWorkspace):
            with self.assertRaisesRegex(OSError, "workspace cleanup failed"):
                self.transfer(fixture(1), reporter)
        self.assertFalse(any(e["type"] == "pull_status" and e["details"]["stage"] == "ready"
                             for e in self.events(output)))
        self.assertFalse(self.store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_empty_image_has_honest_zero_layer_summary(self):
        reporter, output = self.reporter()
        self.transfer(fixture(0), reporter)
        ready = self.events(output)[-1]["details"]
        self.assertEqual((ready["prepare_total"], ready["store_total"], ready["store_completed"]), (0, 0, 0))
        self.assertIn("0/0 层", format_pull_status(ready))

    def test_old_dispatch_snapshot_cannot_reverse_worker_completion(self):
        reporter, output = self.reporter()
        group = LayerProgress("prepare", 2, reporter)
        pull = PullProgress(4, reporter)
        pull.preparation = group
        pull.update("transfer")
        stale = dict(pull.state)
        with group.layer(2):
            pass
        reporter.pull_status(dict(stale, stage="waiting"))
        state = self.events(output)[-1]["details"]
        self.assertEqual((state["prepare_completed"], state["prepare_active"], state["prepare_pending"]), (1, 0, 1))
        self.assertEqual(state["prepare_revision"], 2)
        self.assertEqual(state["stage"], "waiting")

    def test_new_group_does_not_inherit_previous_image_counts(self):
        reporter, output = self.reporter()
        for total in (3, 1):
            pull = PullProgress(total + 2, reporter)
            pull.preparation = LayerProgress("prepare", total, reporter)
            pull.update("transfer")
            state = self.events(output)[-1]["details"]
            self.assertEqual((state["prepare_total"], state["prepare_completed"]), (total, 0))
            with pull.preparation.layer(1):
                pass

    def test_stage_clock_is_independent_of_outer_phase_elapsed(self):
        reporter, output = self.reporter()
        reporter.phase_start("Resolving base image")
        with patch("progress.reporter.time.monotonic", return_value=100):
            reporter.pull_status({"stage": "waiting", "files_total": 5})
        with patch("progress.reporter.time.monotonic", return_value=107):
            state = reporter._pull_snapshot()
        text = format_pull_status(state)
        self.assertIn("本阶段 7.0s", text)
        stream = io.StringIO()
        PlainRenderer(stream).render(BuildEvent("heartbeat", 999, duration=999, details={"pull": state}))
        self.assertNotIn("999", stream.getvalue())

    def test_tty_updates_transient_snapshot_without_newline(self):
        output = io.StringIO()
        renderer = TTYRenderer(output, color=False)
        renderer.render(BuildEvent("pull_status", 0, details={"stage": "transfer", "files_total": 5}))
        self.assertNotIn("\n", output.getvalue())
        renderer.render(BuildEvent("pull_status", 1, details={"stage": "waiting", "milestone": True}))
        self.assertIn("网络传输完成", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\n"))

    def test_long_parallel_status_does_not_wrap_terminal_rows(self):
        output = io.StringIO()
        renderer = TTYRenderer(output, color=False)
        with patch("progress.renderer_tty.shutil.get_terminal_size", return_value=type("Size", (), {"columns": 40})()):
            renderer.render(BuildEvent("pull_status", 0, details={"stage": "transfer", "files_total": 50,
                "prepare_total": 48, "prepare_completed": 12, "prepare_active": 2}))
        value = output.getvalue().lstrip("\r")
        self.assertLessEqual(terminal_width(value), 39)
        self.assertTrue(value.endswith("..."))

    def test_plain_output_keeps_completion_and_verbose_controls_start(self):
        details = {"operation": "prepare", "layers_total": 1, "layers_completed": 1, "layer_index": 1}
        for verbose in (False, True):
            output = io.StringIO()
            renderer = PlainRenderer(output, verbose=verbose)
            renderer.render(BuildEvent("layer_start", 0, details=details))
            renderer.render(BuildEvent("layer_complete", 1, details=details))
            self.assertEqual("开始" in output.getvalue(), verbose)
            self.assertIn("已完成 1/1", output.getvalue())

    def test_timing_summary_warns_about_overlap_and_tail_wait(self):
        reporter, output = self.reporter("plain")
        reporter.record_timing("Prepare Artifactory layers (overlap)", 4)
        reporter.record_timing("Wait for Artifactory layers", 2)
        reporter.success(image="base:1")
        text = output.getvalue()
        self.assertIn("不能直接相加", text)
        self.assertIn("与下载并行的墙钟跨度", text)
        self.assertIn("剩余等待", text)

    def test_conformance_subprocess_reads_chinese_logs_with_explicit_encoding(self):
        from conformance import _run
        from errors import BuildError
        script = "import sys;print('中文构建日志');sys.stderr.write('中文失败原因');sys.exit(int(sys.argv[1]))"
        self.assertIn("中文构建日志", _run([sys.executable, "-c", script, "0"]))
        with self.assertRaisesRegex(BuildError, "中文失败原因"):
            _run([sys.executable, "-c", script, "1"])


if __name__ == "__main__":
    unittest.main()
