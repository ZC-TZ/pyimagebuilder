"""对照归档索引与实际文件系统，验证目录属主和硬链接层应用。"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cache import LayerCache, cache_key
from cas_store import CASStore
from dockerfile_parser import parse
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from layer import LayerBuilder
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


def write_layer(path, entries):
    """按指定顺序写入文件/硬链接，便于复现前向引用和旧层遮蔽。"""
    with tarfile.open(path, "w") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            if kind == "file":
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.type = tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)


class FilesystemSemanticsTests(unittest.TestCase):
    def read_image(self, output, root):
        image = ImageArchiveReader(output, root / (output.stem + "-layers")).read("example/app:1")
        index = RootFSIndex()
        for layer in image.layers:
            index.apply_layer(layer)
        return image, index

    def identity_context(self, root):
        context = root / "context"
        context.mkdir()
        (context / "passwd").write_text("app:x:1201:1202::/app:/bin/sh\n", encoding="utf-8")
        (context / "group").write_text("staff:x:1300:\n", encoding="utf-8")
        (context / "payload").write_bytes(b"WAR")
        return context

    def test_user_controls_new_workdir_ownership_and_cache(self):
        for user, owner in (("1201:1300", (1201, 1300)), ("1201", (1201, 1201)),
                            ("app", (1201, 1202)), ("app:staff", (1201, 1300)),
                            ("1201:staff", (1201, 1300))):
            with self.subTest(user=user), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = self.identity_context(root)
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nCOPY passwd /etc/passwd\nCOPY group /etc/group\n"
                                      "USER " + user + "\nWORKDIR /owned/nested\n")
                for number in range(2):
                    output = root / ("owned-{}.tar".format(number))
                    stats = {}
                    build(dockerfile, context, None, None, "example/app:1", output,
                          cache_dir=root / "cache", cache_stats=stats)
                    image, _ = self.read_image(output, root)
                    with tarfile.open(image.layers[-1]) as archive:
                        for name in ("owned", "owned/nested"):
                            member = archive.getmember(name)
                            self.assertEqual((member.uid, member.gid, member.mode), (*owner, 0o755))
                    self.assertEqual(stats["hits"], number * 3)

    def test_named_chown_uses_primary_group_when_group_is_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.identity_context(root)
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY passwd /etc/passwd\n"
                                  "COPY --chown=app payload /app.war\n")
            output = root / "chown.tar"
            build(dockerfile, context, None, None, "example/app:1", output)
            image, _ = self.read_image(output, root)
            with tarfile.open(image.layers[-1]) as archive:
                member = archive.getmember("app.war")
                self.assertEqual((member.uid, member.gid), (1201, 1202))

    def test_inherited_user_and_existing_directory_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch AS parent\nUSER 123:456\nWORKDIR /shared\n"
                                  "FROM parent\nWORKDIR /inherited\nUSER 789:987\nWORKDIR /shared/nested\n")
            output = root / "inherited.tar"
            build(dockerfile, context, None, None, "example/app:1", output)
            image, index = self.read_image(output, root)
            for path, expected in (("shared", (123, 456)), ("inherited", (123, 456)),
                                   ("shared/nested", (789, 987))):
                entry = index.entries[path]
                with tarfile.open(entry.layer) as archive:
                    member = archive.getmember(entry.member)
                    self.assertEqual((member.uid, member.gid), expected)
            self.assertEqual(image.config["config"]["User"], "789:987")

    def test_workdir_unknown_user_fails_without_output(self):
        for user in ("missing", "1000:missing"):
            with self.subTest(user=user), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                dockerfile = context / "Dockerfile"
                dockerfile.write_text("FROM scratch\nUSER " + user + "\nWORKDIR /owned\n")
                output = root / "bad.tar"
                with self.assertRaises(BuildError):
                    build(dockerfile, context, None, None, "example/app:1", output)
                self.assertFalse(output.exists())

    def test_legacy_root_owned_workdir_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            recipe = "FROM scratch\nUSER 123:456\nWORKDIR /owned\n"
            dockerfile = context / "Dockerfile"
            dockerfile.write_text(recipe)
            user, instruction = parse(recipe)[1:]
            parent = cache_key("state", cache_key("scratch-base", "linux/amd64", 0), user.raw, None)
            key = cache_key("instruction", 4, parent, instruction.raw, None)
            layer = root / "legacy.tar"
            diff_id = LayerBuilder(context, RootFSIndex()).workdir_layer("/owned", layer)
            cache = LayerCache(root / "cache")
            cache.store(key, diff_id, layer)
            entry_path = cache.entries / (key + ".json")
            entry = json.loads(entry_path.read_text())
            entry["version"] = 4
            entry_path.write_text(json.dumps(entry))
            stats = {}
            output = root / "fixed.tar"
            build(dockerfile, context, None, None, "example/app:1", output,
                  cache_dir=root / "cache", cache_stats=stats)
            image, _ = self.read_image(output, root)
            with tarfile.open(image.layers[-1]) as archive:
                member = archive.getmember("owned")
                self.assertEqual((member.uid, member.gid), (123, 456))
            self.assertEqual((stats["hits"], stats["misses"]), (0, 1))

    def test_forward_hardlink_chain_matches_index_and_keeps_old_inode(self):
        for old_alias in (False, True):
            with self.subTest(old_alias=old_alias), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                first, second, third = [root / name for name in ("first.tar", "second.tar", "third.tar")]
                initial = [("original", "file", b"new")]
                if old_alias:
                    initial.append(("alias-b", "file", b"stale"))
                write_layer(first, initial)
                write_layer(second, [("alias-a", "link", "alias-b"), ("alias-b", "link", "original")])
                write_layer(third, [("original", "file", b"replacement")])
                index = RootFSIndex()
                materializer = RootFSMaterializer(root / "rootfs")
                # Windows 上实际创建文件和硬链接，仅跳过 Linux 属主/xattr 元数据调用。
                with patch.object(materializer, "_metadata"):
                    for layer in (first, second, third):
                        index.apply_layer(layer)
                        materializer.apply(layer)
                for name in ("original", "alias-a", "alias-b"):
                    self.assertEqual((materializer.root / name).read_bytes(), index.read_file(name))
                self.assertEqual((materializer.root / "alias-a").stat().st_ino,
                                 (materializer.root / "alias-b").stat().st_ino)
                self.assertNotEqual((materializer.root / "alias-a").stat().st_ino,
                                    (materializer.root / "original").stat().st_ino)

    def test_absolute_hardlink_target_matches_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "absolute.tar"
            write_layer(layer, [("original", "file", b"data"), ("alias", "link", "/original")])
            index = RootFSIndex()
            index.apply_layer(layer)
            materializer = RootFSMaterializer(root / "rootfs")
            with patch.object(materializer, "_metadata"):
                materializer.apply(layer)
            self.assertEqual((materializer.root / "alias").read_bytes(), index.read_file("alias"))

    def test_cas_base_run_materialization_stage_copy_and_cache_agree(self):
        """贯通 CAS 基础层、实际 rootfs、模拟 RUN、新层、跨阶段复制和重复构建。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.tar", root / "second.tar"
            write_layer(first, [("original", "file", b"new"), ("alias-b", "file", b"stale")])
            write_layer(second, [("alias-a", "link", "alias-b"), ("alias-b", "link", "original")])
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(path)
                                                                  for path in (first, second)]},
                      "history": [{"created_by": "first"}, {"created_by": "second"}]}
            base = root / "base.tar"
            ImageArchiveWriter().write_new(base, config, [first, second], 'example/base:1')
            cas = CASStore(root / "store")
            cas.import_docker(base, "example/base:1", "linux/amd64")
            source = {"type": "cas", "store": str(root / "store"), "reference": "example/base:1",
                      "platform": "linux/amd64", "source": "local"}
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1 AS source\nRUN generate\n"
                                  "FROM scratch\nCOPY --from=source /generated /result\n")
            calls = []

            class FakeOverlay:
                def __init__(self, rootfs, _workspace):
                    self.rootfs = rootfs

                def to_layer(self, target):
                    write_layer(target, [("generated", "file", self.content)])
                    return "sha256:" + sha256_file(target)

            class FakeExecutor:
                def __init__(self, *_args):
                    pass

                def execute(self, overlay, *_args):
                    overlay.content = (overlay.rootfs / "alias-a").read_bytes()
                    calls.append(overlay.content)

            with patch.object(RootFSMaterializer, "_metadata"), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FakeExecutor):
                for number in range(2):
                    output = root / ("cas-{}.tar".format(number))
                    stats = {}
                    build(dockerfile, context, source, None, "example/app:1", output,
                          enable_run=True, cache_dir=root / "cache", cache_stats=stats)
                    _, index = self.read_image(output, root)
                    self.assertEqual(index.read_file("result"), b"new")
                    self.assertEqual(stats["hits"], number * 2)
            self.assertEqual(calls, [b"new"])

    def test_cyclic_hardlinks_cannot_reuse_lower_layer_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.tar", root / "cycle.tar"
            write_layer(first, [("a", "file", b"old a"), ("b", "file", b"old b")])
            write_layer(second, [("a", "link", "b"), ("b", "link", "a")])
            materializer = RootFSMaterializer(root / "rootfs")
            with patch.object(materializer, "_metadata"):
                materializer.apply(first)
                with self.assertRaises(ArchiveError):
                    materializer.apply(second)


if __name__ == "__main__":
    unittest.main()
