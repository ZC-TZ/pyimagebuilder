"""比较真实复制调用次数和归档字节，防止缓冲优化改变镜像身份。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from image_reader import sha256_file
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter


class ArchiveCopyBufferTests(unittest.TestCase):
    def test_larger_reads_preserve_docker_and_oci_archive_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "layer.tar"
            with tarfile.open(str(source), "w") as archive:
                member = tarfile.TarInfo("app.war")
                member.size = 9 * 1024 * 1024
                archive.addfile(member, io.BytesIO(b"x" * member.size))
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(source)]}}
            real_open = open

            class ObservedFile:
                def __init__(self, stream, reads):
                    self.stream, self.reads = stream, reads

                def read(self, size=-1):
                    self.reads.append(size)
                    return self.stream.read(size)

                def __enter__(self):
                    return self

                def __exit__(self, *exception):
                    return self.stream.__exit__(*exception)

            for writer_type, module in ((ImageArchiveWriter, "image_writer"),
                                        (OCIImageWriter, "oci_writer")):
                with self.subTest(format=module):
                    baseline = root / (module + "-old.tar")
                    output = root / (module + "-new.tar")
                    before, after = [], []
                    progress = []
                    with patch("reproducible.open", side_effect=lambda *a, **kw:
                               ObservedFile(real_open(*a, **kw), before), create=True), \
                            patch(module + ".TAR_COPY_BUFFER", 16 * 1024):
                        writer_type().write_new(baseline, config, [source], "example/app:1",
                                                progress=lambda *event: None)
                    with patch("reproducible.open", side_effect=lambda *a, **kw:
                               ObservedFile(real_open(*a, **kw), after), create=True):
                        writer_type().write_new(output, config, [source], "example/app:1",
                                                progress=lambda *event: progress.append(event))
                    self.assertLess(len(after), len(before) // 8)
                    self.assertTrue(progress)
                    self.assertEqual(progress[-1][:2], (source.stat().st_size, source.stat().st_size))
                    self.assertEqual(sha256_file(baseline), sha256_file(output))
                    writer_type().verify(output, "example/app:1")


if __name__ == "__main__":
    unittest.main()
