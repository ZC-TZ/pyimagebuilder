"""贯通 rootfs 命令、CAS、再构建，并验证扁平化的文件与配置语义。"""

import contextlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import BuildError
from image_cli import load_archive, save_archive
from image_reader import ImageArchiveReader, sha256_file
from image_store import _archive_path
from image_reader import BaseImage
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from rootfs import RootFSIndex
from rootfs_archive import export_rootfs, import_rootfs, flatten_image
import rootfs_archive
import main


BASE = "example/app:1"
PROJECT = Path(__file__).resolve().parents[1]


def tar_at(path, entries, mode="w"):
    """构造真实 tar 头，可直接模拟 Linux 属主、链接、设备及 PAX 元数据。"""
    with tarfile.open(path, mode, format=tarfile.PAX_FORMAT) as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode, member.uid, member.gid, member.mtime = 0o751, 1201, 1302, 123456
            member.uname, member.gname = "tongadmin", "tonggrp"
            member.pax_headers = {"SCHILY.xattr.user.example": "value"}
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                if kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                    member.linkname = value
                if kind in (tarfile.CHRTYPE, tarfile.BLKTYPE):
                    member.devmajor, member.devminor = 1, 3
                archive.addfile(member)


def source_at(root, kind="docker", architecture="amd64", empty=False):
    """两层示例含删除、opaque 目录和覆盖内容；配置含运行字段与额外扩展。"""
    layers = []
    if not empty:
        for number, entries in enumerate((
                [("app/data", tarfile.REGTYPE, b"old"), ("old", tarfile.REGTYPE, b"gone"),
                 ("logs/stale", tarfile.REGTYPE, b"gone")],
                [(".wh.old", tarfile.REGTYPE, b""), ("logs/.wh..wh..opq", tarfile.REGTYPE, b""),
                 ("logs/current", tarfile.REGTYPE, b"keep"), ("app/data", tarfile.REGTYPE, b"new")])):
            layer = root / ("layer-{}.tar".format(number))
            tar_at(layer, entries)
            layers.append(layer)
    runtime = {"Env": ["PATH=/bin:/soft/TongWeb7.0/bin", "JAVA_HOME=/soft/TongWeb7.0/jdk1.8"],
               "User": "tongadmin", "WorkingDir": "/soft/TongWeb7.0",
               "Entrypoint": ["bin/startserver.sh"], "Cmd": ["--prod"], "Shell": ["/bin/bash", "-c"],
               "ExposedPorts": {"8089/tcp": {}}, "Volumes": {"/logs": {}},
               "Labels": {"app": "tongweb"}, "StopSignal": "SIGQUIT",
               "Healthcheck": {"Test": ["CMD", "check"], "Interval": 30000000000}}
    config = {"os": "linux", "architecture": architecture, "config": runtime,
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in layers]},
              "history": [{"created_by": "source"} for _ in layers], "x-company": {"build": "原始"}}
    output = root / (kind + "-base.tar")
    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
    writer.write_image(output, BaseImage(config, layers, [BASE], {}, config_raw=(json.dumps(config, indent=3) + '\n').encode()), BASE)
    return output, config


def read_image(path, root, tag=BASE):
    image = ImageArchiveReader(path, root / (path.stem + "-read")).read(tag)
    index = RootFSIndex()
    for layer in image.layers:
        index.apply_layer(layer)
    return image, index


