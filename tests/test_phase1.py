import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from errors import UnsupportedInstruction
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex


def layer_file(path, contents):
    with tarfile.open(path, "w") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def add_bytes(archive, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def make_base(root):
    first = root / "first.tar"
    second = root / "second.tar"
    layer_file(first, {"etc/old.conf": b"old", "app/existing.txt": b"base"})
    layer_file(second, {"etc/.wh.old.conf": b"", "etc/new.conf": b"new"})
    diffs = ["sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)]
    config = {
        "architecture": "amd64", "os": "linux", "config": {"Env": ["BASE=1"]},
        "rootfs": {"type": "layers", "diff_ids": diffs},
        "history": [{"created_by": "first"}, {"created_by": "second"}],
    }
    raw = json.dumps(config).encode()
    config_name = hashlib.sha256(raw).hexdigest() + ".json"
    manifest = [{"Config": config_name, "RepoTags": ["example/base:1"],
                 "Layers": ["one/layer.tar", "two/layer.tar"]}]
    archive_path = root / "base-image.tar"
    with tarfile.open(archive_path, "w") as archive:
        add_bytes(archive, "manifest.json", json.dumps(manifest).encode())
        add_bytes(archive, config_name, raw)
        archive.add(first, arcname="one/layer.tar")
        archive.add(second, arcname="two/layer.tar")
    return archive_path


class PhaseOneTests(unittest.TestCase):
    def test_build_copy_config_and_whiteout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "app.war").write_bytes(b"WAR data")
            (context / "Dockerfile").write_text("""FROM example/base:1
ENV APP_MODE=prod
WORKDIR /app
COPY app.war /app/
CMD ["java", "-jar", "/app/app.war"]
""")
            output = root / "output-image.tar"
            build(context / "Dockerfile", context, base, None, "example/result:1", output)
            self.assertTrue(output.is_file())
            with tempfile.TemporaryDirectory() as extracted:
                image = ImageArchiveReader(output, Path(extracted)).read("example/result:1")
                self.assertEqual(len(image.layers), 3)
                self.assertIn("APP_MODE=prod", image.config["config"]["Env"])
                self.assertEqual(image.config["config"]["Cmd"], ["java", "-jar", "/app/app.war"])
                self.assertEqual(len(image.config["rootfs"]["diff_ids"]), 3)
                index = RootFSIndex()
                for path in image.layers:
                    index.apply_layer(path)
                self.assertIsNone(index.kind("etc/old.conf"))
                self.assertEqual(index.kind("etc/new.conf"), "file")
                self.assertEqual(index.kind("app/app.war"), "file")
                with tarfile.open(image.layers[-1]) as new_layer:
                    self.assertEqual(new_layer.extractfile("app/app.war").read(), b"WAR data")

    def test_workdir_directory_copy_and_base_map(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_base(root)
            mapping = root / "base-map.json"
            mapping.write_text(json.dumps({"example/base:1": base.name}))
            context = root / "context"
            assets = context / "assets" / "nested"
            assets.mkdir(parents=True)
            (assets / "index.html").write_text("ok")
            (context / "Dockerfile").write_text("""FROM example/base:1
WORKDIR /opt/new
COPY assets/ ./
ENTRYPOINT ["/bin/true"]
""")
            output = root / "result.tar"
            build(context / "Dockerfile", context, None, mapping, "example/new:1", output)
            with tempfile.TemporaryDirectory() as extracted:
                image = ImageArchiveReader(output, Path(extracted)).read("example/new:1")
                self.assertEqual(len(image.layers), 4)
                self.assertEqual(image.config["config"]["WorkingDir"], "/opt/new")
                self.assertEqual(image.config["config"]["Entrypoint"], ["/bin/true"])
                index = RootFSIndex()
                for path in image.layers:
                    index.apply_layer(path)
                self.assertEqual(index.kind("opt/new/nested/index.html"), "file")

    def test_run_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\nRUN false\n")
            output = root / "output-image.tar"
            with self.assertRaises(UnsupportedInstruction):
                build(context / "Dockerfile", context, root / "missing.tar", None,
                      "example/result:1", output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
