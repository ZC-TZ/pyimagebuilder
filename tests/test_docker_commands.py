import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from build_check import check
from errors import UnsupportedInstruction
from image_cli import main as image_main
from image_store import pull_image
from main import main as build_main
from multiarch import verify as verify_multiarch


class DockerCommandsTests(unittest.TestCase):
    def run_cli(self, entry, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = entry(args)
        self.assertEqual(status, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_check_is_static_and_detects_missing_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text("FROM scratch\nCOPY payload /app/payload\n")
            (root / "payload").write_text("ok")
            result = self.run_cli(build_main, ["build", "--check", str(root)])
            self.assertEqual(result["stages"], 1)
            self.assertEqual(result["local_sources_checked"][0]["source"], "payload")
            (root / "payload").unlink()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build_main(["build", "--check", str(root)]), 1)

    def test_check_accepts_link_source_but_rejects_link_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            outside = root / "outside.txt"
            outside.write_text("payload")
            try:
                (context / "link").symlink_to(outside)
                (context / "directory-link").symlink_to(root, target_is_directory=True)
            except OSError as exc:
                self.skipTest("Creating symlinks requires OS permission: {}".format(exc))
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY link /link\n")
            self.assertEqual(check(dockerfile, context)["status"], "ok")
            build(dockerfile, context, None, None, "example/link:1", root / "link.tar")
            dockerfile.write_text("FROM scratch\nCOPY directory-link/outside.txt /copied\n")
            with self.assertRaises(UnsupportedInstruction):
                check(dockerfile, context)

    def test_inspect_reports_missing_manifest_as_archive_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "empty.tar"
            with tarfile.open(archive, "w"):
                pass
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = image_main(["inspect", str(archive)])
            self.assertEqual(status, 1)
            self.assertIn("manifest.json", stderr.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                status = image_main(["tag", str(archive), "example/repo:2",
                                     "-o", str(Path(temporary) / "retagged.tar")])
            self.assertEqual(status, 1)
            self.assertFalse((Path(temporary) / "retagged.tar").exists())

    def test_load_save_offline_build_remove_and_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_text("ok")
            dockerfile = context / "Dockerfile"
            reference = "example.test/demo/base:1"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            base = root / "base.tar"
            build(dockerfile, context, None, None, reference, base)
            store = root / "store"
            loaded = self.run_cli(image_main, ["load", "-i", str(base),
                                               "--image-store", str(store)])
            self.assertEqual(loaded["tag"], reference)
            selected, pulled = pull_image(reference, store=store, offline=True)
            self.assertFalse(pulled)
            self.assertEqual(selected, Path(loaded["archive"]))
            exported = root / "exported.tar"
            self.run_cli(image_main, ["save", reference, "-o", str(exported),
                                      "--image-store", str(store)])
            self.assertEqual(exported.read_bytes(), base.read_bytes())
            dockerfile.write_text("FROM " + reference + "\nCOPY payload /other\n")
            output = root / "derived.tar"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = build_main(["build", str(context), "--offline", "--image-store", str(store),
                                     "-t", "example.test/demo/derived:1", "-o", str(output)])
            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            usage = self.run_cli(image_main, ["system", "df", "--image-store", str(store),
                                              "--cache-dir", str(root / "cache")])
            self.assertGreater(usage["image_store"]["bytes"], 0)
            self.run_cli(image_main, ["rmi", reference, "--image-store", str(store)])
            self.assertEqual(self.run_cli(image_main, ["images", "--image-store", str(store)])
                             ["images"], [])

    def test_manifest_create_push_dispatch_and_prune(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload").write_text("x")
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            amd, arm = root / "amd.tar", root / "arm.tar"
            build(dockerfile, root, None, None, "example.test/demo/app:1", amd,
                  image_format="oci")
            build(dockerfile, root, None, None, "example.test/demo/app:1", arm,
                  image_format="oci", target_platform="linux/arm64")
            combined = root / "multi.tar"
            self.run_cli(image_main, ["manifest", "create", "--amd64", str(amd),
                                      "--arm64", str(arm), "-o", str(combined),
                                      "-t", "example.test/demo/app:1"])
            verify_multiarch(combined, "example.test/demo/app:1")
            with patch("registry.push_index", return_value="sha256:abc") as pushed:
                result = self.run_cli(image_main, ["manifest", "push", str(combined),
                                                   "example.test/demo/app:1"])
            self.assertEqual(result["digest"], "sha256:abc")
            pushed.assert_called_once()
            store = root / "store"
            archive = root / "base.tar"
            build(dockerfile, root, None, None, "example.test/demo/base:1", archive)
            loaded = self.run_cli(image_main, ["load", "-i", str(archive),
                                               "--image-store", str(store)])
            old = time.time() - 3 * 86400
            os.utime(loaded["archive"], (old, old))
            result = self.run_cli(image_main, ["image", "prune", "--older-than", "2",
                                               "--dry-run", "--image-store", str(store)])
            self.assertEqual(len(result["candidates"]), 1)
            self.assertGreater(result["reclaimable_bytes"],
                               Path(loaded["archive"]).stat().st_size)
            self.assertTrue(Path(loaded["archive"]).exists())
            self.run_cli(image_main, ["image", "prune", "--older-than", "2",
                                      "--image-store", str(store)])
            self.assertFalse(Path(loaded["archive"]).exists())

    def test_age_based_cache_prune(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_text("cache")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            cache = root / "cache"
            build(dockerfile, context, None, None, "example/app:1", root / "app.tar",
                  cache_dir=cache)
            entries = list((cache / "entries").glob("*.json"))
            self.assertTrue(entries)
            old = time.time() - 3 * 86400
            for entry in entries:
                os.utime(entry, (old, old))
            result = self.run_cli(image_main, ["cache", "prune", "--older-than", "2",
                                               "--dry-run", "--cache-dir", str(cache)])
            self.assertEqual(len(result["expired_entries"]), len(entries))
            self.assertTrue(all(entry.exists() for entry in entries))
            self.run_cli(image_main, ["cache", "prune", "--older-than", "2",
                                      "--cache-dir", str(cache)])
            self.assertFalse(any(entry.exists() for entry in entries))
            self.assertEqual(list((cache / "layers").glob("*.tar")), [])
