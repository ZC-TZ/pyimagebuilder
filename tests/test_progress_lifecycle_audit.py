"""覆盖拉取重试、终态回调、终端缩窄及工作区清理叠加失败的边界。"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
from cas_store import CASStore
from errors import ArchiveError
from progress import make_reporter
from progress.events import BuildEvent
from progress.pull import PullProgress
from progress.renderer_tty import TTYRenderer, terminal_width
from test_artifactory_publication import artifact_files, download, layer_bytes, REFERENCE, PLATFORM, SOURCE


class ProgressLifecycleAuditTests(unittest.TestCase):
    def reporter(self):
        output = io.StringIO()
        reporter = make_reporter(progress="json", stream=output)
        self.addCleanup(reporter.close)
        return reporter, output

    def test_new_pull_restarts_metadata_clock_even_in_same_outer_phase(self):
        reporter, output = self.reporter()
        reporter.phase_start("Resolving base images")
        with patch("progress.reporter.time.monotonic", return_value=100):
            PullProgress(5, reporter).update("metadata", milestone=True)
        with patch("progress.reporter.time.monotonic", return_value=120):
            PullProgress(6, reporter).update("metadata", milestone=True)
        state = json.loads(output.getvalue().splitlines()[-1])["details"]
        self.assertEqual(state["stage_seconds"], 0)
        self.assertEqual(state["files_total"], 6)

    def test_dockerfile_step_does_not_keep_previous_pull_status(self):
        reporter, _ = self.reporter()
        reporter.phase_start("Resolving base images")
        PullProgress(5, reporter).update("metadata", milestone=True)
        reporter.step_start(1, 1, "FROM scratch", 1)
        self.assertIsNone(reporter._pull_state)
        self.assertIsNone(reporter._pull_since)

    def test_step_completion_clears_pull_state_started_inside_the_step(self):
        reporter, _ = self.reporter()
        reporter.step_start(1, 1, "FROM example/base:1", 1)
        PullProgress(5, reporter).update("metadata", milestone=True)
        reporter.step_success()
        self.assertIsNone(reporter._pull_state)
        self.assertIsNone(reporter._pull_since)

    def test_late_pull_status_cannot_revive_closed_reporter_state(self):
        reporter, output = self.reporter()
        reporter.close()
        before = output.getvalue()
        PullProgress(5, reporter).update("metadata", milestone=True)
        self.assertIsNone(reporter._pull_state)
        self.assertIsNone(reporter._pull_since)
        self.assertEqual(output.getvalue(), before)

    def test_tty_resize_limits_padding_and_clearing_to_current_columns(self):
        for clear in (False, True):
            with self.subTest(clear=clear):
                stream = io.StringIO()
                renderer = TTYRenderer(stream, color=False)
                with patch("progress.renderer_tty.shutil.get_terminal_size", return_value=SimpleNamespace(columns=120)):
                    renderer.render(BuildEvent("heartbeat", 0, message="x" * 100))
                stream.seek(0)
                stream.truncate()
                with patch("progress.renderer_tty.shutil.get_terminal_size", return_value=SimpleNamespace(columns=30)):
                    if clear:
                        renderer._clear()
                    else:
                        renderer.render(BuildEvent("heartbeat", 1, message="next"))
                for segment in stream.getvalue().split("\r"):
                    self.assertLessEqual(terminal_width(segment), 29)

    def test_standalone_display_uses_terminal_columns_for_chinese_padding(self):
        stream = io.StringIO()
        display = artifacts.ProgressDisplay()
        display.is_tty = True
        with patch.object(artifacts.sys, "stdout", stream), \
                patch.object(artifacts.shutil, "get_terminal_size", return_value=SimpleNamespace(columns=120)):
            display.status("中文状态")
            stream.seek(0)
            stream.truncate()
            display.status("新")
        self.assertEqual(terminal_width(stream.getvalue().lstrip("\r")), 8)

    def test_standalone_display_does_not_wrap_after_terminal_resize(self):
        stream = io.StringIO()
        display = artifacts.ProgressDisplay()
        display.is_tty = True
        with patch.object(artifacts.sys, "stdout", stream):
            with patch.object(artifacts.shutil, "get_terminal_size", return_value=SimpleNamespace(columns=120)):
                display.status("x" * 100)
            stream.seek(0)
            stream.truncate()
            with patch.object(artifacts.shutil, "get_terminal_size", return_value=SimpleNamespace(columns=30)):
                display.status("下一个阶段")
        self.assertLessEqual(terminal_width(stream.getvalue().lstrip("\r")), 29)

    def test_original_failure_survives_workspace_cleanup_failure_and_ref_is_restored(self):
        for error in (ArchiveError("original decoding error"), KeyboardInterrupt("cancelled")):
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                files = artifact_files(layer_bytes(b"old image"))
                download(files, root / "unused.tar", store)
                previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
                factory = artifacts.tempfile.TemporaryDirectory

                class FailingWorkspace:
                    def __init__(self, *args, **kwargs):
                        self.inner = factory(*args, **kwargs)

                    def __enter__(self):
                        return self.inner.__enter__()

                    def __exit__(self, *args):
                        self.inner.__exit__(*args)
                        raise OSError("secondary workspace cleanup error")

                with patch.object(artifacts.tempfile, "TemporaryDirectory", FailingWorkspace), \
                        patch.object(artifacts, "materialize_layer", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        download(files, root / "unused.tar", store)
                self.assertIs(caught.exception, error)
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
                self.assertFalse(list(store.root.parent.glob("artifactory_download_*")))


if __name__ == "__main__":
    unittest.main()
