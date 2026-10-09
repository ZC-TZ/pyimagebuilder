"""无需 Docker/Podman，验证单应用包的 Fast 打包流程。"""

import contextlib
import errno
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast
import main as builder_main
from errors import BuildError
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from test_phase1 import make_base


class FastBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = make_base(self.root)
        self.profile = self.root / "config.json"

    def init(self, flavor="tomcat", **kwargs):
        options = ["init", "--base-tar", str(self.base), "--base-image", "example/base:1",
                   "--flavor", flavor, "--owner", "1000:1000", "--config", str(self.profile)]
        for key, value in kwargs.items():
            options += ["--" + key.replace("_", "-"), str(value)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fast.main(options), 0)

    def inspect(self, archive, tag, expected_path, contents):
        with tempfile.TemporaryDirectory() as temporary:
            image = ImageArchiveReader(archive, Path(temporary)).read(tag)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.kind(expected_path), "file")
            import tarfile
            with tarfile.open(image.layers[-1]) as layer:
                self.assertEqual(layer.extractfile(expected_path).read(), contents)

    def test_explicit_project_and_version_override_war_filename(self):
        self.init()
        war = self.root / "report-1.2.3.war"
        war.write_bytes(b"WAR contents")
        old = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fast.main([str(war), "sfcx", "1.3.4"]), 0)
        finally:
            os.chdir(old)
        archive = self.root / "sfcx_back-1.3.4.tar"
        self.inspect(archive, "sfcx_back:1.3.4",
                     "soft/tomcat/webapps/sfcx.war", b"WAR contents")

    def test_war_hardlink_falls_back_to_copy(self):
        self.init()
        war = self.root / "app.war"
        war.write_bytes(b"WAR fallback")
        linked, _ = fast.quick_build(war, "sfcx", "1.3.4", self.profile,
                                     output=self.root / "linked.tar")
        with patch.object(fast.os, "link", side_effect=OSError(errno.EXDEV, "different filesystem")):
            output, tag = fast.quick_build(war, "sfcx", "1.3.4", self.profile,
                                           output=self.root / "copied.tar")
        self.inspect(output, tag, "soft/tomcat/webapps/sfcx.war", b"WAR fallback")
        self.assertEqual(linked.read_bytes(), output.read_bytes())

    def test_main_fast_and_standalone_fast_share_the_same_workflow(self):
        init_args = ["init", "--base-tar", str(self.base),
                     "--base-image", "example/base:1", "--flavor", "tomcat",
                     "--owner", "1000:1000", "--config", str(self.profile)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_main.main(["fast", *init_args]), 0)
        war = self.root / "report-1.2.3.war"
        war.write_bytes(b"WAR contents")
        via_main = self.root / "via-main.tar"
        standalone = self.root / "standalone.tar"
        args = [str(war), "sfcx", "1.3.4", "--config", str(self.profile)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_main.main(["fast", *args, "-o", str(via_main)]), 0)
            self.assertEqual(fast.main([*args, "-o", str(standalone)]), 0)
        self.assertEqual(via_main.read_bytes(), standalone.read_bytes())
        self.inspect(via_main, "sfcx_back:1.3.4",
                     "soft/tomcat/webapps/sfcx.war", b"WAR contents")

    def test_dist_zip_extracts_both_layouts(self):
        self.init("tongweb")
        for layout in ("dist/index.html", "index.html"):
            with self.subTest(layout=layout):
                folder = self.root / ("web-a" if layout.startswith("dist/") else "web-b")
                folder.mkdir()
                archive_path = folder / "dist.zip"
                with zipfile.ZipFile(archive_path, "w") as package:
                    package.writestr(layout, b"<h1>hello</h1>")
                result, tag = fast.quick_build(archive_path, "sfcx", "1.3.4", self.profile)
                self.assertEqual(tag, "sfcx_front:1.3.4")
                self.inspect(result, tag,
                             "soft/TongWeb7.0/autodeploy/sfcxf/index.html",
                             b"<h1>hello</h1>")

    def test_profile_can_copy_server_config(self):
        config = self.root / "tongweb.xml"
        config.write_text("<tongweb/>\n", encoding="utf-8")
        self.init("tongweb", server_config=config)
        war = self.root / "app.war"
        war.write_bytes(b"app")
        dockerfiles = []
        original_build = fast.build
        def capture_build(dockerfile, *args, **kwargs):
            dockerfiles.append(dockerfile.read_text(encoding="utf-8"))
            return original_build(dockerfile, *args, **kwargs)
        with patch.object(fast, "build", side_effect=capture_build):
            output, tag = fast.quick_build(war, "sfcx", "1.3.4", self.profile)
        self.assertIn("COPY --chown=1000:1000 tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml",
                      dockerfiles[0])
        with tempfile.TemporaryDirectory() as temporary:
            image = ImageArchiveReader(output, Path(temporary)).read(tag)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.kind("soft/TongWeb7.0/conf/tongweb.xml"), "file")

    def test_zip_traversal_rejected_without_output(self):
        self.init()
        folder = self.root / "web"
        folder.mkdir()
        archive_path = folder / "dist.zip"
        with zipfile.ZipFile(archive_path, "w") as package:
            package.writestr("../escape.txt", "bad")
        with patch.object(fast, "_base_archive") as pull_base:
            with self.assertRaises(BuildError):
                fast.quick_build(archive_path, "sfcx", "1.3.4", self.profile)
            pull_base.assert_not_called()
        self.assertFalse((folder / "sfcx_front-1.3.4.tar").exists())

    def test_zip_file_directory_conflicts_fail_before_extraction(self):
        for names in (("dist", "dist/index.html"),
                      ("assets", "assets/index.html"),
                      ("assets/index.html", "assets")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "dist.zip"
                with zipfile.ZipFile(archive_path, "w") as package:
                    for name in names:
                        package.writestr(name, b"data")
                destination = root / "extracted"
                with self.assertRaises(BuildError):
                    fast._extract_dist(archive_path, destination)
                self.assertFalse(destination.exists())

    def test_project_and_version_are_required(self):
        war = self.root / "report-1.2.3.war"
        war.write_bytes(b"app")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                fast.main([str(war)])
        self.assertEqual(error.exception.code, 2)

    def test_base_tar_tag_must_match_from_reference(self):
        self.init()
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        profile["fast"]["profiles"]["default"]["baseImage"] = "wrong/base:9"
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        war = self.root / "app.war"
        war.write_bytes(b"app")
        with self.assertRaisesRegex(BuildError, "does not contain FROM tag"):
            fast.quick_build(war, "sfcx", "1.3.4", self.profile)


if __name__ == "__main__":
    unittest.main()