class RootfsCommandTests(unittest.TestCase):
    def test_build_export_import_load_save_and_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "app.war").write_bytes(b"WAR")
            (context / "Dockerfile").write_text("FROM scratch\nCOPY app.war /app.war\nENV OLD=yes\n")
            source = root / "built.tar"
            build(context / "Dockerfile", context, None, None, BASE, source)
            exported, imported = root / "rootfs.tar", root / "imported.tar"
            export_rootfs(source, exported)
            changes = ["ENV PATH=/usr/bin:/soft/TongWeb7.0/bin", "ENV TMOUT=120 LANG=en_US.utf8",
                       "ENV JAVA_HOME=/soft/TongWeb7.0/jdk1.8 SECURITY_ENABLED=true UMASK=0022",
                       "USER tongadmin", "WORKDIR /soft/TongWeb7.0", "EXPOSE 8089",
                       'ENTRYPOINT ["bin/startserver.sh"]']
            result = import_rootfs(exported, "cmas_back:9.4-flat", imported, changes)
            image, index = read_image(imported, root, "cmas_back:9.4-flat")
            self.assertEqual(index.read_file("app.war"), b"WAR")
            self.assertEqual(len(image.layers), 1)
            self.assertEqual(image.config["config"]["Entrypoint"], ["bin/startserver.sh"])
            self.assertEqual(image.config["config"]["ExposedPorts"], {"8089/tcp": {}})
            self.assertNotIn("OLD=yes", image.config["config"]["Env"])
            self.assertIsNone(index.kind("soft"))  # --change WORKDIR 只设置配置。
            self.assertEqual(result["image_id"], image.config_digest)
            store = root / "store"
            load_archive(imported, store)
            saved = root / "saved.tar"
            save_archive("cmas_back:9.4-flat", saved, store)
            self.assertEqual(saved.read_bytes(), imported.read_bytes())
            (context / "Dockerfile").write_text("FROM cmas_back:9.4-flat\nCOPY app.war /next.war\n")
            base = {"type": "cas", "store": str(store), "reference": "cmas_back:9.4-flat",
                    "platform": "linux/amd64", "source": "local"}
            built = root / "child.tar"
            build(context / "Dockerfile", context, base, None, "child:1", built)
            self.assertEqual(read_image(built, root, "child:1")[1].read_file("next.war"), b"WAR")

    def test_flatten_preserves_config_whiteouts_source_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, config = source_at(root)
            original = source.read_bytes()
            outputs = []
            for number in range(2):
                output = root / ("flat-{}.tar".format(number))
                result = flatten_image(source, output)
                image, index = read_image(output, root)
                self.assertEqual(image.config["config"], config["config"])
                self.assertEqual(image.config["x-company"], config["x-company"])
                self.assertEqual(len(image.layers), 1)
                self.assertEqual(index.read_file("app/data"), b"new")
                self.assertEqual(index.read_file("logs/current"), b"keep")
                self.assertIsNone(index.kind("old"))
                self.assertIsNone(index.kind("logs/stale"))
                self.assertNotEqual(result["image_id"], result["source_image_id"])
                outputs.append(output.read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(source.read_bytes(), original)

    def test_metadata_links_original_inode_and_special_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second, third = [root / name for name in ("first.tar", "second.tar", "third.tar")]
            tar_at(first, [(".", tarfile.DIRTYPE, ""), ("original", tarfile.REGTYPE, b"old inode"),
                           ("link", tarfile.SYMTYPE, "/original"), ("fifo", tarfile.FIFOTYPE, ""),
                           ("dev/null", tarfile.CHRTYPE, ""), ("dev/disk", tarfile.BLKTYPE, "")])
            tar_at(second, [("alias-a", tarfile.LNKTYPE, "alias-b"), ("alias-b", tarfile.LNKTYPE, "/original")])
            tar_at(third, [("original", tarfile.REGTYPE, b"new inode")])
            merged = root / "merged.tar"
            rootfs_archive.merge_rootfs([first, second, third], merged)
            with tarfile.open(merged) as archive:
                self.assertEqual(archive.extractfile("alias-a").read(), b"old inode")
                self.assertEqual(archive.extractfile("alias-b").read(), b"old inode")
                self.assertEqual(archive.extractfile("original").read(), b"new inode")
                self.assertTrue(archive.getmember("alias-b").islnk())
                self.assertEqual(archive.getmember("alias-b").linkname, "alias-a")
                self.assertEqual(archive.getmember("link").linkname, "/original")
                self.assertTrue(archive.getmember("fifo").isfifo())
                self.assertTrue(archive.getmember("dev/null").ischr())
                self.assertTrue(archive.getmember("dev/disk").isblk())
                self.assertEqual((archive.getmember("dev/null").devmajor, archive.getmember("dev/null").devminor), (1, 3))
                for name in (".", "original", "alias-a", "fifo", "link"):
                    member = archive.getmember(name)
                    self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o751, 1201, 1302, 123456))
                    self.assertEqual(member.pax_headers["SCHILY.xattr.user.example"], "value")
            imported = root / "imported.tar"
            import_rootfs(merged, "special:1", imported)
            image, index = read_image(imported, root, "special:1")
            self.assertEqual(index.read_file("alias-a"), b"old inode")
            self.assertEqual(index.kind("fifo"), "other")
            again = root / "again.tar"
            export_rootfs(imported, again)
            self.assertEqual(again.read_bytes(), merged.read_bytes())

    def test_compressed_rootfs_both_formats_and_arm64(self):
        for mode in ("w:gz", "w:bz2", "w:xz"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "rootfs.compressed"
                tar_at(source, [("app.war", tarfile.REGTYPE, b"WAR")], mode)
                for kind in ("docker", "oci"):
                    output = root / (kind + ".tar")
                    result = import_rootfs(source, "app", output, platform="linux/arm64", format=kind,
                                           source_date_epoch=123, message="offline import")
                    self.assertEqual(result["tag"], "app:latest")
                    store = CASStore(root / (kind + "-store"))
                    getattr(store, "import_" + kind)(output, "app:latest", "linux/arm64")
                    image = store.open_base("app:latest", "linux/arm64")
                    self.assertEqual(image.config["architecture"], "arm64")
                    self.assertEqual(image.config["history"][0]["comment"], "offline import")

    def test_export_and_flatten_cas_do_not_materialize_base_tar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            store = root / "store"
            cas = CASStore(store)
            cas.import_docker(source, BASE, "linux/amd64", "artifactory")
            with patch.object(CASStore, "export_docker", side_effect=AssertionError("intermediate tar")):
                export_rootfs(BASE, root / "rootfs.tar", store=store)
                flatten_image(BASE, root / "flat.tar", store=store, reference="flat:1")
            self.assertFalse(_archive_path(store, BASE, "linux/amd64", "artifactory").exists())
            self.assertEqual(read_image(root / "flat.tar", root, "flat:1")[1].read_file("app/data"), b"new")

    def test_legacy_tar_sources_and_ambiguous_cas_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            store = root / "store"
            store.mkdir()
            target = _archive_path(store, BASE, "linux/amd64", "local")
            target.write_bytes(source.read_bytes())
            export_rootfs(BASE, root / "legacy.tar", store=store)
            cas = CASStore(store)
            cas.import_docker(source, BASE, "linux/amd64", "registry")
            with self.assertRaisesRegex(BuildError, "Multiple stored sources"):
                flatten_image(BASE, root / "bad.tar", store=store)
            flatten_image(BASE, root / "good.tar", store=store, kind="registry")

    def test_oci_flatten_retains_image_annotations_and_updates_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, config = source_at(root, kind="oci")
            with tarfile.open(source) as archive:
                index = json.load(archive.extractfile("index.json"))
                name = "blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]
                manifest = json.load(archive.extractfile(name))
            manifest["annotations"] = {"org.example": "retained"}
            manifest["config"]["annotations"] = {"config.example": "retained"}
            index["annotations"] = {"index.example": "retained"}
            OCIImageWriter().write_new(source, config, [root / "layer-0.tar", root / "layer-1.tar"], BASE,
                                      manifest_template=manifest, index_template=index)
            output = root / "flat.oci.tar"
            flatten_image(source, output, format="oci", reference="flat:1")
            OCIImageWriter().verify(output, "flat:1")
            with tarfile.open(output) as archive:
                new_index = json.load(archive.extractfile("index.json"))
                new_name = "blobs/sha256/" + new_index["manifests"][0]["digest"].split(":")[1]
                new_manifest = json.load(archive.extractfile(new_name))
            self.assertEqual(new_manifest["annotations"], manifest["annotations"])
            self.assertEqual(new_manifest["config"]["annotations"], manifest["config"]["annotations"])
            self.assertEqual(new_index["annotations"], index["annotations"])
            self.assertEqual(len(new_manifest["layers"]), 1)

    def test_binary_stdout_stdin_and_main_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            process = subprocess.run([sys.executable, str(PROJECT / "main.py"), "export", str(source), "--quiet"],
                                     cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(process.returncode, 0, process.stderr)
            with tarfile.open(fileobj=io.BytesIO(process.stdout)) as archive:
                self.assertEqual(archive.extractfile("app/data").read(), b"new")
            output = root / "imported.tar"
            process = subprocess.run([sys.executable, str(PROJECT / "main.py"), "import", "-", "pipe:1",
                                      "-o", str(output), "--change", "ENV MODE=prod", "--quiet"],
                                     input=process.stdout, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["tag"], "pipe:1")
            self.assertEqual(read_image(output, root, "pipe:1")[1].read_file("app/data"), b"new")

    def test_standalone_entry_all_commands_from_other_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            for arguments in (("export", str(source), "-o", str(root / "rootfs.tar")),
                              ("import", str(root / "rootfs.tar"), "standalone:1", "-o", str(root / "imported.tar")),
                              ("flatten", str(source), "-t", "standalone:2", "-o", str(root / "flat.tar"))):
                process = subprocess.run([sys.executable, str(PROJECT / "rootfs_archive.py")] + list(arguments) + ["--quiet"],
                                         cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn("output", json.loads(process.stdout))

    def test_full_metadata_changes_do_not_execute_triggers_or_create_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            changes = ["ENV PREFIX=/new PORT=9090", 'LABEL site="$PREFIX"', "WORKDIR $PREFIX/work",
                       "USER 123:456", "EXPOSE $PORT/udp", 'VOLUME ["/data"]',
                       'CMD ["--custom"]', 'ENTRYPOINT ["launch"]', "STOPSIGNAL SIGTERM",
                       "HEALTHCHECK --interval=30s --timeout=3s CMD check", "ONBUILD COPY child /child"]
            output = root / "flat.tar"
            flatten_image(source, output, changes=changes)
            image, index = read_image(output, root)
            runtime = image.config["config"]
            self.assertEqual(runtime["WorkingDir"], "/new/work")
            self.assertEqual(runtime["Labels"]["site"], "/new")
            self.assertEqual(runtime["User"], "123:456")
            self.assertEqual(runtime["Cmd"], ["--custom"])
            self.assertEqual(runtime["ExposedPorts"]["9090/udp"], {})
            self.assertEqual(runtime["Healthcheck"]["Test"], ["CMD-SHELL", "check"])
            self.assertEqual(runtime["Healthcheck"]["Timeout"], 3000000000)
            self.assertEqual(runtime["OnBuild"], ["COPY child /child"])
            self.assertIsNone(index.kind("new"))
            self.assertIsNone(index.kind("data"))
            self.assertIsNone(index.kind("child"))
            context = root / "context"
            context.mkdir()
            (context / "child").write_bytes(b"triggered later")
            (context / "Dockerfile").write_text("FROM " + BASE + "\n")
            built = root / "child.tar"
            build(context / "Dockerfile", context, output, None, "child:1", built)
            self.assertEqual(read_image(built, root, "child:1")[1].read_file("child"), b"triggered later")

    def test_entrypoint_cmd_order_shell_forms_and_healthcheck_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            output = root / "flat.tar"
            flatten_image(source, output, changes=["ENTRYPOINT exec launch", "HEALTHCHECK NONE"])
            runtime = read_image(output, root)[0].config["config"]
            self.assertEqual(runtime["Entrypoint"], ["/bin/bash", "-c", "exec launch"])
            self.assertIsNone(runtime["Cmd"])
            self.assertEqual(runtime["Healthcheck"], {"Test": ["NONE"]})

    def test_invalid_changes_do_not_publish_output(self):
        for change in ("RUN echo bad", "COPY a /a", "FROM scratch", "ARG A=1", 'SHELL ["sh"]',
                       "", "ENV A=1\nRUN touch /x", "EXPOSE ${PORT}", "STOPSIGNAL invalid"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "rootfs.tar"
                tar_at(source, [("file", tarfile.REGTYPE, b"data")])
                output = root / "bad.tar"
                with self.assertRaises(BuildError):
                    import_rootfs(source, "bad:1", output, changes=[change])
                self.assertFalse(output.exists())

    def test_unsafe_duplicate_reserved_and_invalid_link_inputs(self):
        cases = [[("../outside", tarfile.REGTYPE, b"x")], [("/etc/x", tarfile.REGTYPE, b"x")],
                 [("a\\b", tarfile.REGTYPE, b"x")], [(".wh.file", tarfile.REGTYPE, b"x")],
                 [("file", tarfile.REGTYPE, b"x"), ("./file", tarfile.REGTYPE, b"y")],
                 [("link", tarfile.SYMTYPE, "/tmp"), ("link/child", tarfile.REGTYPE, b"x")],
                 [("a", tarfile.LNKTYPE, "b"), ("b", tarfile.LNKTYPE, "a")],
                 [("a", tarfile.LNKTYPE, "missing")], [("a", tarfile.LNKTYPE, "../outside")],
                 [("a", b"Z", "")]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / "rootfs.tar", root / "bad.tar"
                tar_at(source, entries)
                with self.assertRaises(BuildError):
                    import_rootfs(source, "bad:1", output)
                self.assertFalse(output.exists())
                self.assertEqual(list(root.glob("pyimagebuilder-import-*")), [])

    def test_existing_outputs_and_publication_race_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root)
            raw = root / "rootfs.tar"
            export_rootfs(source, raw)
            output = root / "existing.tar"
            output.write_bytes(b"existing")
            for operation in (lambda: export_rootfs(source, output), lambda: flatten_image(source, output),
                              lambda: import_rootfs(raw, "app:1", output)):
                with self.assertRaises(BuildError):
                    operation()
                self.assertEqual(output.read_bytes(), b"existing")
            for number, operation in enumerate((export_rootfs, flatten_image)):
                output = root / ("raced-{}.tar".format(number))
                original = rootfs_archive.publish_new_file
                def race(partial, target):
                    Path(target).write_bytes(b"competitor")
                    return original(partial, target)
                with patch.object(rootfs_archive, "publish_new_file", side_effect=race):
                    with self.assertRaises(FileExistsError):
                        operation(source, output)
                self.assertEqual(output.read_bytes(), b"competitor")

    def test_platform_tag_and_missing_inputs_have_controlled_cli_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root, architecture="arm64")
            for arguments in (("export", str(source)), ("flatten", str(source)),
                              ("export", str(root / "missing.tar")),
                              ("import", str(source), "Invalid Tag")):
                output = root / "bad.tar"
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main.main(list(arguments) + ["-o", str(output), "--quiet"]), 1)
                self.assertFalse(output.exists())
            export_rootfs(source, root / "arm64-rootfs.tar", platform="linux/arm64")

    def test_zero_layer_images_and_empty_rootfs_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = source_at(root, empty=True)
            raw = root / "rootfs.tar"
            export_rootfs(source, raw)
            with tarfile.open(raw) as archive:
                self.assertEqual(archive.getmembers(), [])
            output = root / "empty.tar"
            import_rootfs(raw, "empty:1", output)
            image, index = read_image(output, root, "empty:1")
            self.assertEqual(len(image.layers), 1)
            self.assertEqual(index.entries, {})
            flatten_image(source, root / "flat.tar")

    def test_long_paths_and_link_pax_headers_are_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "app/" + "x" * 180
            source = root / "rootfs.tar"
            tar_at(source, [("./" + name, tarfile.REGTYPE, b"long"),
                           ("alias", tarfile.LNKTYPE, "./" + name)])
            imported = root / "imported.tar"
            import_rootfs(source, "app:1", imported)
            exported = root / "exported.tar"
            export_rootfs(imported, exported)
            with tarfile.open(exported) as archive:
                self.assertEqual(archive.extractfile(name).read(), b"long")
                self.assertEqual(archive.extractfile("alias").read(), b"long")
                self.assertNotIn("./" + name, archive.getnames())

    def test_large_file_progress_uses_existing_reporter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "rootfs.tar"
            tar_at(raw, [("big", tarfile.REGTYPE, b"x" * (9 * 1024 * 1024))])
            output = root / "image.tar"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as progress:
                self.assertEqual(main.main(["import", str(raw), "large:1", "-o", str(output),
                                           "--progress", "json"]), 0)
            events = [json.loads(line) for line in progress.getvalue().splitlines()]
            self.assertTrue(any(event["type"] == "step_progress" for event in events))
            self.assertEqual(read_image(output, root, "large:1")[1].read_file("big", limit=10*1024*1024),
                             b"x" * (9 * 1024 * 1024))

    def test_port_registry_tags_cli_aliases_and_reproducible_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rootfs.tar"
            tar_at(source, [("file", tarfile.REGTYPE, b"data")])
            first, second = root / "one.tar", root / "two.tar"
            reference = "harbor.internal:5000/team/app:1"
            for output in (first, second):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main.main(["import", str(source), reference, "-o", str(output),
                                                "--source-date-epoch", "123", "--quiet"]), 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            flat = root / "flat.tar"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main.main(["flatten", str(first), "--input-tag", reference,
                                            "--tag", "harbor.internal:5000/team/flat", "-o", str(flat),
                                            "--quiet"]), 0)
            self.assertEqual(read_image(flat, root, "harbor.internal:5000/team/flat:latest")[1].read_file("file"), b"data")

    def test_malformed_input_and_failed_verification_leave_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "bad.tar", root / "output.tar"
            source.write_bytes(b"not a tar")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main.main(["import", str(source), "bad:1", "-o", str(output), "--quiet"]), 1)
            self.assertFalse(output.exists())
            source, _ = source_at(root)
            with patch.object(ImageArchiveWriter, "verify", side_effect=BuildError("injected verification failure")):
                with self.assertRaises(BuildError):
                    flatten_image(source, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("pyimagebuilder-flatten-*")), [])

    def test_multi_platform_docker_archive_selects_requested_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archives = []
            for architecture in ("amd64", "arm64"):
                directory = root / architecture
                directory.mkdir()
                source, _ = source_at(directory, architecture=architecture)
                archives.append(source)
            manifest, contents = [], {}
            for source in archives:
                with tarfile.open(source) as archive:
                    manifest.extend(json.load(archive.extractfile("manifest.json")))
                    for member in archive:
                        if member.isfile() and member.name not in ("manifest.json", "repositories"):
                            contents[member.name] = archive.extractfile(member).read()
            contents["manifest.json"] = json.dumps(manifest).encode()
            multi = root / "multi.tar"
            with tarfile.open(multi, "w") as archive:
                for name, data in contents.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            output = root / "flat.tar"
            flatten_image(multi, output, platform="linux/arm64")
            self.assertEqual(read_image(output, root)[0].config["architecture"], "arm64")
            with self.assertRaises(BuildError):
                flatten_image(multi, root / "bad.tar", tag="missing:1")


if __name__ == "__main__":
    unittest.main()
