"""集中验证路径别名、Windows 特殊组件和消费端边界，防止局部修补再次漂移。"""

import io
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast
import attest
import registry
import artifactory_download
import cas_store
from builder import build
from cache import LayerCache
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader, archive_members, outer_name
from image_cli import disk_usage
from path_policy import (archive_path, container_path, host_components, host_path_aliases,
                         output_path, overlay_path)
from rootfs import RootFSIndex, clean_path
from rootfs_materializer import RootFSMaterializer
from run_mounts import normalize_mount_target
from store_lock import file_lock
from test_context_inputs import directory_link
from test_phase1 import make_base


class PathPolicyTests(unittest.TestCase):
    def test_container_aliases_are_canonical(self):
        for value, expected in (("//proc", "/proc"), ("///cache/./x", "/cache/x"),
                                ("/cache/..", "/"), ("//", "/"), ("/../x", "/x")):
            with self.subTest(value=value):
                self.assertEqual(container_path(value), expected)
        self.assertEqual(container_path("../x", "//app/y"), "/app/x")

    def test_invalid_container_inputs(self):
        for value in (None, 1, "", "/a\0b", "/a\\b"):
            with self.subTest(value=value), self.assertRaises(BuildError):
                container_path(value)
        with self.assertRaises(BuildError):
            container_path("relative", "relative-workdir")

    def test_all_root_aliases_rejected_by_sensitive_consumers(self):
        for name in ("/", "//", "///", "/./", "/cache/..", "//cache/../."):
            with self.subTest(name=name):
                with self.assertRaises(BuildError):
                    container_path(name, non_root=True)
                with self.assertRaises(BuildError):
                    normalize_mount_target(name, set())
                with self.assertRaises(BuildError):
                    fast._validate_path(name, "deployDir")

    def test_virtual_mount_aliases(self):
        for name in ("//proc", "///sys/a", "/cache/../dev", "/x/../proc/./fd"):
            with self.subTest(name=name), self.assertRaises(BuildError):
                normalize_mount_target(name, set())
        targets = set()
        normalize_mount_target("//cache/./", targets)
        with self.assertRaises(BuildError):
            normalize_mount_target("/cache/a/..", targets)

    def test_archive_aliases_consistent_between_consumers(self):
        for name, expected in (("a//b", "a/b"), ("./a/./b/", "a/b"),
                               (".//a", "a"), ("././.", ""), ("a:b", "a:b")):
            with self.subTest(name=name):
                for normalize in (archive_path, clean_path, outer_name):
                    self.assertEqual(normalize(name), expected)

    def test_archive_traversal_is_rejected_before_normalization(self):
        for name in ("/a", "//a", "a/../b", "./../a", "a\\b", "a\0b", None):
            with self.subTest(name=name):
                for normalize in (archive_path, clean_path, outer_name):
                    with self.assertRaises(ArchiveError):
                        normalize(name)

    def test_outer_alias_duplicates_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.addfile(tarfile.TarInfo("a/b"))
            archive.addfile(tarfile.TarInfo("a//./b"))
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive, self.assertRaises(ArchiveError):
            archive_members(archive)

    def test_outer_root_header_must_be_directory(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.addfile(tarfile.TarInfo("."))
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive, self.assertRaises(ArchiveError):
            archive_members(archive)

    def test_windows_component_matrix(self):
        for name in ("C:", "a:stream", "NUL", "nul.txt", "COM1", "LPT¹.txt",
                     "dir.", "dir ", "bad|name", "bad\x01name", "CONIN$"):
            with self.subTest(name=name), self.assertRaises(BuildError):
                host_components(["safe", name], windows=True)
        host_components(["normal.txt", "com10", "null"], windows=True)
        host_components(["a:stream", "NUL"], windows=False)

    def test_zip_invalid_host_paths_leave_no_output(self):
        # 显式选择 Windows 组件策略，使 Linux CI 也能复现同一输入边界。
        for name in ("dist/sub/C:/file", "dist/a:stream", "dist/NUL.txt", "dist/dir./file"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, target = root / "dist.zip", root / "dist"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("dist/valid.txt", "valid")
                    archive.writestr(name, "invalid")
                with patch.object(fast, "host_components", lambda parts: host_components(parts, windows=True)):
                    with self.assertRaises(BuildError):
                        fast._extract_dist(source, target)
                self.assertFalse(target.exists())

    def test_zip_normalized_duplicates_leave_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "dist.zip", root / "dist"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("dist/a/b", "one")
                archive.writestr("dist/a//./b", "two")
            with self.assertRaises(BuildError):
                fast._extract_dist(source, target)
            self.assertFalse(target.exists())

    def test_provenance_rejects_directory_links_without_hashing_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, outside = root / "context", root / "outside"
            context.mkdir()
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"must not read")
            link = context / "linked"
            directory_link(outside, link)
            try:
                # 普通符号链接可记录目标文本；Windows junction 必须在遍历前拒绝。
                with patch.object(attest, "sha256_file") as hashed:
                    if os.name == "nt":
                        with self.assertRaises(BuildError):
                            attest.context_digest(context)
                    else:
                        attest.context_digest(context)
                    hashed.assert_not_called()
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_output_preflight_does_not_erase_final_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nWORKDIR //app/./\n")
            target = root / "output.tar"
            original = Path.is_symlink
            # Windows 无文件 symlink 权限时模拟 lstat 结果；断言构建入口也接入策略。
            with patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
                with self.assertRaises(BuildError):
                    output_path(target)
                with self.assertRaises(BuildError):
                    build(dockerfile, context, None, None, "test/path:1", target)
            self.assertFalse(target.exists())

    def test_cache_keys_cannot_escape_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = LayerCache(root / "cache")
            for key in ("../outside", "nested/key", "C:stream", "", ".", None):
                with self.subTest(key=key):
                    with self.assertRaises(BuildError):
                        cache.store(key, None, None)
                    with self.assertRaises(BuildError):
                        cache.lookup(key)
            self.assertEqual(list(cache.entries.iterdir()), [])

    def test_lock_alias_is_reentrant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "directory").mkdir()
            with file_lock(root / "test.lock"):
                with file_lock(root / "directory" / ".." / "test.lock"):
                    pass

    def test_registry_upload_paths_reject_encoded_traversal(self):
        for path in ("/v2/r/blobs/uploads/%2e%2e/manifests/tag", "/v2/r/blobs/uploads/../x",
                     "/v2/r/blobs/uploads/a%2fb", "/v2/r/blobs/uploads/%252e%252e/x",
                     "/v2/r/blobs/uploads/a%5cb", "/v2/r/blobs/uploads/a%00", "//v2/r"):
            with self.subTest(path=path), self.assertRaises(BuildError):
                registry.validate_upload_path(path)
        registry.validate_upload_path("/v2/r/blobs/uploads/abc-123")

    def test_artifactory_encoded_directories_and_windows_names(self):
        self.assertIsNone(artifactory_download.safe_relative_name("blob%2f"))
        self.assertIsNone(artifactory_download.safe_relative_name("blob%00"))
        self.assertIsNone(artifactory_download.safe_relative_name("a/%2e%2e/b"))

    def test_download_direct_api_checks_path_before_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(BuildError):
                artifactory_download.download_one(None, "https://local.invalid/", "../outside",
                                                 root, None, None, None, None)
            self.assertEqual(list(root.iterdir()), [])

    def test_overlay_option_separators_rejected(self):
        for name in ("/tmp/a,b", "/tmp/a:b", "/tmp/a\\b", "/tmp/a\nother", "/tmp/a\0b"):
            with self.subTest(name=name), self.assertRaises(BuildError):
                overlay_path(name)
        self.assertEqual(overlay_path("/tmp/normal space"), "/tmp/normal space")

    def test_windows_parent_aliases_checked_as_batch(self):
        for names in (("dir/a", "DIR/b"), ("a", "A/b"), ("a.txt", "A.TXT")):
            with self.subTest(names=names), self.assertRaises(BuildError):
                host_path_aliases(names, windows=True)
        host_path_aliases(("dir/a", "dir/b", "dir"), windows=True)

    def test_zip_case_aliases_fail_before_extracting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "dist.zip", root / "dist"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("dist/Assets/a", "one")
                archive.writestr("dist/assets/b", "two")
            with patch.object(fast, "host_path_aliases",
                              lambda names: host_path_aliases(names, windows=True)):
                with self.assertRaises(BuildError):
                    fast._extract_dist(source, target)
            self.assertFalse(target.exists())

    def test_invalid_hardlink_rejected_before_whiteout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "bad.tar"
            with tarfile.open(layer, "w") as archive:
                archive.addfile(tarfile.TarInfo(".wh.keep"))
                link = tarfile.TarInfo("link")
                link.type = tarfile.LNKTYPE
                link.linkname = "../outside"
                archive.addfile(link)
            index = RootFSIndex()
            sentinel = object()
            index.entries["keep"] = sentinel
            with self.assertRaises(ArchiveError):
                index.apply_layer(layer)
            self.assertIs(index.entries["keep"], sentinel)

    def test_base_cache_directory_junction_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache, outside = root / "cache", root / "outside"
            cache.mkdir()
            outside.mkdir()
            link = cache / "layers"
            directory_link(outside, link)
            try:
                reader = ImageArchiveReader(root / "input.tar", root / "work", cache)
                with self.assertRaises(ArchiveError):
                    reader._cached_layer("layer.tar", "sha256:" + "a" * 64)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_artifact_pack_rejects_linked_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, outside = root / "source", root / "outside"
            source.mkdir()
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"must not pack")
            link = source / "linked"
            directory_link(outside, link)
            try:
                with self.assertRaises(BuildError):
                    artifactory_download.pack_files(source, root / "output.tar", None)
                self.assertFalse((root / "output.tar").exists())
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_rootfs_removal_does_not_follow_host_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            materializer = RootFSMaterializer(root / "rootfs")
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"keep")
            link = materializer.root / "linked"
            directory_link(outside, link)
            try:
                if os.name == "nt":
                    with self.assertRaises(ArchiveError):
                        materializer._remove("linked")
                    self.assertIsNone(materializer._whiteout_directory("linked"))
                else:
                    materializer._remove("linked")
                self.assertEqual(sentinel.read_bytes(), b"keep")
            finally:
                if link.exists() or link.is_symlink():
                    if os.name == "nt":
                        link.rmdir()
                    else:
                        link.unlink()

    def test_cas_ref_link_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = cas_store.CASStore(Path(temporary))
            ref = store._ref("test/path:1", "linux/amd64", "local")
            ref.parent.mkdir(parents=True)
            ref.write_text("{}")
            original = Path.is_symlink
            with patch.object(Path, "is_symlink", lambda path: path == ref or original(path)):
                with patch.object(cas_store, "read_image_json_file") as reader:
                    with self.assertRaises(ArchiveError):
                        store.resolve("test/path:1", source="local")
                    self.assertEqual(store.list_refs(), [])
                    reader.assert_not_called()

    def test_disk_usage_does_not_count_linked_outside_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, outside = root / "store", root / "outside"
            store.mkdir()
            outside.mkdir()
            (store / "inside").write_bytes(b"ok")
            (outside / "sentinel").write_bytes(b"outside")
            link = store / "linked"
            directory_link(outside, link)
            try:
                usage = disk_usage(store, root / "cache")
                self.assertEqual(usage["image_store"]["bytes"], 2)
                self.assertEqual(usage["image_store"]["files"], 1)
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_registry_location_validation_prevents_put(self):
        with tempfile.TemporaryDirectory() as temporary:
            blob = Path(temporary) / "blob"
            blob.write_bytes(b"test")
            descriptor = {"size": 4, "digest": "sha256:" + hashlib.sha256(b"test").hexdigest()}
            client = registry.RegistryClient("local.invalid/repo:1")
            for location in ("/v2/repo/blobs/uploads/%2e%2e/manifests/1",
                             "/v2/repo/blobs/uploads/%ff", "/v2/repo/blobs/uploads/a\tb"):
                with self.subTest(location=location), patch.object(client, "request",
                        side_effect=[(404, {}, b""), (202, {"location": location}, b"")]) as request:
                    with self.assertRaises(BuildError):
                        client.upload_file(blob, descriptor)
                    self.assertEqual(request.call_count, 2)

    def test_build_canonicalizes_workdir_and_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"valid")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text('FROM scratch\nCOPY ["payload", "//app/./file"]\nWORKDIR //app/.\n')
            output = root / "result.tar"
            build(dockerfile, context, None, None, "test/path:1", output)
            image = ImageArchiveReader(output, root / "read").read("test/path:1")
            self.assertEqual(image.config["config"]["WorkingDir"], "/app")
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/app/file"), b"valid")
            self.assertEqual(index.kind("//app/./file"), "file")

    def test_fast_deployment_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            base = make_base(root)
            config.write_text(json.dumps({"schemaVersion": 2, "fast": {"defaultProfile": "test",
                "profiles": {"test": {"flavor": "tomcat", "baseImage": "example/base:1",
                "baseTar": str(base), "owner": "0:0", "deployDir": "/app with spaces"}}}}))
            war = root / "app.war"
            war.write_bytes(b"WAR")
            result, tag = fast.quick_build(war, "demo", "1", config, cache_dir=root / "cache")
            image = ImageArchiveReader(result, root / "read").read(tag)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("/app with spaces/demo.war"), b"WAR")


if __name__ == "__main__":
    unittest.main()
