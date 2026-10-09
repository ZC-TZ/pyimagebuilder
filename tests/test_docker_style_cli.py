"""验证 Docker 风格参数，同时保留原有 CLI 调用方式。"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class DockerStyleCLITests(unittest.TestCase):
    def test_build_subcommand_positional_context_and_short_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n",
                                                 encoding="utf-8")
            (context / "payload").write_bytes(b"hello")
            output = root / "image.tar"
            with contextlib.redirect_stdout(io.StringIO()):
                result = main.main(["build", "-t", "example/short:1", "-o", str(output),
                                    str(context)])
            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())

    def test_explicit_file_and_legacy_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = root / "Other.Dockerfile"
            dockerfile.write_text("FROM scratch\nENV MODE=test\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["build", "-f", str(dockerfile), "-t", "x/y:1",
                                            "-o", str(root / "new.tar"), str(context)]), 0)
                self.assertEqual(main.main(["--dockerfile", str(dockerfile),
                                            "--context", str(context), "--tag", "x/y:1",
                                            "--output", str(root / "legacy.tar")]), 0)
            self.assertEqual((root / "new.tar").read_bytes(), (root / "legacy.tar").read_bytes())

    def test_duplicate_context_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with contextlib.redirect_stderr(io.StringIO()):
                result = main.main(["build", "-t", "x/y:1", "-o", str(root / "x.tar"),
                                    "--context", str(root), str(root)])
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
