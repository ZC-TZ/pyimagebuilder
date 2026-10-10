#!/usr/bin/env python3
"""真实 Linux/WSL RUN 冒烟测试，需要带 sh/rm 的 linux/amd64 基础镜像。"""

import argparse
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from errors import BuildError
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-tar", type=Path, required=True)
    parser.add_argument("--from", dest="reference", required=True)
    parser.add_argument("--workspace", type=Path, default=Path("/tmp"))
    parser.add_argument("--sandbox", choices=("legacy", "hardened", "rootless"),
                        default="hardened")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-smoke-", dir=args.workspace) as temp:
        root = Path(temp)
        context = root / "context"
        context.mkdir()
        (context / "marker.txt").write_text("delete me", encoding="utf-8")
        with tarfile.open(context / "bundle.tar.gz", "w:gz") as archive:
            member = tarfile.TarInfo("payload.txt")
            payload = b"from local ADD\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        dockerfile = context / "Dockerfile"
        owner_option = "" if args.sandbox == "rootless" else "--chown=123:456 "
        dockerfile.write_text("FROM {}\nUSER 0\n"
                              "COPY {}--chmod=0700 marker.txt /tmp/marker.txt\n"
                              "ADD bundle.tar.gz /tmp/archive/\n"
                              "RUN test -f /tmp/archive/payload.txt && rm /tmp/marker.txt "
                              "&& printf ok > /tmp/run-result\n".format(args.reference, owner_option),
                              encoding="utf-8")
        success = root / "success.tar"
        build(dockerfile, context, args.base_tar, None, "pyimagebuilder/smoke:success",
              success, enable_run=True, workspace_dir=args.workspace,
              run_sandbox=args.sandbox)
        with tempfile.TemporaryDirectory(dir=args.workspace) as unpacked:
            image = ImageArchiveReader(success, Path(unpacked)).read("pyimagebuilder/smoke:success")
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            if (index.kind("tmp/marker.txt") is not None or
                    index.kind("tmp/run-result") != "file" or
                    index.kind("tmp/archive/payload.txt") != "file"):
                raise BuildError("RUN output or whiteout is incorrect")
            with tarfile.open(image.layers[-3]) as archive:
                marker = archive.getmember("tmp/marker.txt")
                expected_ownership = (0, 0) if args.sandbox == "rootless" else (123, 456)
                if (marker.uid, marker.gid, marker.mode) != (*expected_ownership, 0o700):
                    raise BuildError("COPY ownership or mode is incorrect")

        # 在严格宿主 umask 下删除基础 /tmp，再验证执行器补建目录及非 root 写入。
        # rootless 单映射模式继续使用镜像 0:0，检查其宿主 umask 隔离。
        run_owner = (0, 0) if args.sandbox == "rootless" else (12345, 23456)
        dockerfile.write_text(
            "FROM {}\nUSER 0:0\nRUN rm -rf /tmp\n"
            "USER {}:{}\nRUN umask 077; printf permissions-ok > /tmp/permission-result\n".format(
                args.reference, *run_owner), encoding="utf-8")
        permissions = root / "permissions.tar"
        previous_umask = os.umask(0o077)
        try:
            build(dockerfile, context, args.base_tar, None, "pyimagebuilder/smoke:permissions",
                  permissions, enable_run=True, workspace_dir=args.workspace,
                  run_sandbox=args.sandbox)
        finally:
            os.umask(previous_umask)
        with tempfile.TemporaryDirectory(dir=args.workspace) as unpacked:
            image = ImageArchiveReader(permissions, Path(unpacked)).read("pyimagebuilder/smoke:permissions")
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            if index.read_file("/tmp/permission-result") != b"permissions-ok":
                raise BuildError("RUN cannot write to a newly created /tmp")
            for name, owner, mode in (("tmp", (0, 0), 0o1777),
                                      ("tmp/permission-result", run_owner, 0o600)):
                entry = index.entries[name]
                with tarfile.open(entry.layer) as archive:
                    member = archive.getmember(entry.member)
                    if (member.uid, member.gid, member.mode) != (*owner, mode):
                        raise BuildError("RUN permission metadata is incorrect: " + name)
        dockerfile.write_text("FROM {}\nUSER 0\nRUN false\n".format(args.reference), encoding="utf-8")
        failure = root / "failure.tar"
        try:
            build(dockerfile, context, args.base_tar, None, "pyimagebuilder/smoke:failure",
                  failure, enable_run=True, workspace_dir=args.workspace,
                  run_sandbox=args.sandbox)
        except BuildError:
            pass
        else:
            raise BuildError("RUN false unexpectedly succeeded")
        if failure.exists():
            raise BuildError("Failed build published an output tar")
    print("Linux smoke test passed ({}): COPY metadata, ADD, RUN, whiteout, permissions, nonzero exit".format(args.sandbox))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BuildError, OSError) as exc:
        print("Linux RUN smoke test failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
