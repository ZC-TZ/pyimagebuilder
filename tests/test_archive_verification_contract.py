"""核对归档发布门禁，以及校验后重开 OCI 输入时的身份绑定。"""

import copy
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas_store import CASStore
from errors import ArchiveError
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from tests.test_phase1 import add_bytes, layer_file


TAG = "example/verified:1"


def empty_config():
    return {"architecture": "amd64", "os": "linux", "config": {},
            "rootfs": {"type": "layers", "diff_ids": []}, "history": []}


def docker_archive(path, config, layer_entries=(), duplicate_manifest=False):
    """直接制作异常输入，避免 writer 在测试到校验器之前替我们过滤问题。"""
    raw = json.dumps(config).encode()
    name = hashlib.sha256(raw).hexdigest() + ".json"
    manifest = json.dumps([{"Config": name, "RepoTags": [TAG],
                            "Layers": [entry[0] for entry in layer_entries]}]).encode()
    with tarfile.open(path, "w") as archive:
        add_bytes(archive, "manifest.json", manifest)
        add_bytes(archive, name, raw)
        for layer_name, content, link in layer_entries:
            if link:
                info = tarfile.TarInfo(layer_name)
                info.type = tarfile.LNKTYPE
                info.linkname = "payload.tar"
                add_bytes(archive, "payload.tar", content)
                archive.addfile(info)
            else:
                add_bytes(archive, layer_name, content)
        if duplicate_manifest:
            add_bytes(archive, "./manifest.json", manifest)


class ArchiveVerificationContractTests(unittest.TestCase):
    def test_docker_verifier_rejects_duplicate_normalized_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            docker_archive(path, empty_config(), duplicate_manifest=True)
            with self.assertRaises(ArchiveError):
                ImageArchiveWriter().verify(path, TAG)

    def test_docker_verifier_rejects_non_file_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "layer.tar"
            layer_file(layer, {"file": b"data"})
            raw = layer.read_bytes()
            config = empty_config()
            config["rootfs"]["diff_ids"] = ["sha256:" + hashlib.sha256(raw).hexdigest()]
            config["history"] = [{}]
            path = root / "image.tar"
            docker_archive(path, config, [("layer.tar", raw, True)])
            with self.assertRaises(ArchiveError):
                ImageArchiveWriter().verify(path, TAG)

    def test_docker_verifier_rejects_invalid_rootfs_and_non_json_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            for rootfs in ([], None, "layers"):
                with self.subTest(rootfs=rootfs):
                    config = empty_config()
                    config["rootfs"] = rootfs
                    docker_archive(path, config)
                    with self.assertRaises(ArchiveError):
                        ImageArchiveWriter().verify(path, TAG)
            config = empty_config()
            config["invalid"] = float("nan")
            docker_archive(path, config)
            with self.assertRaises(ArchiveError):
                ImageArchiveWriter().verify(path, TAG)

    def test_docker_verifier_bounds_both_json_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            config = empty_config()
            config["large"] = "x" * 512
            docker_archive(path, config)
            with tarfile.open(path) as archive:
                manifest_size = archive.getmember("manifest.json").size
            for limit in (16, manifest_size):
                with self.subTest(limit=limit), patch("image_writer.MAX_JSON", limit, create=True):
                    with self.assertRaises(ArchiveError):
                        ImageArchiveWriter().verify(path, TAG)

    def test_oci_import_rejects_coherent_archive_replacement(self):
        # 两份输入均合法：仅重新检查替换后的 descriptor 无法察觉身份变化。
        for replacement_tag in (TAG, "example/other:1"):
            for replace in (False, True):
                with self.subTest(tag=replacement_tag, replace=replace), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, replacement = root / "source.tar", root / "replacement.tar"
                    config = empty_config()
                    OCIImageWriter().write_new(source, config, [], TAG)
                    changed = copy.deepcopy(config)
                    changed["config"]["Env"] = ["REPLACED=1"]
                    OCIImageWriter().write_new(replacement, changed, [], replacement_tag)
                    OCIImageWriter().verify(replacement, replacement_tag)
                    store = CASStore(root / "store")
                    previous = store.import_oci(source, TAG, "linux/amd64") if replace else None
                    verify = OCIImageWriter.verify

                    def replaced_after_verify(instance, path, tag):
                        result = verify(instance, path, tag)
                        shutil.copyfile(replacement, path)
                        return result

                    with patch.object(OCIImageWriter, "verify", replaced_after_verify):
                        with self.assertRaises(ArchiveError):
                            store.import_oci(source, TAG, "linux/amd64", replace=replace)
                    if replace:
                        self.assertEqual(store.resolve(TAG), previous)
                    else:
                        self.assertFalse(store.has_ref(TAG, "linux/amd64", "local"))

    def test_verified_oci_snapshot_keeps_exact_json_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            OCIImageWriter().write_new(path, empty_config(), [], TAG)
            snapshot = OCIImageWriter().verify(path, TAG)
            with tarfile.open(path) as archive:
                expected = {member.name: archive.extractfile(member).read() for member in archive}
            self.assertEqual(snapshot, expected)
            self.assertTrue(all(isinstance(raw, bytes) for raw in snapshot.values()))

    def test_both_verifiers_reject_non_tar_layer_with_matching_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "layer.tar"
            info = tarfile.TarInfo("truncated")
            info.size = 20000
            for raw in (b"not a tar archive", info.tobuf() + b"x" * 512):
                layer.write_bytes(raw)
                config = empty_config()
                config["rootfs"]["diff_ids"] = ["sha256:" + hashlib.sha256(raw).hexdigest()]
                config["history"] = [{}]
                for writer in (ImageArchiveWriter(), OCIImageWriter()):
                    with self.subTest(writer=type(writer).__name__, size=len(raw)):
                        path = root / "image.tar"
                        writer.write_new(path, config, [layer], TAG)
                        with self.assertRaises(ArchiveError):
                            writer.verify(path, TAG)

    def test_both_verifiers_accept_sparse_layer_physical_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "sparse.tar"
            with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo("sparse")
                info.size = 1
                info.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.size": "1048576"}
                archive.addfile(info, io.BytesIO(b"x"))
            config = empty_config()
            config["rootfs"]["diff_ids"] = ["sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()]
            config["history"] = [{}]
            for writer in (ImageArchiveWriter(), OCIImageWriter()):
                with self.subTest(writer=type(writer).__name__):
                    path = root / "image.tar"
                    writer.write_new(path, config, [layer], TAG)
                    writer.verify(path, TAG)


if __name__ == "__main__":
    unittest.main()
