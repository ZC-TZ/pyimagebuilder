#!/usr/bin/env python3
"""使用带 shell 的基础镜像测试真实 Linux cache/secret 挂载。"""

import argparse
import sys
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
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--sandbox", choices=("hardened", "rootless", "legacy"),
                        default="hardened")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-phase13-") as temp:
        root = Path(temp)
        context = root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(
            "FROM {}\nARG MODE=ok\n"
            "RUN --mount=type=cache,target=/build-cache,id=phase13 "
            "--mount=type=secret,id=token "
            "test -s /run/secrets/token && printf '%s' \"$MODE\" > /build-cache/mark\n"
            "RUN --mount=type=cache,target=/build-cache,id=phase13 "
            "test -s /build-cache/mark && printf success > /phase13-result\n".format(
                args.reference), encoding="utf-8")
        secret = root / "token.txt"
        secret.write_text("phase13-private-marker-123", encoding="utf-8")
        output = root / "out.tar"
        build(dockerfile, context, args.base_tar, None, "pyimagebuilder/phase13:smoke",
              output, enable_run=True, workspace_dir=args.workspace,
              cache_dir=root / "cache", run_sandbox=args.sandbox,
              secret_sources={"token": secret})
        with tempfile.TemporaryDirectory() as extracted:
            image = ImageArchiveReader(output, Path(extracted)).read(
                "pyimagebuilder/phase13:smoke")
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            if index.kind("phase13-result") != "file" or index.kind("build-cache/mark"):
                raise BuildError("Cache mount result leaked into image or RUN failed")
        if secret.read_bytes() in output.read_bytes():
            raise BuildError("Secret bytes leaked into Docker archive")
    print("Phase 13 Linux mount smoke test passed")


if __name__ == "__main__":
    main()
