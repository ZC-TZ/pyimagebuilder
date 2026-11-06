"""核对下载整批路径预检，以及转换时使用实际已校验的配置字节。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
from errors import BuildError
from image_writer import ImageArchiveWriter
from path_policy import host_path_aliases
from test_artifactory_publication import artifact_files, digest, layer_bytes, download, REFERENCE, PLATFORM, SOURCE
from cas_store import CASStore


class ArtifactoryInputContractTests(unittest.TestCase):
    def test_config_digest_checks_bytes_actually_returned_by_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = artifact_files(layer_bytes(b"payload"))
            for name, raw in files.items():
                (root / name).write_bytes(raw)
            entry = json.loads(files["manifest.json"])["config"]
            original = artifacts.read_image_json_file

            def wrong_snapshot(path, *args):
                result = original(path, *args)
                if Path(path).name == "sha256__" + entry["digest"][7:]:
                    value = dict(result[0], config={"Env": ["REPLACED=1"]})
                    return value, json.dumps(value).encode()
                return result

            with patch.object(artifacts, "read_image_json_file", side_effect=wrong_snapshot), \
                    patch.object(artifacts, "materialize_layer") as materialize:
                with self.assertRaisesRegex(RuntimeError, "摘要校验失败"):
                    artifacts.convert_registry_to_docker_archive(root, root / "converted", REFERENCE,
                                                                 artifacts.ProgressDisplay())
            materialize.assert_not_called()

    def test_invalid_config_descriptor_size_fails_before_layer_preparation(self):
        for size in (0, True, "123", 123.0):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                files = artifact_files(layer_bytes(b"payload"))
                manifest = json.loads(files["manifest.json"])
                config_bytes = files["sha256__" + manifest["config"]["digest"][7:]]
                manifest["config"]["size"] = float(len(config_bytes)) if type(size) is float else size
                files["manifest.json"] = json.dumps(manifest).encode()
                for name, raw in files.items():
                    (root / name).write_bytes(raw)
                with patch.object(artifacts, "materialize_layer") as materialize:
                    with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                        artifacts.convert_registry_to_docker_archive(root, root / "converted", REFERENCE,
                                                                     artifacts.ProgressDisplay())
                materialize.assert_not_called()

    def test_cas_metadata_size_failure_does_not_start_layer_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            files = artifact_files(layer_bytes(b"payload"))
            manifest = json.loads(files["manifest.json"])
            manifest["config"]["size"] = 1
            files["manifest.json"] = json.dumps(manifest).encode()
            calls = []

            def transfer(_opener, _url, name, directory, _size, _limiter, stats, _display):
                calls.append(name)
                (directory / name).write_bytes(files[name])
                stats.add(len(files[name]))

            with patch.object(artifacts, "probe_file_sizes", return_value={name: len(raw) for name, raw in files.items()}), \
                    patch.object(artifacts, "download_one", side_effect=transfer), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                    artifacts.download_and_pack(object(), "https://example.test/", list(files), root / "unused.tar",
                                                 2, REFERENCE, cas_store=store, cas_only=True)
            self.assertEqual(calls, ["manifest.json", "sha256__" + manifest["config"]["digest"][7:]])
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_pack_verification_failure_rolls_back_updated_cas_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            download(artifact_files(layer_bytes(b"old")), root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            original = artifacts.CountingReader.read
            changed = []

            def tamper(reader, *args):
                raw = original(reader, *args)
                if raw and str(reader.raw.name).endswith("layer.tar") and not changed:
                    changed.append(True)
                    return bytes([raw[0] ^ 1]) + raw[1:]
                return raw

            with patch.object(artifacts.CountingReader, "read", tamper):
                with self.assertRaises(BuildError):
                    download(artifact_files(layer_bytes(b"new")), root / "bad.tar", store, cas_only=False)
            self.assertTrue(changed)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            self.assertFalse((root / "bad.tar").exists())

    def test_valid_nested_artifact_paths_still_import_and_keep_config_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            fixture = artifact_files(layer_bytes(b"payload"))
            files = {(name if name == "manifest.json" else "some blobs/" + name): raw
                     for name, raw in fixture.items()}
            manifest = json.loads(files["manifest.json"])

            def transfer(_opener, _url, name, directory, _size, _limiter, stats, _display):
                path = directory.joinpath(*name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(files[name])
                stats.add(len(files[name]))

            with patch.object(artifacts, "probe_file_sizes", return_value={name: len(raw) for name, raw in files.items()}), \
                    patch.object(artifacts, "download_one", side_effect=transfer), contextlib.redirect_stdout(io.StringIO()):
                artifacts.download_and_pack(object(), "https://example.test/", list(files), root / "unused.tar",
                                             2, REFERENCE, cas_store=store, cas_only=True)
            image = store.open_base(REFERENCE, PLATFORM, SOURCE)
            expected = fixture["sha256__" + manifest["config"]["digest"][7:]]
            self.assertEqual(image.original_config_bytes(), expected)
            self.assertEqual(image.config_digest, manifest["config"]["digest"])

    def test_standalone_download_rejects_layer_changed_while_packing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = artifact_files(layer_bytes(b"payload"))
            output = root / "corrupt.tar"
            original = artifacts.CountingReader.read
            changed = []

            def alter_pack_bytes(reader, *args):
                raw = original(reader, *args)
                if raw and str(reader.raw.name).endswith("layer.tar") and not changed:
                    changed.append(True)
                    return bytes([raw[0] ^ 1]) + raw[1:]
                return raw

            with patch.object(artifacts.CountingReader, "read", alter_pack_bytes):
                with self.assertRaises(BuildError):
                    download(files, output, None, cas_only=False)
            self.assertTrue(changed)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob("*.part")))

    def test_conversion_writes_verified_config_snapshot_if_source_changes_after_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_root, converted = root / "raw", root / "converted"
            raw_root.mkdir()
            files = artifact_files(layer_bytes(b"payload"))
            for name, raw in files.items():
                (raw_root / name).write_bytes(raw)
            entry = json.loads(files["manifest.json"])["config"]
            config_name = "sha256__" + entry["digest"][7:]
            expected = files[config_name]
            changed = json.loads(expected)
            changed["config"] = {"Env": ["CHANGED=1"]}
            different = json.dumps(changed).encode()
            original_read = artifacts.read_image_json_file
            swaps = []

            def read_then_replace(path, *args):
                result = original_read(path, *args)
                if Path(path).name == config_name:
                    swaps.append(True)
                    Path(path).write_bytes(different)
                return result

            with patch.object(artifacts, "read_image_json_file", side_effect=read_then_replace), \
                    patch.object(artifacts, "ProgressDisplay") as display:
                artifacts.convert_registry_to_docker_archive(raw_root, converted, REFERENCE, display())
            self.assertTrue(swaps)
            output_config = converted / (entry["digest"][7:] + ".json")
            self.assertEqual(output_config.read_bytes(), expected)
            self.assertEqual(digest(output_config.read_bytes()), entry["digest"])
            output = root / "image.tar"
            artifacts.pack_files(converted, output, display())
            ImageArchiveWriter().verify(output, REFERENCE)

    def test_batch_conflicts_fail_before_probe_download_or_output_directory_creation(self):
        batches = ((["blob", "blob"], False), (["a/b", "a//b"], False),
                   (["a", "a/b"], False), (["a/b/c", "a/b"], False),
                   (["manifest.json", "MANIFEST.JSON"], True),
                   (["Dir/a", "dir/b"], True))
        for names, windows in batches:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "exports" / "image.tar"
                with patch.object(artifacts, "probe_file_sizes", side_effect=AssertionError("probe started")) as probe, \
                        patch.object(artifacts, "download_one", side_effect=AssertionError("download started")) as transfer, \
                        patch.object(artifacts, "host_path_aliases", create=True,
                                     side_effect=lambda values: host_path_aliases(values, windows=windows)):
                    with self.assertRaises(BuildError):
                        artifacts.download_and_pack(object(), "https://example.test/", names, output, 2, REFERENCE)
                probe.assert_not_called()
                transfer.assert_not_called()
                self.assertFalse(output.parent.exists())


if __name__ == "__main__":
    unittest.main()
