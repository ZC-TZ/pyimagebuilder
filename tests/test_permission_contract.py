"""权限专项：在归档层验证有效元数据，并模拟 RUN 的 Linux 系统调用顺序。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance
import executor
import optimizer
import run_mounts
from dockerfile_parser import parse
from builder import build
from cas_store import CASStore
from errors import BuildError
from image_reader import sha256_file
from image_writer import ImageArchiveWriter
from layer import LayerBuilder
from oci_writer import OCIImageWriter
from overlay import OverlayManager
from permissions import validate_metadata
from rootfs import RootFSIndex
from rootfs_archive import flatten_image, merge_rootfs, open_image
from rootfs_materializer import RootFSMaterializer


def directory(name, attrs=None, mode=0o2750):
    member = tarfile.TarInfo(name)
    member.type, member.mode, member.uid, member.gid = tarfile.DIRTYPE, mode, 10, 20
    member.pax_headers = {"SCHILY.xattr." + key: value for key, value in (attrs or {}).items()}
    return member


def write_layer(path, members):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for member in members:
            archive.addfile(member, io.BytesIO(b"x" * member.size) if member.isfile() else None)
    return path


class PermissionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lower = write_layer(self.root / "lower.tar", [
            directory(".", {"user.keep": "root"}), directory("app", {"user.keep": "directory"})])
        self.upper = write_layer(self.root / "upper.tar", [directory(".", mode=0o1755),
                                                                 directory("app", mode=0o700)])

    def index(self, *layers):
        result = RootFSIndex()
        for layer in layers:
            result.apply_layer(layer)
        return result

    def assert_attrs(self, index):
        self.assertEqual(index.root_metadata.pax_headers["SCHILY.xattr.user.keep"], "root")
        self.assertEqual(index.entries["app"].metadata.pax_headers["SCHILY.xattr.user.keep"], "directory")

    def image(self, format="docker"):
        layers = [self.lower, self.upper]
        config = {"os": "linux", "architecture": "amd64", "config": {"User": "10:20"},
                  "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in layers]},
                  "history": [{"created_by": "lower"}, {"created_by": "upper"}]}
        output = self.root / ("base-" + format + ".tar")
        writer = ImageArchiveWriter() if format == "docker" else OCIImageWriter()
        writer.write_new(output, config, layers, "example/perms:1")
        return output

    def test_directory_and_root_updates_keep_undeclared_xattrs(self):
        index = self.index(self.lower, self.upper)
        self.assert_attrs(index)
        self.assertEqual(index.root_metadata.mode, 0o1755)
        self.assertEqual(index.entries["app"].metadata.mode, 0o700)

    def test_directory_updates_replace_only_declared_xattrs(self):
        update = write_layer(self.root / "update.tar", [directory("app", {"user.keep": "new", "user.add": "added"})])
        attrs = self.index(self.lower, update).entries["app"].metadata.pax_headers
        self.assertEqual(attrs, {"SCHILY.xattr.user.keep": "new", "SCHILY.xattr.user.add": "added"})

    def test_opaque_keeps_directory_metadata(self):
        child = tarfile.TarInfo("app/file")
        child.size = 1
        middle = write_layer(self.root / "middle.tar", [child])
        for name in ("app/.wh..wh..opq", ".wh..wh..opq"):
            with self.subTest(name=name):
                opaque = write_layer(self.root / "opaque.tar", [tarfile.TarInfo(name)])
                index = self.index(self.lower, middle, opaque)
                self.assertNotIn("app/file", index.entries)
                self.assertEqual(index.root_metadata.pax_headers["SCHILY.xattr.user.keep"], "root")
                if name.startswith("app/"):
                    self.assertEqual(index.entries["app"].metadata.pax_headers["SCHILY.xattr.user.keep"], "directory")

    def test_whiteout_recreation_does_not_inherit_deleted_xattrs(self):
        replacement = write_layer(self.root / "new.tar", [tarfile.TarInfo(".wh.app"), directory("app")])
        self.assertEqual(self.index(self.lower, replacement).entries["app"].metadata.pax_headers, {})

    def test_file_replacement_does_not_inherit_previous_xattrs(self):
        first, second = tarfile.TarInfo("file"), tarfile.TarInfo("file")
        first.pax_headers = {"SCHILY.xattr.user.keep": "old"}
        first.size = second.size = 1
        a = write_layer(self.root / "a.tar", [first])
        b = write_layer(self.root / "b.tar", [second])
        self.assertEqual(self.index(a, b).entries["file"].metadata.pax_headers, {})

    def test_rootfs_export_keeps_effective_directory_attributes(self):
        output = self.root / "rootfs.tar"
        merge_rootfs([self.lower, self.upper], output)
        self.assert_attrs(self.index(output))

    def test_stage_copy_keeps_effective_directory_attributes(self):
        source = self.index(self.lower, self.upper)
        transfer = parse("FROM scratch\nCOPY --from=source / /out/\n")[1].value
        output = self.root / "copy.tar"
        LayerBuilder(self.root, RootFSIndex()).transfer_from_rootfs(source, transfer, "/", output)
        with tarfile.open(output) as archive:
            member = archive.getmember("out/app")
            self.assertEqual((member.mode, member.uid, member.gid), (0o700, 10, 20))
            self.assertEqual(member.pax_headers["SCHILY.xattr.user.keep"], "directory")

    def test_flatten_both_formats_preserves_attributes_and_user(self):
        source = self.image()
        for format in ("docker", "oci"):
            with self.subTest(format=format):
                output = self.root / ("flat-" + format + ".tar")
                flatten_image(source, output, reference="example/flat:1", format=format)
                with open_image(output, self.root / ("read-" + format), tag="example/flat:1") as (image, _):
                    self.assertEqual(image.config["config"]["User"], "10:20")
                    self.assert_attrs(self.index(*image.layers))

    def test_optimizer_cannot_prune_layer_that_supplies_inherited_xattrs(self):
        for format in ("docker", "oci"):
            with self.subTest(format=format):
                source = self.image(format)
                output = self.root / ("optimized-" + format + ".tar")
                result = optimizer.optimize_image(source, output)
                self.assertEqual(result["removed_layer_numbers"], [])
                with open_image(output, self.root / ("optimized-read-" + format), tag="example/perms:1") as (image, _):
                    self.assert_attrs(self.index(*image.layers))

    def test_independent_conformance_oracle_keeps_directory_xattrs(self):
        tree = conformance._layer_tree([self.lower, self.upper])
        self.assertEqual(tree[""]["xattrs"], {"user.keep": "root"})
        self.assertEqual(tree["app"]["xattrs"], {"user.keep": "directory"})

    def test_cas_build_and_cache_hit_keep_effective_directory_permissions(self):
        store = self.root / "store"
        CASStore(store).import_docker(self.image(), "example/perms:1", "linux/amd64")
        source = {"type": "cas", "store": str(store), "reference": "example/perms:1",
                  "platform": "linux/amd64", "source": "local"}
        context = self.root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM example/perms:1 AS source\nFROM scratch\n"
                              "COPY --from=source / /out/\n", encoding="utf-8")
        identities = []
        for number in range(2):
            output = self.root / ("build-{}.tar".format(number))
            stats = {}
            build(dockerfile, context, source, None, "example/app:1", output,
                  cache_dir=self.root / "cache", cache_stats=stats)
            self.assertEqual(stats["hits"], number)
            with open_image(output, self.root / ("build-read-{}".format(number)), tag="example/app:1") as (image, _):
                identities.append(image.config_digest)
                member = self.index(*image.layers).entries["out/app"].metadata
                self.assertEqual((member.mode, member.uid, member.gid), (0o700, 10, 20))
                self.assertEqual(member.pax_headers["SCHILY.xattr.user.keep"], "directory")
        self.assertEqual(identities[0], identities[1])

    def test_overlay_root_restores_mode_after_xattrs(self):
        lower = self.root / "lower"
        lower.mkdir()
        events = []
        with patch("overlay.os.chown", side_effect=lambda *a, **k: events.append("chown"), create=True), \
                patch("overlay._file_xattrs", return_value={"user.example": b"value"}), \
                patch("overlay.os.setxattr", side_effect=lambda *a, **k: events.append("xattr"), create=True), \
                patch.object(Path, "chmod", autospec=True, side_effect=lambda *a: events.append("chmod")):
            OverlayManager(lower, self.root / "overlay")
        self.assertEqual(events, ["chown", "xattr", "chmod"])

    def test_metadata_syscall_order_restores_mode_after_xattrs(self):
        member = directory("app", {"user.example": "value"}, mode=0o4750)
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        for symlink in (False, True):
            events = []
            with self.subTest(symlink=symlink), \
                    patch("rootfs_materializer.os.chown", side_effect=lambda *a, **k: events.append("chown"), create=True), \
                    patch("rootfs_materializer.os.setxattr", side_effect=lambda *a, **k: events.append("xattr"), create=True), \
                    patch("rootfs_materializer.os.chmod", side_effect=lambda *a, **k: events.append("chmod")), \
                    patch("rootfs_materializer.os.utime", side_effect=lambda *a, **k: events.append("utime")):
                materializer._metadata(self.root / "app", member, symlink=symlink)
            self.assertEqual(events, ["chown", "xattr"] + ([] if symlink else ["chmod"]) + ["utime"])

    def test_invalid_metadata_is_rejected(self):
        for field, value in (("uid", -1), ("gid", -1), ("mode", -1), ("mode", 0o10000),
                             ("mode", True), ("mode", "755")):
            with self.subTest(field=field, value=value):
                member = directory("app")
                setattr(member, field, value)
                with self.assertRaises(BuildError):
                    validate_metadata(member)

    def test_invalid_metadata_cannot_apply_an_earlier_whiteout(self):
        invalid = directory("bad")
        invalid.uid = -1
        bad = write_layer(self.root / "bad.tar", [tarfile.TarInfo(".wh.app"), invalid])
        index = self.index(self.lower)
        with self.assertRaises(BuildError):
            index.apply_layer(bad)
        self.assertIn("app", index.entries)
        with patch.object(RootFSMaterializer, "_metadata"):
            materializer = RootFSMaterializer(self.root / "fs")
            (materializer.root / "app").mkdir()
            with self.assertRaises(BuildError):
                materializer.apply(bad)
        self.assertTrue((materializer.root / "app").is_dir())

    def test_add_rejects_permission_bits_that_tarfile_would_silently_mask(self):
        # 手工写头：TarInfo.tobuf 会预先按 07777 截断，不能用它生成这类坏输入。
        header = bytearray(tarfile.TarInfo("file").tobuf())
        header[100:108] = b"0010000\0"
        header[148:156] = b"        "
        header[148:156] = ("%06o\0 " % sum(header)).encode("ascii")
        source = self.root / "bad-mode.tar"
        source.write_bytes(header + b"\0" * 1024)
        transfer = parse("FROM scratch\nADD bad-mode.tar /app/\n")[1].value
        with self.assertRaisesRegex(BuildError, "permission mode"):
            LayerBuilder(self.root, RootFSIndex()).transfer_layer(
                transfer, "/", self.root / "bad-add.tar", add=True)

    def test_binary_xattrs_transfer_but_fail_before_run_materialization(self):
        member = directory("app", {"security.capability": "\udc80\udcff"})
        binary = write_layer(self.root / "binary.tar", [member])
        output = self.root / "binary-rootfs.tar"
        merge_rootfs([binary], output)
        self.assertEqual(self.index(output).entries["app"].metadata.pax_headers["SCHILY.xattr.security.capability"],
                         member.pax_headers["SCHILY.xattr.security.capability"])
        with patch.object(RootFSMaterializer, "_metadata"):
            materializer = RootFSMaterializer(self.root / "binary-fs")
        with patch("rootfs_materializer.os.chown", create=True) as chown:
            with self.assertRaisesRegex(BuildError, "Cannot preserve xattr security.capability"):
                materializer.apply(binary)
        chown.assert_not_called()
        self.assertFalse((materializer.root / "app").exists())

    def test_new_runtime_directory_restores_owner_and_mode(self):
        target = self.root / "mount-parent"
        with patch.object(executor.os, "chown", create=True) as chown, \
                patch.object(Path, "chmod", autospec=True) as chmod:
            executor._runtime_directory(target)
        chown.assert_called_once_with(target, 0, 0)
        chmod.assert_called_once_with(target, 0o755)

    def test_existing_runtime_directory_keeps_declared_permissions(self):
        with patch.object(executor.os, "chown", create=True) as chown, \
                patch.object(Path, "chmod", autospec=True) as chmod:
            executor._runtime_directory(self.root)
        chown.assert_not_called()
        chmod.assert_not_called()

    def test_device_permissions_are_restored_after_mknod(self):
        events = []
        with patch.object(executor.os, "mknod", side_effect=lambda *a: events.append("mknod"), create=True), \
                patch.object(executor.os, "makedev", return_value=123, create=True), \
                patch.object(executor.os, "chown", side_effect=lambda *a: events.append("chown"), create=True), \
                patch.object(executor.os, "chmod", side_effect=lambda *a: events.append(("chmod", a[1]))):
            executor._device(self.root / "null", 0o666, 1, 3)
        self.assertEqual(events, ["mknod", "chown", ("chmod", 0o666)])

    def test_new_cache_mount_source_has_deterministic_permissions(self):
        cache = self.root / "mount-cache"
        options = [{"type": "cache", "target": "/cache"}]
        with patch.object(run_mounts.os, "geteuid", return_value=1001, create=True), \
                patch.object(run_mounts.os, "getegid", return_value=1002, create=True), \
                patch.object(run_mounts.os, "chown", create=True) as chown, \
                patch.object(Path, "chmod", autospec=True) as chmod:
            first = run_mounts.prepare(options, {}, cache, self.root, "linux/amd64")
            chown.assert_called_once_with(first[0][1], 1001, 1002)
            chmod.assert_called_once_with(first[0][1], 0o755)
            chown.reset_mock()
            chmod.reset_mock()
            second = run_mounts.prepare(options, {}, cache, self.root, "linux/amd64")
            self.assertEqual(first, second)
            chown.assert_not_called()
            chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
