"""验证现代 Dockerfile 元数据和文件传输指令语义。"""

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from dockerfile_parser import parse
from errors import BuildError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from build_args import expand
from platforms import host_architecture


class ModernDockerfileTests(unittest.TestCase):
    def _image(self, archive, tag, root):
        return ImageArchiveReader(archive, root / "unpacked").read(tag)

    def test_healthcheck_signal_platform_args_and_variable_modifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "FROM --platform=$TARGETPLATFORM scratch\n"
                "ARG TARGETARCH\nENV ARCH=${TARGETARCH:-unknown} NAME=${MISSING-default}\n"
                "HEALTHCHECK --interval=30s --timeout=3s --retries=4 CMD curl -f localhost\n"
                "STOPSIGNAL SIGQUIT\nMAINTAINER Example Team\n")
            output = root / "image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", output)
            config = self._image(output, "demo:1", root).config
            self.assertIn("ARCH=amd64", config["config"]["Env"])
            self.assertIn("NAME=default", config["config"]["Env"])
            self.assertEqual(config["config"]["Healthcheck"]["Interval"], 30_000_000_000)
            self.assertEqual(config["config"]["Healthcheck"]["Timeout"], 3_000_000_000)
            self.assertEqual(config["config"]["StopSignal"], "SIGQUIT")
            self.assertEqual(config["author"], "Example Team")
            self.assertEqual(expand("${A:-x}/${A-y}/${A:+z}/${B+q}/\\$A", {"A": "", "B": "1"}),
                             "x///q/$A")

    def test_heredoc_exclude_parents_and_unpack_false(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            (context / "src").mkdir(parents=True)
            (context / "src" / "keep.txt").write_bytes(b"keep")
            (context / "src" / "skip.log").write_bytes(b"skip")
            tar_path = context / "blob.tar"
            with tarfile.open(tar_path, "w") as archive:
                payload = b"inside"
                info = tarfile.TarInfo("inside.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            (context / "Dockerfile").write_text(
                "# syntax=docker/dockerfile:1\nFROM scratch\n"
                "COPY --exclude=*.log src/ /app/\n"
                "COPY --parents src/keep.txt /parent/\n"
                "COPY <<EOF /app/hello.txt\nhello\nEOF\n"
                "ADD --unpack=false blob.tar /app/blob.tar\n")
            output = root / "image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", output)
            image = self._image(output, "demo:1", root)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/app/hello.txt"), b"hello\n")
            self.assertEqual(index.read_file("/app/keep.txt"), b"keep")
            self.assertEqual(index.read_file("/parent/src/keep.txt"), b"keep")
            self.assertIsNone(index.kind("app/skip.log"))
            self.assertEqual(index.read_file("/app/blob.tar"), tar_path.read_bytes())

    def test_onbuild_trigger_runs_in_child_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent"
            child = root / "child"
            parent.mkdir()
            child.mkdir()
            (parent / "Dockerfile").write_text("FROM scratch\nONBUILD COPY data.txt /inherited.txt\n")
            base = root / "base.tar"
            build(parent / "Dockerfile", parent, None, None, "demo/parent:1", base)
            (child / "Dockerfile").write_text("FROM demo/parent:1\nENV CHILD=yes\n")
            (child / "data.txt").write_bytes(b"downstream")
            output = root / "child.tar"
            build(child / "Dockerfile", child, base, None, "demo/child:1", output)
            image = self._image(output, "demo/child:1", root)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/inherited.txt"), b"downstream")
            self.assertNotIn("OnBuild", image.config["config"])

    def test_remote_add_checksum_and_run_flags_parse(self):
        script = "FROM scratch\nRUN --network=none --security=sandbox echo hi\n"
        run = parse(script)[1].value
        self.assertEqual((run.network, run.security), ("none", "sandbox"))
        content = b"remote"
        checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "FROM scratch\nADD --checksum={} https://example.test/data.txt /remote.txt\n".format(checksum))
            class Response(io.BytesIO):
                headers = {"Content-Length": str(len(content))}
                def geturl(self):
                    return "https://example.test/data.txt"
            with patch("remote_add.urlopen", side_effect=lambda *args, **kwargs: Response(content)):
                output = root / "image.tar"
                build(context / "Dockerfile", context, None, None, "demo:1", output)
            image = self._image(output, "demo:1", root)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/remote.txt"), content)

    def test_copy_link_layer_reused_after_prior_metadata_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "payload").write_bytes(b"linked")
            dockerfile = context / "Dockerfile"
            cache = root / "cache"
            statistics = []
            for number in (1, 2):
                dockerfile.write_text("FROM scratch\nENV VERSION={}\nCOPY --link payload /app/payload\n".format(number))
                stats = {}
                build(dockerfile, context, None, None, "demo:1", root / ("image{}.tar".format(number)),
                      cache_dir=cache, cache_stats=stats)
                statistics.append(stats)
            self.assertEqual(statistics[0]["misses"], 1)
            self.assertEqual(statistics[1]["hits"], 1)

    def test_copy_link_cache_tracks_expansion_workdir_and_epoch(self):
        scenarios = (
            ("ENV DEST=/first OWNER=1:2\nCOPY --link --chown=$OWNER payload $DEST\n",
             "ENV DEST=/second OWNER=3:4\nCOPY --link --chown=$OWNER payload $DEST\n",
             (0, 0), "second", (3, 4)),
            ("WORKDIR /first\nCOPY --link payload output\n",
             "WORKDIR /second\nCOPY --link payload output\n",
             (0, 0), "second/output", (0, 0)),
            ("COPY --link payload /output\n", "COPY --link payload /output\n",
             (0, 123), "output", (0, 0)),
        )
        for first, second, epochs, expected_path, owner in scenarios:
            with self.subTest(path=expected_path, epochs=epochs), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "ctx"
                context.mkdir()
                (context / "payload").write_bytes(b"linked")
                dockerfile = context / "Dockerfile"
                for number, text in enumerate((first, second)):
                    dockerfile.write_text("FROM scratch\n" + text)
                    build(dockerfile, context, None, None, "demo:1", root / ("image{}.tar".format(number)),
                          cache_dir=root / "cache", source_date_epoch=epochs[number])
                image = self._image(root / "image1.tar", "demo:1", root)
                with tarfile.open(image.layers[-1]) as archive:
                    member = archive.getmember(expected_path)
                    self.assertEqual((member.uid, member.gid), owner)
                    self.assertEqual(member.mtime, epochs[1])

    def test_quoted_heredoc_escape_directive_and_health_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "# escape=`\nFROM scratch\nARG NAME=expanded\n"
                "COPY <<'EOF' /literal.txt\n$NAME\nEOF\n"
                "COPY <<EOF /expanded.txt\n$NAME\nEOF\n"
                "HEALTHCHECK NONE\n")
            output = root / "image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", output)
            image = self._image(output, "demo:1", root)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/literal.txt"), b"$NAME\n")
            self.assertEqual(index.read_file("/expanded.txt"), b"expanded\n")
            self.assertEqual(image.config["config"]["Healthcheck"], {"Test": ["NONE"]})

    def test_run_heredoc_and_security_flags(self):
        instructions = parse("FROM scratch\nRUN <<EOF\necho one\necho two\nEOF\n")
        self.assertTrue(instructions[1].value.command.shell_form)
        self.assertEqual(instructions[1].value.command.value, "echo one\necho two\n")
        self.assertEqual(instructions[1].line, 2)
        combined = parse("FROM scratch\nRUN <<A cat > /a && <<B cat > /b\nfirst\nA\nsecond\nB\n")
        self.assertTrue(combined[1].value.command.shell_form)
        self.assertIn("first\nA\nsecond\nB", combined[1].value.command.value)
        quoted = parse("FROM scratch\nRUN echo '<<EOF'\n")
        self.assertEqual(quoted[1].value.command.value, "echo '<<EOF'")
        with self.assertRaises(BuildError):
            parse("FROM scratch\nRUN --security=privileged echo hi\n")
        escaped = parse("# escape=`\nFROM scratch\nENV A=one `\n B=two\n")
        self.assertEqual(escaped[1].value, [["A", "one"], ["B", "two"]])
        self.assertEqual(expand("${S#foo}/${S%%bar}/${S/oo/EE}", {"S": "foobar"}),
                         "bar/foo/fEEbar")

    def test_offline_remote_add_and_bad_signal_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nADD https://example.test/a /a\n")
            with self.assertRaises(BuildError):
                build(context / "Dockerfile", context, None, None, "demo:1",
                      root / "output.tar", allow_remote_add=False)
        with self.assertRaises(BuildError):
            parse("FROM scratch\nSTOPSIGNAL SIGFAKE\n")

    def test_entrypoint_resets_inherited_cmd_and_shell_form(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent"
            child = root / "child"
            parent.mkdir()
            child.mkdir()
            (parent / "Dockerfile").write_text("FROM scratch\nCMD [\"old\"]\n")
            base = root / "base.tar"
            build(parent / "Dockerfile", parent, None, None, "demo/base:1", base)
            (child / "Dockerfile").write_text(
                "FROM demo/base:1\nSHELL [\"/bin/bash\",\"-c\"]\nENTRYPOINT echo ready\n")
            output = root / "child.tar"
            build(child / "Dockerfile", child, base, None, "demo/child:1", output)
            config = self._image(output, "demo/child:1", root).config["config"]
            self.assertIsNone(config["Cmd"])
            self.assertEqual(config["Entrypoint"], ["/bin/bash", "-c", "echo ready"])

    def test_add_exclude_and_remote_unpack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                for name, value in (("keep.txt", b"keep"), ("skip.log", b"skip")):
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
            tar_data = payload.getvalue()
            (context / "local.tar").write_bytes(tar_data)
            (context / "Dockerfile").write_text(
                "FROM scratch\nADD --exclude=*.log local.tar /local/\n")
            local_image = root / "local-image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", local_image)
            local_index = RootFSIndex()
            for layer in self._image(local_image, "demo:1", root).layers:
                local_index.apply_layer(layer)
            self.assertEqual(local_index.read_file("/local/keep.txt"), b"keep")
            self.assertIsNone(local_index.kind("local/skip.log"))
            checksum = "sha256:" + hashlib.sha256(tar_data).hexdigest()
            (context / "Dockerfile").write_text(
                "FROM scratch\nADD --unpack=true --checksum={} https://example.test/file.tar /remote/\n".format(checksum))
            class Response(io.BytesIO):
                headers = {"Content-Length": str(len(tar_data))}
                def geturl(self):
                    return "https://example.test/file.tar"
            with patch("remote_add.urlopen", side_effect=lambda *args, **kwargs: Response(tar_data)):
                remote_image = root / "remote-image.tar"
                build(context / "Dockerfile", context, None, None, "demo:2", remote_image)
            remote_index = RootFSIndex()
            with tempfile.TemporaryDirectory() as unpacked:
                for layer in ImageArchiveReader(remote_image, Path(unpacked)).read("demo:2").layers:
                    remote_index.apply_layer(layer)
                self.assertEqual(remote_index.read_file("/remote/keep.txt"), b"keep")

    def test_excluded_directory_does_not_leak_from_stage_or_add_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            (context / "src" / "private").mkdir(parents=True)
            (context / "src" / "private" / "secret.txt").write_bytes(b"secret")
            (context / "src" / "public.txt").write_bytes(b"public")
            with tarfile.open(context / "bundle.tar", "w") as archive:
                for name, content in (("private/secret.txt", b"secret"),
                                      ("public.txt", b"public")):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            (context / "Dockerfile").write_text(
                "FROM scratch AS source\n"
                "COPY src/ /source/\n"
                "FROM scratch\n"
                "COPY --from=source --exclude=private /source/ /stage/\n"
                "ADD --exclude=private bundle.tar /archive/\n")
            output = root / "image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", output)
            index = RootFSIndex()
            for image_layer in self._image(output, "demo:1", root).layers:
                index.apply_layer(image_layer)
            self.assertEqual(index.read_file("/stage/public.txt"), b"public")
            self.assertEqual(index.read_file("/archive/public.txt"), b"public")
            self.assertIsNone(index.kind("stage/private"))
            self.assertIsNone(index.kind("stage/private/secret.txt"))
            self.assertIsNone(index.kind("archive/private/secret.txt"))

    def test_run_flags_require_explicit_host_and_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nRUN --network=host echo hi\n")
            with self.assertRaises(UnsupportedInstruction):
                build(dockerfile, context, None, None, "demo:1", root / "a.tar",
                      enable_run=True)
            dockerfile.write_text("FROM scratch\nRUN --security=insecure echo hi\n")
            with self.assertRaises(UnsupportedInstruction):
                build(dockerfile, context, None, None, "demo:1", root / "b.tar",
                      enable_run=True)

    def test_build_platform_can_differ_from_target(self):
        if host_architecture() is None:
            self.skipTest("Host architecture unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "ctx"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "FROM --platform=$BUILDPLATFORM scratch\nARG TARGETARCH\nENV CROSS=$TARGETARCH\n")
            output = root / "image.tar"
            build(context / "Dockerfile", context, None, None, "demo:1", output,
                  target_platform="linux/arm64")
            config = self._image(output, "demo:1", root).config
            self.assertEqual(config["architecture"], host_architecture())
            self.assertIn("CROSS=arm64", config["config"]["Env"])


if __name__ == "__main__":
    unittest.main()
