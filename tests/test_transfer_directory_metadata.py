"""验证文件传输自动创建目录时的权限、属主以及缓存重建行为。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from errors import BuildError
from image_reader import ImageArchiveReader
from layer import LayerBuilder
from rootfs import RootFSIndex


class TransferDirectoryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = self.root / "ctx"
        self.context.mkdir()
        (self.context / "file").write_bytes(b"payload")
        (self.context / "tree").mkdir()
        (self.context / "tree" / "child").write_bytes(b"child")

    def _build(self, script, filename="image.tar", **kwargs):
        dockerfile = self.context / "Dockerfile"
        dockerfile.write_text(script, encoding="utf-8")
        output = self.root / filename
        build(dockerfile, self.context, None, None, "demo:1", output, **kwargs)
        image = ImageArchiveReader(output, self.root / (filename + "-unpacked")).read("demo:1")
        index = RootFSIndex()
        for layer in image.layers:
            index.apply_layer(layer)
        return index

    def _metadata(self, index, path):
        entry = index.entries[path.strip("/")]
        with tarfile.open(entry.layer, "r:") as archive:
            info = archive.getmember(entry.member)
        return info.uid, info.gid, info.mode

    def _assert_new_directories(self, index):
        for path in ("/app", "/app/private"):
            self.assertEqual(self._metadata(index, path), (1001, 2001, 0o750), path)

    def test_local_file_creates_owned_parents(self):
        index = self._build("FROM scratch\nCOPY --chown=1001:2001 --chmod=0750 file /app/private/file\n")
        self._assert_new_directories(index)
        self.assertEqual(index.read_file("/app/private/file"), b"payload")

    def test_chown_alone_keeps_default_directory_mode(self):
        index = self._build("FROM scratch\nUSER 3001:4001\nCOPY --chown=1001:2001 file /app/private/file\n")
        self.assertEqual(self._metadata(index, "/app/private"), (1001, 2001, 0o755))

    def test_chmod_alone_keeps_default_directory_owner(self):
        index = self._build("FROM scratch\nUSER 3001:4001\nCOPY --chmod=0750 file /app/private/file\n")
        self.assertEqual(self._metadata(index, "/app/private"), (0, 0, 0o750))

    def test_directory_copy_creates_owned_destination(self):
        index = self._build("FROM scratch\nCOPY --chown=1001:2001 --chmod=0750 tree /app/private/\n")
        self._assert_new_directories(index)
        self.assertEqual(index.read_file("/app/private/child"), b"child")

    def test_heredoc_creates_owned_parents(self):
        index = self._build("FROM scratch\nCOPY --chown=1001:2001 --chmod=0750 <<EOF /app/private/file\nhello\nEOF\n")
        self._assert_new_directories(index)
        self.assertEqual(index.read_file("/app/private/file"), b"hello\n")

    def test_remote_add_creates_owned_parents(self):
        def fetch(url, target, checksum, reporter):
            Path(target).write_bytes(b"remote")
            return "remote.txt", "unused"

        with patch("remote_add.fetch", side_effect=fetch):
            index = self._build("FROM scratch\nADD --chown=1001:2001 --chmod=0750 https://example.test/file /app/private/\n")
        self._assert_new_directories(index)
        self.assertEqual(index.read_file("/app/private/remote.txt"), b"remote")

    def test_archive_add_creates_owned_parents(self):
        with tarfile.open(self.context / "input.tar", "w") as archive:
            info = tarfile.TarInfo("nested/file")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"tar"))
        index = self._build("FROM scratch\nADD --chown=1001:2001 --chmod=0750 input.tar /app/private/\n")
        self._assert_new_directories(index)
        self.assertEqual(self._metadata(index, "/app/private/nested"), (1001, 2001, 0o750))
        self.assertEqual(index.read_file("/app/private/nested/file"), b"tar")

    def test_stage_copy_and_linked_copy_create_owned_parents(self):
        for source in ("/src/file", "/src/"):
            with self.subTest(source=source):
                index = self._build(
                    "FROM scratch AS base\nCOPY file /src/file\nFROM scratch\n"
                    "COPY --from=base --link --chown=1001:2001 --chmod=0750 {} /app/private/\n".format(source),
                    filename="stage-{}.tar".format("dir" if source.endswith("/") else "file"))
                self._assert_new_directories(index)
                self.assertEqual(index.read_file("/app/private/file"), b"payload")

    def test_existing_parent_metadata_is_not_rewritten(self):
        index = self._build("FROM scratch\nUSER 3001:4001\nWORKDIR /app\n"
                            "COPY --chown=1001:2001 --chmod=0750 file /app/private/file\n")
        self.assertEqual(self._metadata(index, "/app"), (3001, 4001, 0o755))
        self.assertEqual(self._metadata(index, "/app/private"), (1001, 2001, 0o750))

    def test_chown_rejects_out_of_range_numeric_ids(self):
        for owner in ("2147483648", "1001:2147483648"):
            with self.subTest(owner=owner), self.assertRaises(BuildError):
                self._build("FROM scratch\nCOPY --chown={} file /file\n".format(owner))
            self.assertFalse((self.root / "image.tar").exists())

    def test_chown_rejects_out_of_range_image_account_ids(self):
        for uid, gid in ((2147483648, 1001), (1001, 2147483648)):
            (self.context / "passwd").write_text("app:x:{}:{}::/:/bin/sh\n".format(uid, gid), encoding="utf-8")
            with self.subTest(uid=uid, gid=gid), self.assertRaises(BuildError):
                self._build("FROM scratch\nCOPY passwd /etc/passwd\nCOPY --chown=app file /file\n")
            self.assertFalse((self.root / "image.tar").exists())

    def test_old_cache_entries_are_rebuilt_then_new_entries_hit(self):
        script = "FROM scratch\nCOPY --chown=1001:2001 --chmod=0750 file /app/private/file\n"
        cache = self.root / "cache"
        ensure = LayerBuilder._ensure_directories

        def legacy_directories(builder, archive, destination, added, ownership=None, chmod=None):
            # 重现旧构建器遗漏传输选项的目录层，确保升级后确实修正产物。
            return ensure(builder, archive, destination, added)

        with patch("builder.CACHE_VERSION", 8), patch("cache.CACHE_VERSION", 8), \
                patch.object(LayerBuilder, "_ensure_directories", legacy_directories):
            old = self._build(script, "old.tar", cache_dir=cache)
        self.assertEqual(self._metadata(old, "/app/private"), (0, 0, 0o755))
        rebuilt = {}
        index = self._build(script, "rebuilt.tar", cache_dir=cache, cache_stats=rebuilt)
        self.assertEqual(rebuilt["misses"], 1)
        self._assert_new_directories(index)
        reused = {}
        index = self._build(script, "reused.tar", cache_dir=cache, cache_stats=reused)
        self.assertEqual(reused["hits"], 1)
        self._assert_new_directories(index)


if __name__ == "__main__":
    unittest.main()
