"""验证 ARG 作用域、路径解码、停止信号及外部 ONBUILD 的构建语义。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cache import LayerCache, cache_key
from dockerfile_parser import parse
from errors import BuildError
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from image_store import external_bases
from layer import LayerBuilder
from rootfs import RootFSIndex
from test_phase1 import layer_file


class BuildSemanticsTests(unittest.TestCase):
    def read_image(self, output, root):
        image = ImageArchiveReader(output, root / (output.stem + "-layers")).read("example/app:1")
        index = RootFSIndex()
        for path in image.layers:
            index.apply_layer(path)
        return image, index

    def test_stop_signal_expands_case_sensitive_names_before_normalizing(self):
        for expression in ("$signal", "${signal}", "${signal:-SIGTERM}", '"$signal"'):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nARG signal=SIGQUIT SIGNAL=SIGTERM\n"
                                      "STOPSIGNAL " + expression + "\n")
                output = root / "signal.tar"
                build(dockerfile, context, None, None, "example/app:1", output)
                image, _ = self.read_image(output, root)
                self.assertEqual(image.config["config"]["StopSignal"], "SIGQUIT")

    def test_workdir_quotes_and_escaped_spaces_use_one_decoded_path(self):
        for expression in ('"/app space"', "'/app space'", r"/app\ space"):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "payload.war").write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nWORKDIR " + expression +
                                      "\nCOPY payload.war ./app.war\n")
                for number in range(2):
                    output = root / ("workdir-{}.tar".format(number))
                    stats = {}
                    build(dockerfile, context, None, None, "example/app:1", output,
                          cache_dir=root / "cache", cache_stats=stats)
                    image, index = self.read_image(output, root)
                    self.assertEqual(image.config["config"]["WorkingDir"], "/app space")
                    self.assertEqual(index.read_file("app space/app.war"), b"WAR")
                    self.assertEqual(stats["hits"], 2 * number)

    def test_multiple_arg_defaults_see_preceding_declarations(self):
        for arguments, expected in (({}, "local"), ({"DEST": "cli"}, "cli")):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "payload.war").write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("ARG ROOT=scratch BASE=$ROOT\nFROM $BASE AS parent\n"
                                      "ARG DEST=local NEXT=$DEST FINAL=${NEXT}\n"
                                      "FROM parent\nENV RESULT=$FINAL\n"
                                      "COPY payload.war /${FINAL}/app.war\n")
                self.assertEqual(external_bases(dockerfile, "linux/amd64", None, arguments), {})
                for number in range(2):
                    output = root / ("args-{}.tar".format(number))
                    stats = {}
                    build(dockerfile, context, None, None, "example/app:1", output,
                          build_args=arguments, cache_dir=root / "cache", cache_stats=stats)
                    image, index = self.read_image(output, root)
                    self.assertIn("RESULT=" + expected, image.config["config"]["Env"])
                    self.assertEqual(index.read_file(expected + "/app.war"), b"WAR")
                    self.assertEqual(stats["hits"], number)

    def test_workdir_literal_dollars_survive_syntax_decoding(self):
        for expression in ("'/app/$folder'", r"/app/\$folder", '"/app/\\$folder"'):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "payload.war").write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nARG folder=wrong\nWORKDIR " + expression +
                                      "\nCOPY payload.war ./app.war\n")
                output = root / "literal.tar"
                build(dockerfile, context, None, None, "example/app:1", output)
                image, index = self.read_image(output, root)
                self.assertEqual(image.config["config"]["WorkingDir"], "/app/$folder")
                self.assertEqual(index.read_file("app/$folder/app.war"), b"WAR")

    def test_literal_stop_signal_variable_is_rejected(self):
        for expression in ("'$signal'", r"\$signal", '"\\$signal"', "$signal SIGTERM"):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nARG signal=SIGQUIT\nSTOPSIGNAL " + expression + "\n")
                output = root / "invalid.tar"
                with self.assertRaises(BuildError):
                    build(dockerfile, context, None, None, "example/app:1", output)
                self.assertFalse(output.exists())

    def test_legacy_quoted_workdir_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            recipe = 'FROM scratch\nWORKDIR "/app space"\n'
            dockerfile = context / "Dockerfile"
            dockerfile.write_text(recipe)
            instruction = parse(recipe)[1]
            legacy_key = cache_key("instruction", 3, cache_key("scratch-base", "linux/amd64", 0),
                                   instruction.raw, None)
            legacy_layer = root / "legacy.tar"
            diff_id = LayerBuilder(context, RootFSIndex()).workdir_layer('/"/app space"', legacy_layer)
            cache = LayerCache(root / "cache")
            cache.store(legacy_key, diff_id, legacy_layer)
            entry_path = cache.entries / (legacy_key + ".json")
            entry = json.loads(entry_path.read_text())
            entry["version"] = 3
            entry_path.write_text(json.dumps(entry))
            output = root / "image.tar"
            stats = {}
            build(dockerfile, context, None, None, "example/app:1", output,
                  cache_dir=root / "cache", cache_stats=stats)
            image, index = self.read_image(output, root)
            self.assertEqual(image.config["config"]["WorkingDir"], "/app space")
            self.assertEqual(index.kind("app space"), "dir")
            self.assertIsNone(index.kind('"'))
            self.assertEqual((stats["hits"], stats["misses"]), (0, 1))

    def test_arg_redeclaration_and_inheritance_with_layer_cache(self):
        scenarios = (
            ("FROM scratch\nARG DEST=local\nARG DEST\n", {}, "local"),
            ("FROM scratch AS parent\nARG DEST=parent\nFROM parent\nARG DEST\n", {}, "parent"),
            ("ARG DEST=global\nFROM scratch\nARG DEST=local\nARG DEST\n", {}, "global"),
            ("FROM scratch AS parent\nARG DEST=parent\nFROM parent\nARG DEST=child\n", {}, "child"),
            ("FROM scratch AS parent\nARG DEST=parent\nFROM parent\nARG DEST\n", {"DEST": "cli"}, "cli"),
            ("FROM scratch AS parent\nARG DEST=parent\nFROM scratch\nARG DEST\n", {}, ""),
        )
        for recipe, arguments, expected in scenarios:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "payload.war").write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text(recipe + "ENV SEEN=$DEST\nCOPY payload.war /${DEST}/app.war\n")
                for number in range(2):
                    stats = {}
                    output = root / ("image-{}.tar".format(number))
                    build(dockerfile, context, None, None, "example/app:1", output,
                          build_args=arguments, cache_dir=root / "cache", cache_stats=stats)
                    image, index = self.read_image(output, root)
                    self.assertIn("SEEN=" + expected, image.config["config"]["Env"])
                    self.assertEqual(index.read_file(expected + "/app.war"), b"WAR")
                    self.assertEqual(stats["hits"], number)

    def test_copy_parents_pivot_preserves_hidden_names_correctly(self):
        for expression in (".hidden/./nested/app.war", "./.hidden/./nested/app.war",
                           "...meta/./nested/app.war", "normal/./nested/app.war"):
            with self.subTest(source=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                source = context / expression
                source.parent.mkdir(parents=True)
                source.write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nCOPY --parents " + expression + " /out/\n")
                output = root / "image.tar"
                build(dockerfile, context, None, None, "example/app:1", output)
                _, index = self.read_image(output, root)
                self.assertEqual(index.read_file("out/nested/app.war"), b"WAR")
                self.assertEqual([path for path, entry in index.entries.items() if entry.kind == "file"],
                                 ["out/nested/app.war"])

    def test_legacy_parents_layer_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / ".hidden").mkdir()
            (context / ".hidden" / "app.war").write_bytes(b"WAR")
            recipe = "FROM scratch\nCOPY --parents .hidden/./app.war /out/\n"
            dockerfile = context / "Dockerfile"
            dockerfile.write_text(recipe)
            instruction = parse(recipe)[1]
            fingerprint = LayerBuilder(context, RootFSIndex()).fingerprint_sources(instruction.value)
            legacy_key = cache_key("instruction", 2, cache_key("scratch-base", "linux/amd64", 0),
                                   instruction.raw, fingerprint)
            legacy_layer = root / "legacy.tar"
            layer_file(legacy_layer, {"out/.hidden/app.war": b"WAR"})
            cache = LayerCache(root / "cache")
            cache.store(legacy_key, "sha256:" + sha256_file(legacy_layer), legacy_layer)
            entry_path = cache.entries / (legacy_key + ".json")
            entry = json.loads(entry_path.read_text())
            entry["version"] = 2
            entry_path.write_text(json.dumps(entry))
            output = root / "image.tar"
            stats = {}
            build(dockerfile, context, None, None, "example/app:1", output,
                  cache_dir=root / "cache", cache_stats=stats)
            _, index = self.read_image(output, root)
            self.assertEqual(index.read_file("out/app.war"), b"WAR")
            self.assertIsNone(index.kind("out/.hidden/app.war"))
            self.assertEqual((stats["hits"], stats["misses"]), (0, 1))

    def test_invalid_external_onbuild_fails_without_publication(self):
        for trigger in ("", "# comment only", "ENV A=1\nCOPY payload.war /app.war",
                        "ENV A=1\nRUN false", "FROM scratch"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "payload.war").write_bytes(b"WAR")
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM example/base:1\nENV CHILD=1\n")
                config = {"os": "linux", "architecture": "amd64", "history": [],
                          "config": {"OnBuild": [trigger]}, "rootfs": {"type": "layers", "diff_ids": []}}
                base = root / "base.tar"
                ImageArchiveWriter().write(base, config, [], "example/base:1")
                output = root / "image.tar"
                with self.assertRaises(BuildError):
                    build(dockerfile, context, base, None, "example/app:1", output,
                          cache_dir=root / "cache")
                self.assertFalse(output.exists())
                self.assertEqual(list((root / "cache" / "entries").glob("*.json")), [])

    def test_valid_multiline_onbuild_heredoc_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\n")
            config = {"os": "linux", "architecture": "amd64", "history": [],
                      "config": {"OnBuild": ["COPY <<EOF /inline.txt\nhello\nEOF"]},
                      "rootfs": {"type": "layers", "diff_ids": []}}
            base = root / "base.tar"
            ImageArchiveWriter().write(base, config, [], "example/base:1")
            output = root / "image.tar"
            build(dockerfile, context, base, None, "example/app:1", output)
            image, index = self.read_image(output, root)
            self.assertEqual(index.read_file("inline.txt"), b"hello\n")
            self.assertNotIn("OnBuild", image.config["config"])


if __name__ == "__main__":
    unittest.main()
