"""验证层任务状态、并发计数，以及真实下载/CAS/构建事件的统一契约。"""

import gzip
import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
import registry
from builder import build
from cas_store import CASStore
from image_reader import ImageArchiveReader
from progress import LayerProgress, make_reporter
from progress.events import BuildEvent
from progress.renderer_tty import TTYRenderer, terminal_width
from test_artifactory_publication import digest, layer_bytes
from test_phase1 import make_base
from test_registry_publication import local_registry


def fixture(count, invalid=False):
    files, descriptors, diffs = {}, [], []
    for number in range(count):
        layer = layer_bytes(str(number).encode())
        blob = gzip.compress(layer)
        files["sha256__" + digest(blob)[7:]] = blob
        descriptors.append({"digest": digest(blob), "size": len(blob),
                            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"})
        diffs.append(digest(layer))
    if invalid:
        diffs[-1] = "sha256:" + "0" * 64
    config = json.dumps({"os": "linux", "architecture": "amd64", "config": {},
                         "rootfs": {"type": "layers", "diff_ids": diffs}}).encode()
    files["sha256__" + digest(config)[7:]] = config
    files["manifest.json"] = json.dumps({"schemaVersion": 2, "layers": descriptors,
                                         "config": {"digest": digest(config), "size": len(config)}}).encode()
    return files


class LayerProgressTests(unittest.TestCase):
    def reporter(self, mode="json"):
        stream = io.StringIO()
        reporter = make_reporter(progress=mode, stream=stream)
        self.addCleanup(reporter.close)
        return reporter, stream

    def events(self, stream):
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_parallel_completion_counts_are_not_manifest_indices(self):
        reporter, stream = self.reporter()
        group = LayerProgress("download", 3, reporter)
        arrived = threading.Barrier(4)
        gates = {i: threading.Event() for i in (1, 2, 3)}

        def worker(index):
            with group.layer(index):
                arrived.wait(5)
                if not gates[index].wait(5):
                    raise AssertionError("worker not released")

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {i: pool.submit(worker, i) for i in gates}
            try:
                arrived.wait(5)
                for index in (3, 2, 1):
                    gates[index].set()
                    futures[index].result(timeout=5)
            finally:
                for gate in gates.values():
                    gate.set()
        group.finish()
        events = self.events(stream)
        completed = [e["details"] for e in events if e["type"] == "layer_complete"]
        self.assertEqual([e["layer_index"] for e in completed], [3, 2, 1])
        self.assertEqual([e["layers_completed"] for e in completed], [1, 2, 3])
        self.assertEqual(len({e["details"]["group_id"] for e in events}), 1)

    def test_failed_renderer_cannot_replace_original_layer_failure(self):
        reporter, stream = self.reporter()
        render = reporter.renderer.render

        def broken(event):
            if event.type == "layer_failed":
                raise OSError("output unavailable")
            render(event)

        group = LayerProgress("prepare", 1, reporter)
        failure = ValueError("original failure")
        with patch.object(reporter.renderer, "render", side_effect=broken):
            with self.assertRaises(ValueError) as raised:
                with group.layer(1):
                    raise failure
        self.assertIs(raised.exception, failure)
        with self.assertRaises(RuntimeError):
            group.finish()

    def test_incomplete_duplicate_and_zero_layer_groups(self):
        reporter, stream = self.reporter()
        group = LayerProgress("verify", 1, reporter)
        with self.assertRaises(RuntimeError):
            group.finish()
        with group.layer(1):
            pass
        with self.assertRaises(ValueError):
            with group.layer(1):
                pass
        group.finish()
        with self.assertRaises(RuntimeError):
            group.finish()
        LayerProgress("verify", 0, reporter).finish()
        self.assertEqual(self.events(stream)[-1]["details"]["layers_completed"], 0)

    def test_tty_clears_chinese_columns_and_combining_characters(self):
        stream = io.StringIO()
        renderer = TTYRenderer(stream, color=False)
        event = BuildEvent("layer_progress", 0, current=1234, details={"operation": "prepare",
            "layer_index": 1, "layers_total": 21, "layers_completed": 0})
        renderer.render(event)
        width = renderer.width
        self.assertGreater(width, len(stream.getvalue().lstrip("\r")))
        renderer.render(BuildEvent("step_log", 0, message="done"))
        self.assertIn("\r" + " " * width + "\r", stream.getvalue())
        self.assertEqual(terminal_width("中e\u0301"), 3)

    def test_fast_small_artifactory_layers_and_faults_are_visible(self):
        for mode in ("plain", "json"):
            for invalid in (False, True):
                with self.subTest(mode=mode, invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    files = fixture(21, invalid)
                    store = CASStore(root / "store")
                    reporter, stream = self.reporter(mode)

                    def transfer(_opener, _url, name, directory, _size, _limiter, stats, _display):
                        (directory / name).write_bytes(files[name])
                        stats.add(len(files[name]))

                    with patch.object(artifacts, "probe_file_sizes", return_value={n: len(b) for n, b in files.items()}), \
                            patch.object(artifacts, "download_one", side_effect=transfer):
                        args = (object(), "https://example.test/", list(files), root / "base.tar", 2,
                                "example.test/repo/base:1")
                        if invalid:
                            with self.assertRaisesRegex(RuntimeError, "diff_id"):
                                artifacts.download_and_pack(*args, cas_store=store, cas_only=True, reporter=reporter)
                        else:
                            artifacts.download_and_pack(*args, cas_store=store, cas_only=True, reporter=reporter)
                    if mode == "json":
                        events = self.events(stream)
                        completed = [e for e in events if e["type"] == "layer_complete"]
                        self.assertEqual(len(completed), 20 if invalid else 21)
                        self.assertEqual([e["details"]["layer_index"] for e in completed], list(range(1, len(completed)+1)))
                        self.assertEqual(sum(e["type"] == "layers_complete" for e in events), 0 if invalid else 1)
                        self.assertEqual(sum(e["type"] == "layer_failed" for e in events), 1 if invalid else 0)
                    else:
                        self.assertIn("准备镜像层 20/21 | 完成", stream.getvalue())
                        self.assertEqual("准备镜像层完成 | 21/21 层" in stream.getvalue(), not invalid)

    def test_registry_download_prepare_and_store_use_same_events(self):
        with tempfile.TemporaryDirectory() as temporary, local_registry():
            root = Path(temporary)
            reporter, stream = self.reporter()
            registry.pull("example.test/p/app:1", root / "unused.tar", reporter=reporter,
                          cas_store=CASStore(root / "store"), cas_only=True)
            groups = [e["details"]["operation"] for e in self.events(stream) if e["type"] == "layers_complete"]
            self.assertEqual(groups, ["download", "prepare", "store"])

    def test_local_tar_and_warm_cas_reads_have_explicit_cache_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_base(root)
            store = CASStore(root / "store")
            store.import_docker(source, "example/base:1", "linux/amd64")
            reporter, stream = self.reporter()
            ImageArchiveReader(source, root / "read", root / "base-cache").read("example/base:1", reporter=reporter)
            ImageArchiveReader(source, root / "read2", root / "base-cache").read("example/base:1", reporter=reporter)
            store.open_base("example/base:1", reporter=reporter)
            done = [e for e in self.events(stream) if e["type"] == "layer_complete"]
            self.assertEqual(len(done), 6)
            self.assertEqual([e["details"]["cached"] for e in done], [False, False, True, True, True, True])

    def test_build_distinguishes_reading_and_rootfs_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_base(root)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\n", encoding="utf-8")
            reporter, stream = self.reporter()
            build(dockerfile, root, source, None, "example/app:1", root / "out.tar", reporter=reporter)
            stages = [e["details"]["operation"] for e in self.events(stream) if e["type"] == "layers_complete"]
            self.assertEqual(stages, ["read", "index"])


if __name__ == "__main__":
    unittest.main()
