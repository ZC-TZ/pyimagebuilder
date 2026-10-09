import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import main
from builder import build
from errors import ArchiveError, BuildError, UnsupportedInstruction
from image_config import ImageConfig
from image_reader import ImageArchiveReader
from image_writer import ImageArchiveWriter
from rootfs import RootFSIndex
from test_phase1 import make_base


class PhaseFourTests(unittest.TestCase):
    def test_metadata_instructions_and_copy_context_without_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "payload.txt").write_bytes(b"payload")
            (context / "Dockerfile").write_text(
                "FROM example/base:1\n"
                "LABEL owner=team description=\"Ops Team\"\n"
                "EXPOSE 8080 53/UDP\n"
                "VOLUME [\"/data/cache\", \"/var/log\"]\n"
                "COPY . /app/\n", encoding="utf-8")
            output = context / "result.tar"
            build(context / "Dockerfile", context, base, None, "example/result:4", output)
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read("example/result:4")
                runtime = image.config["config"]
                self.assertEqual(runtime["Labels"], {"owner": "team", "description": "Ops Team"})
                self.assertEqual(runtime["ExposedPorts"], {"8080/tcp": {}, "53/udp": {}})
                self.assertEqual(runtime["Volumes"], {"/data/cache": {}, "/var/log": {}})
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.kind("data/cache"), "dir")
                self.assertEqual(index.kind("app/payload.txt"), "file")
                with tarfile.open(image.layers[-1]) as archive:
                    self.assertFalse(any("pyimagebuilder-" in name for name in archive.getnames()))

    def test_invalid_inputs_fail_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\nCOPY . /app/\n")
            output = context / "result.tar"
            with self.assertRaises(BuildError):
                build(dockerfile, context, base, None, ":bad", output)
            with self.assertRaises(BuildError):
                build(dockerfile, context, base, None, "example/result:4", output,
                      workspace_dir=context / "scratch")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"example/base:1": 12}))
            with self.assertRaises(BuildError):
                build(dockerfile, context, None, mapping, "example/result:4", output)
            (context / ".dockerignore").write_text("payload.txt\n")
            with self.assertRaises(UnsupportedInstruction):
                build(dockerfile, context, base, None, "example/result:4", output)
            self.assertFalse(output.exists())
        with self.assertRaises(ArchiveError):
            ImageConfig({"config": {"Env": ["malformed"]},
                         "rootfs": {"type": "layers", "diff_ids": []}, "history": []})

    def test_output_verifier_rejects_malformed_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.tar"
            with tarfile.open(path, "w") as archive:
                data = b"[]"
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            with self.assertRaises(ArchiveError):
                ImageArchiveWriter().verify(path, "example/result:4")

    def test_layer_index_rejects_invalid_whiteout_and_symlink_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            whiteout = root / "whiteout.tar"
            with tarfile.open(whiteout, "w") as archive:
                info = tarfile.TarInfo("etc/.wh.secret")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ArchiveError):
                RootFSIndex().apply_layer(whiteout)
            symlink = root / "symlink.tar"
            with tarfile.open(symlink, "w") as archive:
                info = tarfile.TarInfo("alias")
                info.type = tarfile.SYMTYPE
                info.linkname = "real"
                archive.addfile(info)
                info = tarfile.TarInfo("alias/file")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ArchiveError):
                RootFSIndex().apply_layer(symlink)

    @unittest.skipUnless(os.name == "nt", "Windows WSL map bridge")
    def test_wsl_base_map_translates_relative_windows_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "base-map.json"
            mapping.write_text(json.dumps({"example/base:1": "base.tar"}))
            args = SimpleNamespace(run=True, base_map=mapping, base_tar=None,
                                   dockerfile=root / "Dockerfile", context=root,
                                   output=root / "result.tar", tag="example/result:4",
                                   workspace=None, run_network="none", wsl_distro=None,
                                   wsl_python="python3", wsl_mount_root="/mnt",
                                   no_cache=False, cache_dir=None)
            seen = {}

            def fake_call(command):
                translated = Path(command[command.index("--base-map") + 1])
                seen.update(json.loads(translated.read_text(encoding="utf-8")))
                return 0

            with patch.object(main.platform, "system", return_value="Windows"), \
                 patch.object(main.shutil, "which", return_value="wsl.exe"), \
                 patch.object(main, "_wsl_path", side_effect=lambda path, root: str(Path(path).resolve())), \
                 patch.object(main.subprocess, "call", side_effect=fake_call):
                self.assertEqual(main._run_wsl(args), 0)
            self.assertEqual(seen["example/base:1"], str((root / "base.tar").resolve()))

    @unittest.skipUnless(os.name == "nt", "Windows WSL map bridge")
    def test_wsl_base_map_preserves_cas_source_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {"type": "cas", "store": str(root / "store"),
                      "reference": "example/base:1", "platform": "linux/amd64",
                      "source": "registry"}
            mapping = root / "base-map.json"
            mapping.write_text(json.dumps({"example/base:1": source}))
            args = SimpleNamespace(run=True, base_map=mapping, base_tar=None,
                                   dockerfile=root / "Dockerfile", context=root,
                                   output=root / "result.tar", tag="example/result:4",
                                   workspace=None, run_network="none", wsl_distro=None,
                                   wsl_python="python3", wsl_mount_root="/mnt",
                                   no_cache=False, cache_dir=None)
            seen = {}

            def fake_call(command):
                translated = Path(command[command.index("--base-map") + 1])
                seen.update(json.loads(translated.read_text(encoding="utf-8")))
                return 0

            with patch.object(main.platform, "system", return_value="Windows"), \
                 patch.object(main.shutil, "which", return_value="wsl.exe"), \
                 patch.object(main, "_wsl_path", side_effect=lambda path, root: str(Path(path).resolve())), \
                 patch.object(main.subprocess, "call", side_effect=fake_call):
                self.assertEqual(main._run_wsl(args), 0)
            self.assertEqual(seen["example/base:1"], source)


if __name__ == "__main__":
    unittest.main()
