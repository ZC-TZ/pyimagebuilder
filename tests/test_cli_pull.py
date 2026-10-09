"""无需外部 Registry，验证统一 pull/build 命令及缓存回滚。"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_store
import main
from cas_store import CASStore
from errors import BuildError
from test_phase1 import make_base


class PullCLITests(unittest.TestCase):
    def test_pull_then_offline_build_from_local_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            store = root / "store"
            export = root / "pulled.tar"
            calls = []

            def fake_pull(reference, output, *_args):
                calls.append(reference)
                shutil.copyfile(base, output)

            with patch.object(image_store, "registry_pull", side_effect=fake_pull), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["pull", "example/base:1", "--image-store",
                                            str(store), "-o", str(export)]), 0)
            self.assertTrue(export.is_file())
            self.assertEqual(calls, ["example/base:1"])
            cached_tar = image_store._archive_path(store, "example/base:1",
                                                   "linux/amd64", "registry")
            cached_tar.unlink()
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\nCOPY app.war /app/app.war\n")
            (context / "app.war").write_bytes(b"app")
            output = root / "image.tar"
            with patch.object(image_store, "registry_pull", side_effect=AssertionError("network used")), \
                 patch("builder.ImageArchiveReader.read", side_effect=AssertionError("tar reader used")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["build", "-t", "example/app:1", "-o", str(output),
                                            "--image-store", str(store), "--offline",
                                            "--attest",
                                            str(context)]), 0)
            self.assertTrue(output.is_file())
            self.assertFalse(cached_tar.exists())
            provenance = json.loads(output.with_name(output.name + ".provenance.json").read_text())
            dependencies = provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
            self.assertTrue(any(item["uri"] == "base:example/base:1@linux/amd64" and
                                len(item["digest"]["sha256"]) == 64 for item in dependencies))

    def test_missing_offline_base_fails_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\n")
            with patch.object(image_store, "registry_pull", side_effect=AssertionError("network used")), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main.main(["build", "-t", "example/app:1", "-o",
                                            str(root / "image.tar"), "--offline", "--image-store",
                                            str(root / "store"), str(context)]), 1)

    def test_build_pulls_missing_and_pull_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\n")
            calls = []

            def fake_pull(reference, output, *_args):
                calls.append(reference)
                shutil.copyfile(base, output)

            common = ["build", "-t", "example/app:1", "--image-store", str(root / "store")]
            with patch.object(image_store, "registry_pull", side_effect=fake_pull), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(common + ["-o", str(root / "built-one.tar"), str(context)]), 0)
                self.assertEqual(main.main(common + ["-o", str(root / "built-two.tar"), str(context)]), 0)
                self.assertEqual(main.main(common + ["--pull", "-o", str(root / "built-three.tar"),
                                                    str(context)]), 0)
            self.assertEqual(calls, ["example/base:1", "example/base:1"])

    def test_from_discovery_respects_global_arg_stage_and_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            dockerfile = Path(temporary) / "Dockerfile"
            dockerfile.write_text("ARG BASE=example/base:1\nFROM ${BASE} AS build\n"
                                  "FROM build AS package\nFROM other/base:2 AS later\n")
            self.assertEqual(image_store.external_bases(dockerfile, "linux/amd64", "package", {}),
                             {"example/base:1": {"linux/amd64"}})

    def test_pull_artifactory_source_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)

            def fake_download(_opener, _url, _files, output, _workers, _reference):
                shutil.copyfile(base, output)

            with patch.object(image_store, "make_opener", return_value=object()), \
                 patch.object(image_store, "list_files", return_value=["manifest.json"]), \
                 patch.object(image_store, "download_and_pack", side_effect=fake_download), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["pull", "example/base:1", "--source", "artifactory",
                                            "--image-store", str(root / "store")]), 0)

    def test_failed_refresh_restores_previous_cas_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = "example/base:1"
            store_root = root / "store"
            first = make_base(root)
            cas = CASStore(store_root)
            original = cas.import_docker(first, reference, "linux/amd64", "registry")

            def interrupted_pull(_reference, _output, *_args, cas_store=None,
                                 cas_only=False):
                self.assertTrue(cas_only)
                cas_store.remove_ref(reference, "linux/amd64", "registry")
                raise BuildError("download interrupted")

            with patch.object(image_store, "registry_pull", new=interrupted_pull):
                with self.assertRaises(BuildError):
                    image_store.pull_image(reference, store=store_root, refresh=True,
                                           materialize_tar=False)
            restored = cas.resolve(reference, source="registry")
            self.assertEqual(restored["manifest"], original["manifest"])


if __name__ == "__main__":
    unittest.main()
