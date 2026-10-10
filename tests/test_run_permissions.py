"""审查 RUN 身份隔离、重复账户解析及临时目录权限；不依赖宿主 root。"""

import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor
from overlay import OverlayManager
from dockerfile_parser import parse
from errors import BuildError
from rootfs_materializer import RootFSMaterializer


class ChildExited(BaseException):
    """替代子进程 _exit，让测试可以核对执行命令前的身份设置。"""


class RunPermissionsTests(unittest.TestCase):
    def _accounts(self, passwd, groups):
        def image_open(path, **kwargs):
            content = {"/etc/passwd": passwd, "/etc/group": groups}[path]
            if content is None:
                raise FileNotFoundError(path)
            return mock_open(read_data=content)()
        return patch("builtins.open", side_effect=image_open)

    def _execute_child(self, root, user="", sandbox="legacy"):
        """替代 Linux 系统调用，实际走 execute 的子进程准备与身份切换分支。"""
        runner = object.__new__(executor.RunExecutor)
        runner.sandbox, runner.network = sandbox, "none"
        overlay = SimpleNamespace(merged=root, mount_in_child=Mock())
        command = parse("FROM scratch\nRUN echo ready\n")[1].value
        events = []
        with ExitStack() as stack:
            for name in ("setgroups", "setgid", "setuid", "execvpe"):
                stack.enter_context(patch.object(executor.os, name,
                    side_effect=lambda *args, _name=name: events.append((_name, args)), create=True))
            stack.enter_context(patch.object(executor.os, "pipe", return_value=(101, 102)))
            stack.enter_context(patch.object(executor.os, "fork", return_value=0, create=True))
            stack.enter_context(patch.object(executor.os, "waitpid", return_value=(0, 0), create=True))
            stack.enter_context(patch.object(executor.os, "_exit", side_effect=ChildExited))
            for name in ("close", "write", "chroot", "chdir", "chown"):
                stack.enter_context(patch.object(executor.os, name, create=True))
            for name in ("mount", "_unshare", "_map_rootless_identity", "_prepare_devices",
                         "_no_new_privileges", "_drop_bounding_capabilities", "_clear_capabilities", "install_filter"):
                stack.enter_context(patch.object(executor, name))
            stack.enter_context(patch.object(executor.os, "geteuid", return_value=1001, create=True))
            stack.enter_context(patch.object(executor.os, "getegid", return_value=1002, create=True))
            with self.assertRaises(ChildExited):
                runner.execute(overlay, command, {}, "/", user, ["/bin/sh", "-c"])
        return events

    def test_named_user_sharing_uid_keeps_its_own_supplementary_groups(self):
        with self._accounts("app:x:1201:1202::/:/bin/sh\nalias:x:1201:1203::/:/bin/sh\n",
                            "staff:x:1300:app\nprivileged:x:1400:alias\n"):
            self.assertEqual(executor._resolve_user("app"), (1201, 1202, [1300]))

    def test_numeric_user_uses_first_matching_account_consistently(self):
        with self._accounts("app:x:1201:1202::/:/bin/sh\nalias:x:1201:1203::/:/bin/sh\n",
                            "staff:x:1300:app\nprivileged:x:1400:alias\n"):
            self.assertEqual(executor._resolve_user("1201"), (1201, 1202, [1300]))

    def test_duplicate_username_and_group_use_first_record(self):
        with self._accounts("app:x:1201:1202::/:/bin/sh\napp:x:2201:2202::/:/bin/sh\n",
                            "staff:x:1300:\nstaff:x:2300:\n"):
            self.assertEqual(executor._resolve_user("app:staff"), (1201, 1300, []))

    def test_invalid_passwd_ids_fail_with_build_error(self):
        for uid, gid in (("-1", "1202"), ("1201", "-1"), ("oops", "1202"),
                         ("2147483648", "1202")):
            with self.subTest(uid=uid, gid=gid), self._accounts(
                    "app:x:{}:{}::/:/bin/sh\n".format(uid, gid), None):
                with self.assertRaises(BuildError):
                    executor._resolve_user("app")

    def test_invalid_group_id_fails_with_build_error(self):
        with self._accounts("app:x:1201:1202::/:/bin/sh\n", "staff:x:-1:app\n"):
            with self.assertRaises(BuildError):
                executor._resolve_user("app:staff")

    def test_numeric_user_without_passwd_has_gid_zero(self):
        with self._accounts(None, None):
            self.assertEqual(executor._resolve_user("1201"), (1201, 0, []))

    def test_explicit_group_does_not_add_supplementary_groups(self):
        with self._accounts("app:x:1201:1202::/:/bin/sh\n", "staff:x:1300:app\n"):
            self.assertEqual(executor._resolve_user("app:1202"), (1201, 1202, []))

    def test_default_run_resets_groups_gid_and_uid_before_exec(self):
        with tempfile.TemporaryDirectory() as temporary, self._accounts(None, None):
            events = self._execute_child(Path(temporary))
        self.assertEqual(events[:3], [("setgroups", ([],)), ("setgid", (0,)), ("setuid", (0,))])
        self.assertEqual(events[3][0], "execvpe")

    def test_explicit_default_user_uses_image_account_groups(self):
        with tempfile.TemporaryDirectory() as temporary, self._accounts(
                "root:x:0:0::/:/bin/sh\n", "image-group:x:500:root\n"):
            events = self._execute_child(Path(temporary), user="0")
        self.assertEqual(events[0], ("setgroups", ([500],)))

    def test_new_tmp_mode_is_restored_after_mkdir_umask(self):
        with tempfile.TemporaryDirectory() as temporary, self._accounts(None, None):
            root = Path(temporary)
            with patch.object(Path, "chmod", autospec=True) as chmod:
                self._execute_child(root)
            chmod.assert_any_call(root / "tmp", 0o1777)

    def test_existing_tmp_permissions_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, self._accounts(None, None):
            root = Path(temporary)
            (root / "tmp").mkdir()
            with patch.object(Path, "chmod", autospec=True) as chmod:
                self._execute_child(root)
            chmod.assert_not_called()

    def test_rootless_mapping_does_not_call_forbidden_setgroups(self):
        with tempfile.TemporaryDirectory() as temporary, self._accounts(None, None):
            events = self._execute_child(Path(temporary), sandbox="rootless")
        self.assertEqual([name for name, _ in events], ["execvpe"])

    def test_new_rootfs_root_has_explicit_default_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rootfs"
            with patch.object(executor.os, "chown", create=True) as chown, \
                    patch.object(executor.os, "chmod") as chmod, patch.object(executor.os, "utime"):
                RootFSMaterializer(root)
            chown.assert_called_once_with(root, 0, 0, follow_symlinks=False)
            chmod.assert_called_once_with(root, 0o755, follow_symlinks=False)

    def test_implicit_rootfs_parents_have_explicit_default_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(RootFSMaterializer, "_metadata"):
                materializer = RootFSMaterializer(Path(temporary) / "rootfs")
            with patch.object(materializer, "_metadata") as metadata:
                materializer._parents("app/private/file")
            self.assertEqual([call[0][0] for call in metadata.call_args_list],
                             [materializer.root / "app", materializer.root / "app/private"])
            for call in metadata.call_args_list:
                member = call[0][1]
                self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o755))

    def test_existing_rootfs_parent_metadata_is_not_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(RootFSMaterializer, "_metadata"):
                materializer = RootFSMaterializer(Path(temporary) / "rootfs")
            (materializer.root / "app").mkdir()
            with patch.object(materializer, "_metadata") as metadata:
                materializer._parents("app/file")
            metadata.assert_not_called()

    def test_rootless_materialization_uses_mapped_host_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            materializer = RootFSMaterializer(Path(temporary) / "rootfs", rootless=True)
            path = materializer.root / "file"
            path.write_bytes(b"mapped")
            member = tarfile.TarInfo("file")
            member.mode = 0o640
            with patch.object(executor.os, "chown", create=True) as chown, \
                    patch.object(executor.os, "geteuid", return_value=1001, create=True), \
                    patch.object(executor.os, "getegid", return_value=1002, create=True), \
                    patch.object(executor.os, "chmod"), patch.object(executor.os, "utime"):
                materializer._metadata(path, member)
            chown.assert_called_once_with(path, 1001, 1002, follow_symlinks=False)

    def test_overlay_root_inherits_lower_permissions_instead_of_host_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lower = root / "lower"
            lower.mkdir()
            lower.chmod(0o755)
            with patch.object(Path, "chmod", autospec=True) as chmod, \
                    patch.object(executor.os, "chown", create=True) as chown:
                overlay = OverlayManager(lower, root / "snapshot")
            chmod.assert_any_call(overlay.upper, lower.stat().st_mode & 0o7777)
            chown.assert_called_once_with(overlay.upper, lower.stat().st_uid, lower.stat().st_gid,
                                         follow_symlinks=False)


if __name__ == "__main__":
    unittest.main()
