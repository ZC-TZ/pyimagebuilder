"""验证 ARG 展开与 RUN 临时挂载的回归行为。"""

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import builder
from dockerfile_parser import parse
from errors import BuildError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from run_mounts import prepare, secrets


class PhaseThirteenTests(unittest.TestCase):
    def test_global_and_stage_arg_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context"
            context.mkdir()
            (context / "payload.txt").write_text("data", encoding="utf-8")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text(
                "ARG BASE=scratch\nFROM ${BASE}\nARG DEST=/app\n"
                "COPY payload.txt ${DEST}/payload.txt\n"
                "ENV TARGET=${DEST}\n", encoding="utf-8")
            output = root / "result.tar"
            builder.build(dockerfile, context, None, None, "example/args:1", output,
                          build_args={"DEST": "/custom"})
            with tempfile.TemporaryDirectory() as extracted:
                image = ImageArchiveReader(output, Path(extracted)).read("example/args:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.kind("custom/payload.txt"), "file")
                self.assertIn("TARGET=/custom", image.config["config"]["Env"])
                self.assertNotIn("DEST=/custom", image.config["config"]["Env"])

    def test_mount_parser_and_validation(self):
        run = parse("FROM scratch\nRUN --mount=type=cache,target=/root/.cache,id=pip "
                    "--mount=type=secret,id=token cat /run/secrets/token\n")[1].value
        self.assertEqual(len(run.mounts), 2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = root / "secret.txt"
            secret.write_text("private", encoding="utf-8")
            result = prepare(run.mounts, secrets(["token=" + str(secret)]),
                             root / "cache", root, "linux/amd64")
            self.assertEqual([item[0] for item in result], ["cache", "secret"])
            self.assertEqual(result[1][2], "/run/secrets/token")
            with self.assertRaises(BuildError):
                prepare(run.mounts, {}, root / "cache", root, "linux/amd64")
        with self.assertRaises(UnsupportedInstruction):
            parse("FROM scratch\nRUN --mount=type=ssh,id=default true\n")

    def test_secret_run_is_executed_each_time_and_not_in_layer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nARG MODE=test\n"
                                  "RUN --mount=type=secret,id=token cat /run/secrets/token\n",
                                  encoding="utf-8")
            secret = root / "token.txt"
            secret.write_text("first secret", encoding="utf-8")
            calls = []

            class FakeMaterializer:
                def __init__(self, path):
                    self.root = path
                    self.root.mkdir()

                def apply(self, path):
                    pass

            class FakeOverlay:
                def __init__(self, rootfs, workspace):
                    self.excluded_paths = set()

                def to_layer(self, target):
                    with tarfile.open(target, "w") as archive:
                        data = b"public"
                        info = tarfile.TarInfo("result.txt")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                    return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

            class FakeExecutor:
                def __init__(self, network, sandbox="legacy"):
                    pass

                def execute(self, overlay, command, env, workdir, user, shell, mounts=()):
                    self_outer.assertEqual(env["MODE"], "test")
                    self_outer.assertEqual(overlay.excluded_paths, {"run/secrets/token"})
                    self_outer.assertEqual(mounts[0][1], secret.resolve())
                    calls.append(1)

            self_outer = self
            with patch.object(builder.platform, "system", return_value="Linux"), \
                 patch.object(builder, "RootFSMaterializer", FakeMaterializer), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FakeExecutor):
                for number in (1, 2):
                    builder.build(dockerfile, context, None, None,
                                  "example/secret:1", root / ("out{}.tar".format(number)),
                                  enable_run=True, cache_dir=root / "cache",
                                  secret_sources={"token": secret})
            self.assertEqual(len(calls), 2)
            self.assertNotIn(b"first secret", (root / "out1.tar").read_bytes())


if __name__ == "__main__":
    unittest.main()
