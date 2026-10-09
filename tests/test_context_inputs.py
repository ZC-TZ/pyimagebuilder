"""验证 Dockerfile 字面量变量和目录链接不会改变实际构建输入边界。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_check import check
from builder import build
from errors import BuildError
from hermetic import _copy_context
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex


def directory_link(target, destination):
    """Windows 使用无管理员权限的 junction；其他平台创建目录符号链接。"""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(destination))
    else:
        os.symlink(str(target), str(destination), target_is_directory=True)


class ContextInputTests(unittest.TestCase):
    def read_image(self, output, root):
        image = ImageArchiveReader(output, root / (output.stem + "-layers")).read("example/app:1")
        index = RootFSIndex()
        for path in image.layers:
            index.apply_layer(path)
        return image, index

    def test_literal_dollars_in_copy_env_label_and_arg(self):
        for expression in (r"\$TOKEN", "'$TOKEN'", '"\\$TOKEN"'):
            for link in ("", "--link "):
                with self.subTest(expression=expression, link=link), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    context = root / "context"
                    context.mkdir()
                    (context / "$TOKEN").write_bytes(b"literal")
                    (context / "expanded").write_bytes(b"wrong input")
                    (context / "payload").write_bytes(b"WAR")
                    dockerfile = context / "Dockerfile"
                    dockerfile.write_text("FROM scratch\nARG TOKEN=expanded\nENV TEXT=" + expression +
                                          "\nLABEL literal=" + expression + "\nARG FALLBACK=" + expression +
                                          "\nENV FALLBACK_VALUE=$FALLBACK\nCOPY " + link + expression +
                                          " /artifact\nCOPY payload /${TEXT}.war\n")
                    for number in range(2):
                        stats = {}
                        output = root / ("literal-{}.tar".format(number))
                        build(dockerfile, context, None, None, "example/app:1", output,
                              cache_dir=root / "cache", cache_stats=stats)
                        image, index = self.read_image(output, root)
                        self.assertIn("TEXT=$TOKEN", image.config["config"]["Env"])
                        self.assertIn("FALLBACK_VALUE=$TOKEN", image.config["config"]["Env"])
                        self.assertEqual(image.config["config"]["Labels"]["literal"], "$TOKEN")
                        self.assertEqual(index.read_file("artifact"), b"literal")
                        self.assertEqual(index.read_file("$TOKEN.war"), b"WAR")
                        self.assertIsNone(index.kind("expanded.war"))
                        self.assertEqual(stats["hits"], number * 2)

    def test_json_copy_preserves_literal_escape_and_normal_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "$TOKEN").write_bytes(b"literal")
            (context / "expanded").write_bytes(b"expanded")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nARG TOKEN=expanded\nCOPY " +
                                  json.dumps([r"\$TOKEN", "/literal"]) + "\nCOPY " +
                                  json.dumps(["$TOKEN", "/expanded"]) + "\n")
            output = root / "json.tar"
            build(dockerfile, context, None, None, "example/app:1", output)
            _, index = self.read_image(output, root)
            self.assertEqual(index.read_file("literal"), b"literal")
            self.assertEqual(index.read_file("expanded"), b"expanded")

    def test_global_arg_literal_default_and_env_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("ARG TOKEN=expanded LITERAL='${TOKEN}'\nFROM scratch\n"
                                  "ARG LITERAL\nENV TOKEN=old\nENV TOKEN=new SNAPSHOT=$TOKEN\n"
                                  "ENV LITERAL=$LITERAL\n")
            output = root / "global.tar"
            build(dockerfile, context, None, None, "example/app:1", output)
            image, _ = self.read_image(output, root)
            environment = image.config["config"]["Env"]
            self.assertIn("LITERAL=${TOKEN}", environment)
            self.assertIn("TOKEN=new", environment)
            self.assertIn("SNAPSHOT=old", environment)

    def test_legacy_expanded_dollar_cache_is_not_reused(self):
        """模拟旧版解析器和缓存版本，验证升级后不会继续输出选错源的层。"""
        import dockerfile_parser
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "$TOKEN").write_bytes(b"literal")
            (context / "expanded").write_bytes(b"wrong input")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nARG TOKEN=expanded\nENV TEXT=\\$TOKEN\n"
                                  "COPY \\$TOKEN /artifact\nCOPY expanded /${TEXT}.war\n")
            legacy = root / "legacy.tar"
            with patch.object(dockerfile_parser, "_expanded_words", dockerfile_parser._words), \
                    patch("builder.CACHE_VERSION", 5), patch("cache.CACHE_VERSION", 5):
                build(dockerfile, context, None, None, "example/app:1", legacy,
                      cache_dir=root / "cache")
            _, index = self.read_image(legacy, root)
            self.assertEqual(index.read_file("artifact"), b"wrong input")
            for number in range(2):
                stats = {}
                output = root / ("fixed-{}.tar".format(number))
                build(dockerfile, context, None, None, "example/app:1", output,
                      cache_dir=root / "cache", cache_stats=stats)
                _, index = self.read_image(output, root)
                self.assertEqual(index.read_file("artifact"), b"literal")
                self.assertEqual(index.read_file("$TOKEN.war"), b"wrong input")
                self.assertIsNone(index.kind("expanded.war"))
                self.assertEqual(stats["hits"], number * 2)

    def junction_context(self, root):
        context = root / "context"
        outside = root / "outside"
        context.mkdir()
        outside.mkdir()
        (context / "payload").write_bytes(b"allowed")
        (outside / "secret.war").write_bytes(b"outside context")
        directory_link(outside, context / "junction")
        return context

    @unittest.skipUnless(sys.platform == "win32", "Windows junction 边界")
    def test_context_directory_link_cannot_supply_copy_data(self):
        for expression in ("junction", "junction/secret.war", ".", "junction/**/*.war", "**/*.war"):
            for caching in (False, True):
                with self.subTest(source=expression, caching=caching), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    context = self.junction_context(root)
                    dockerfile = context / "Dockerfile"
                    dockerfile.write_text("FROM scratch\nCOPY " + expression + " /app/\n")
                    output = root / "bad.tar"
                    with self.assertRaises(BuildError):
                        build(dockerfile, context, None, None, "example/app:1", output,
                              cache_dir=root / "cache" if caching else None)
                    self.assertFalse(output.exists())
                    self.assertEqual((root / "outside" / "secret.war").read_bytes(), b"outside context")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction 边界")
    def test_static_check_rejects_traversed_directory_links(self):
        for expression in ("junction", "junction/secret.war", "**/*.war"):
            with self.subTest(source=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = self.junction_context(root)
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nCOPY " + expression + " /app/\n")
                with self.assertRaises(BuildError):
                    check(dockerfile, context)

    def test_hermetic_lock_rejects_junction_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.junction_context(root)
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY . /app/\n")
            # 在真实的固定 HASHSEED 子进程中满足前置检查，避免因无关错误假通过。
            environment = dict(os.environ, PYTHONHASHSEED="0")
            command = ("import sys; from hermetic import make_lock; "
                       "make_lock(sys.argv[1], sys.argv[2], tag='example/app:1')")
            result = subprocess.run([sys.executable, "-c", command, str(dockerfile), str(context)],
                                    cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, "context (symlinks|directory links)")
            with self.assertRaisesRegex(BuildError, "(symlink|directory link)"):
                _copy_context(context, root / "snapshot")

    def test_excluded_junction_does_not_block_unrelated_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.junction_context(root)
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY --exclude=junction . /app/\n")
            output = root / "excluded.tar"
            build(dockerfile, context, None, None, "example/app:1", output, cache_dir=root / "cache")
            _, index = self.read_image(output, root)
            self.assertEqual(index.read_file("app/payload"), b"allowed")
            self.assertIsNone(index.kind("app/junction"))

    def test_recursive_glob_keeps_zero_depth_and_nested_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            (context / "nested" / "deep").mkdir(parents=True)
            (context / "$hidden").write_bytes(b"unrelated")
            (context / "root.war").write_bytes(b"root")
            (context / "nested" / "deep" / "nested.war").write_bytes(b"nested")
            (context / "Dockerfile").write_text("FROM scratch\nCOPY **/*.war /app/\n")
            output = root / "glob.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", output)
            _, index = self.read_image(output, root)
            self.assertEqual(index.read_file("app/root.war"), b"root")
            self.assertEqual(index.read_file("app/nested.war"), b"nested")

    def test_glob_does_not_interpret_context_directory_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context[1]"
            context.mkdir()
            (context / "app.war").write_bytes(b"WAR")
            (context / "Dockerfile").write_text("FROM scratch\nCOPY *.war /app/\n")
            output = root / "glob.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", output)
            _, index = self.read_image(output, root)
            self.assertEqual(index.read_file("app/app.war"), b"WAR")
            self.assertEqual(check(context / "Dockerfile", context)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
