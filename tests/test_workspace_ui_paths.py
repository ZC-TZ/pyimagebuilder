"""验证目录分页、输出格式选择和产物操作导航的只读边界。"""

import base64
import tempfile
import unittest
from pathlib import Path

from errors import BuildError
from web_delivery_ui import navigation, suggested_output
from web_paths import OUTPUT_SUFFIXES, browse, selected_output


class WorkspaceUIPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="web-path-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cursor_pages_are_sorted_complete_and_scope_bound(self):
        for number in range(420):
            (self.root / ("file-%03d.tar" % number)).write_bytes(b"x")
        for name in ("Z-folder", "a-folder", "中文目录"):
            (self.root / name).mkdir()
        (self.root / ".hidden").write_text("hidden")
        fields = {"mode": "file", "path": str(self.root), "hidden": "0"}
        rows, cursor = [], ""
        while True:
            result = browse(dict(fields, cursor=cursor))
            self.assertEqual(result["total"], 423)
            rows += result["entries"]
            cursor = result["next"]
            if not cursor:
                break
        self.assertEqual(len(rows), 423)
        self.assertEqual(len({item["path"] for item in rows}), 423)
        self.assertTrue(all(item["directory"] for item in rows[:3]))
        self.assertEqual([item["name"] for item in rows], sorted(
            (item["name"] for item in rows), key=lambda name: (not (self.root/name).is_dir(), name.casefold(), name)))
        self.assertEqual(rows[-1]["bytes"], 1)
        first = browse(fields)
        selected = browse(dict(fields, path=str(self.root / "file-419.tar"), initial="1"))
        self.assertEqual(selected["selected"]["path"], str(self.root / "file-419.tar"))
        for change in ({"filter": "file"}, {"mode": "output"}, {"path": str(self.root / "a-folder")}):
            with self.subTest(change=change), self.assertRaises(BuildError):
                browse(dict(fields, cursor=first["next"], **change))

    def test_hidden_filter_places_and_breadcrumbs_are_explicit(self):
        (self.root / ".hidden").write_bytes(b"hidden")
        (self.root / "visible").mkdir()
        result = browse({"mode": "file", "path": str(self.root), "hidden": "0"})
        self.assertEqual([item["name"] for item in result["entries"]], ["visible"])
        shown = browse({"mode": "file", "path": str(self.root), "hidden": "1"})
        self.assertEqual(shown["total"], 2)
        self.assertEqual(result["breadcrumbs"][-1]["path"], str(self.root))
        self.assertTrue(result["places"])
        self.assertTrue(result["separator"])

    def test_output_mode_shows_existing_names_and_supports_gzip_and_json(self):
        old = self.root / "old.tar.gz";old.write_bytes(b"existing")
        result = browse({"mode": "output-gzip", "path": str(self.root)})
        self.assertIn("old.tar.gz", [entry["name"] for entry in result["entries"]])
        for name, kind in (("new.tar.gz", "gzip"), ("new.tgz", "gzip"), ("new.tar", "archive"),
                           ("new.tar.gz", "archive"), ("new.json", "json")):
            self.assertEqual(selected_output(self.root, name, OUTPUT_SUFFIXES[kind]), str(self.root/name))
            self.assertFalse((self.root/name).exists())
        for name, kind in (("old.tar.gz", "gzip"), ("wrong.tar", "gzip"), ("wrong.json", "archive"),
                           ("../escape.tar.gz", "gzip"), ("bad\n.tar.gz", "gzip")):
            with self.subTest(name=name), self.assertRaises(BuildError):
                selected_output(self.root, name, OUTPUT_SUFFIXES[kind])
        self.assertEqual(old.read_bytes(), b"existing")

    def test_initial_missing_location_falls_back_without_creating_directories(self):
        missing = self.root / "not-created" / "nested" / "new.tar.gz"
        result = browse({"mode": "output-gzip", "path": str(missing), "initial": "1"})
        self.assertEqual(result["path"], str(self.root))
        self.assertEqual(result["filename"], "new.tar.gz")
        self.assertTrue(result["hint"])
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(BuildError):
            browse({"mode": "directory", "path": str(missing.parent)})

    def test_invalid_or_deeply_nested_cursor_returns_a_user_error(self):
        nested = base64.urlsafe_b64encode(("[" * 1100 + "0" + "]" * 1100).encode()).decode()
        for cursor in ("%%%%", "A"*4097, nested, base64.urlsafe_b64encode(b"true").decode()):
            with self.subTest(cursor=cursor[:20]), self.assertRaises(BuildError):
                browse({"mode": "file", "path": str(self.root), "cursor": cursor})

    def test_name_suggestions_are_new_and_navigation_is_progressive(self):
        source = self.root / "业务包.tar";source.write_bytes(b"source")
        item = {"archive": str(source), "tag": "example/app:1"}
        existing = self.root / "业务包-compressed.tar.gz";existing.write_bytes(b"existing")
        candidate = suggested_output(item, "compress")
        self.assertEqual(candidate, str(self.root / "业务包-1-compressed.tar.gz"))
        self.assertFalse(Path(candidate).exists())
        cas = suggested_output({"archive": None, "tag": "registry/p/app:1"}, "save")
        self.assertTrue(cas.endswith("app-1-saved.tar"))
        rendered = navigation(("compress", "verify", "slim", "push"))
        for action in ("compress", "verify", "slim", "push"):
            self.assertIn('#operation-' + action, rendered)
        self.assertIn("operation-groups", rendered)
        self.assertIn("保留镜像身份", rendered)
        self.assertEqual(source.read_bytes(), b"source")
        long_source = self.root / ("界" * 82 + ".tar")
        long_source.write_bytes(b"source")
        long_candidate = suggested_output({"archive": str(long_source), "tag": "app:1"}, "compress")
        self.assertLessEqual(len(Path(long_candidate).name.encode("utf-8")), 255)
