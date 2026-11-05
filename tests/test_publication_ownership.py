"""验证回滚不能按路径误删替换文件，并覆盖无硬链接复制和跨盘暂存。"""

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import builder
import file_publish
from cas_store import CASStore
from test_export_staging import other_device
from test_ref_transactions import make_image, REFERENCE, PLATFORM


class PublicationOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.write_bytes(b"our output")
        self.target = self.root / "output.tar"
        self.foreign = self.root / "foreign"
        self.foreign.write_bytes(b"other task output")

    def test_transaction_preserves_replaced_output_and_removes_other_owned_file(self):
        second = self.root / "second.tar"
        with self.assertRaisesRegex(RuntimeError, "later failure"):
            with file_publish.publication_transaction() as published:
                for target in (self.target, second):
                    file_publish.publish_new_file(self.source, target)
                published.extend([self.target, second])
                self.foreign.replace(self.target)
                raise RuntimeError("later failure")
        self.assertEqual(self.target.read_bytes(), b"other task output")
        self.assertFalse(second.exists())

    def test_replacement_before_registration_is_not_claimed_as_ours(self):
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction() as published:
                file_publish.publish_new_file(self.source, self.target)
                self.foreign.replace(self.target)
                published.append(self.target)
                raise RuntimeError("rollback")
        self.assertEqual(self.target.read_bytes(), b"other task output")

    def test_outer_transaction_keeps_identity_captured_by_nested_publication(self):
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction() as outer:
                with file_publish.publication_transaction() as inner:
                    file_publish.publish_new_file(self.source, self.target)
                    inner.append(self.target)
                self.foreign.replace(self.target)
                outer.append(self.target)
                raise RuntimeError("outer rollback")
        self.assertEqual(self.target.read_bytes(), b"other task output")
        self.assertEqual(file_publish._PUBLICATIONS.get(), ())

    def test_publication_registration_failure_removes_only_created_output(self):
        original = os.link
        for linked in (True, False):
            with self.subTest(hardlinks=linked):
                def link(*args, **kwargs):
                    if not linked:
                        raise OSError(errno.EPERM, "no links")
                    return original(*args, **kwargs)

                with patch.object(file_publish.os, "link", side_effect=link), \
                        patch.object(file_publish, "_observe_publication", side_effect=OSError("receipt failed")):
                    with self.assertRaisesRegex(OSError, "receipt failed"):
                        file_publish.publish_new_file(self.source, self.target)
                self.assertFalse(self.target.exists())
                self.assertFalse(list(self.root.glob("*.part")))

    def test_self_extension_does_not_lose_ownership_or_loop(self):
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction() as published:
                file_publish.publish_new_file(self.source, self.target)
                published.append(self.target)
                published += published
                self.assertEqual(len(published), 2)
                raise RuntimeError("rollback")
        self.assertFalse(self.target.exists())

    def test_identity_survives_writes_and_private_hardlink_removal(self):
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction() as published:
                with open(self.target, "xb") as output:
                    published.append(self.target)
                    output.write(b"payload written after registration")
                os.link(self.target, self.root / "private-link")
                (self.root / "private-link").unlink()
                raise RuntimeError("rollback")
        self.assertFalse(self.target.exists())

    def test_missing_output_is_not_an_error_during_rollback(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with file_publish.publication_transaction() as published:
                self.target.write_bytes(b"owned")
                published.append(self.target)
                self.target.unlink()
                raise RuntimeError("rollback")

    def test_directory_replacement_is_left_untouched(self):
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction() as published:
                file_publish.publish_new_file(self.source, self.target)
                published.append(self.target)
                self.target.unlink()
                self.target.mkdir()
                (self.target / "keep").write_bytes(b"foreign directory")
                raise RuntimeError("rollback")
        self.assertEqual((self.target / "keep").read_bytes(), b"foreign directory")

    def test_cas_ref_restored_while_replacement_output_is_preserved(self):
        store = CASStore(self.root / "store")
        old, new = make_image(self.root, "old"), make_image(self.root, "new")
        store.import_docker(old, REFERENCE, PLATFORM, "local")
        previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
        with self.assertRaises(RuntimeError):
            with file_publish.publication_transaction(store, REFERENCE, PLATFORM, "local") as published:
                file_publish.publish_new_file(self.source, self.target)
                published.append(self.target)
                store.import_docker(new, REFERENCE, PLATFORM, "local", replace=True)
                self.foreign.replace(self.target)
                raise RuntimeError("rollback")
        self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
        self.assertEqual(self.target.read_bytes(), b"other task output")

    def test_copy_close_failure_keeps_replacement_after_owned_handle_closed(self):
        real_open = open
        target, foreign = self.target, self.foreign

        class ClosedThenReplaced:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                return self

            def __exit__(self, *exception):
                self.stream.__exit__(*exception)
                foreign.replace(target)
                raise OSError("final close failed")

        def opened(path, mode, *args, **kwargs):
            result = real_open(path, mode, *args, **kwargs)
            return ClosedThenReplaced(result) if Path(path) == target and mode == "xb" else result

        with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no links")), \
                patch.object(file_publish, "open", side_effect=opened, create=True):
            with self.assertRaisesRegex(OSError, "final close failed"):
                file_publish.publish_new_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"other task output")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_pending_cleanup_failure_keeps_replaced_published_file(self):
        link, unlink = os.link, file_publish.unlink_missing

        def cross_device(source, target):
            if Path(source) == self.source:
                raise OSError(errno.EXDEV, "cross device")
            return link(source, target)

        def failed_cleanup(path):
            if Path(path).name.startswith("publish-"):
                self.foreign.replace(self.target)
                raise OSError("pending cleanup failed")
            return unlink(path)

        with patch.object(file_publish.os, "link", side_effect=cross_device), \
                patch.object(file_publish, "unlink_missing", side_effect=failed_cleanup):
            with self.assertRaisesRegex(OSError, "pending cleanup failed"):
                file_publish.publish_new_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"other task output")

    def test_cross_device_build_cleanup_preserves_replacement_and_rolls_back_sidecars(self):
        context = self.root / "context"
        context.mkdir()
        recipe = context / "Dockerfile"
        recipe.write_text("FROM scratch\nENV APP=example\n")
        outputs = self.root / "outputs"
        outputs.mkdir()
        target = outputs / "image.tar"
        real_directory = tempfile.TemporaryDirectory
        foreign = self.foreign

        class ReplacedDuringCleanup:
            def __init__(self, *args, **kwargs):
                self.directory = real_directory(*args, **kwargs)
                self.staging = kwargs.get("prefix") == "pyimagebuilder-export-"

            def __enter__(self):
                return self.directory.__enter__()

            def __exit__(self, kind, *exception):
                self.directory.__exit__(kind, *exception)
                if kind is None and self.staging:
                    foreign.replace(target)
                    raise OSError("staging cleanup failed")

        with other_device(outputs), patch.object(builder.tempfile, "TemporaryDirectory", ReplacedDuringCleanup):
            with self.assertRaisesRegex(OSError, "staging cleanup failed"):
                builder.build(recipe, context, None, None, "app:1", target, image_format="both", attest=True)
        self.assertEqual(target.read_bytes(), b"other task output")
        self.assertEqual(list(outputs.iterdir()), [target])

    def test_unavailable_file_identity_does_not_authorize_deletion(self):
        with patch.object(file_publish, "_node_identity", return_value=None):
            with self.assertRaisesRegex(OSError, "stable file identity"):
                with file_publish.publication_transaction() as published:
                    self.target.write_bytes(b"cannot identify")
                    published.append(self.target)
                    raise RuntimeError("rollback")
        self.assertEqual(self.target.read_bytes(), b"cannot identify")


if __name__ == "__main__":
    unittest.main()
