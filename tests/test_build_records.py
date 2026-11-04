"""使用真实小镜像验证打包台账的写入、查询与失败回滚。"""

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import build_records
import fast
import main
from test_phase1 import make_base


class BuildRecordsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ledger = self.root / "data" / "records"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "schemaVersion": 2, "buildRecordsDir": "data/records",
            "cache": {"layerCache": "data/layer-cache"},
            "fast": {"defaultProfile": "tomcat", "profiles": {
                "tomcat": {"flavor": "tomcat", "baseImage": "example/base:1",
                           "baseTar": str(make_base(self.root)), "owner": "0:0"},
            }},
        }), encoding="utf-8")
        self.config_args = ["--config", str(self.config)]
        environment = patch.dict(os.environ, {"PYIMAGEBUILDER_BUILD_RECORDS_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, entry, argv, status=0):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(entry(argv), status, errors.getvalue())
        return output.getvalue(), errors.getvalue()

    def query(self, *filters):
        output, _ = self.invoke(main.main, ["builds", "--json", *self.config_args, *filters])
        return json.loads(output)

    def test_fast_war_and_dist_remain_queryable_after_tar_deletion(self):
        war = self.root / "hisq.war"
        war.write_bytes(b"war")
        dist = self.root / "dist.zip"
        with zipfile.ZipFile(dist, "w") as bundle:
            bundle.writestr("dist/index.html", b"hi")
        for index, source in enumerate((war, war, dist)):
            archive = self.root / ("delivery-{}.tar".format(index))
            args = [str(source), "hisq", "1.0.0", "-o", str(archive), "--quiet", *self.config_args]
            if index == 1:
                args += ["-t", "company/hisq:custom"]
            if index == 0:
                self.invoke(fast.main, args)
            else:
                self.invoke(main.main, ["fast", *args])
            if index == 2:
                digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            archive.unlink()
        result = self.query("--project", "hisq")
        self.assertEqual(Path(result["directory"]), self.ledger)
        rows = result["builds"]
        self.assertEqual([row["tag"] for row in rows],
                         ["hisq_front:1.0.0", "company/hisq:custom", "hisq_back:1.0.0"])
        self.assertEqual([row["packageType"] for row in rows], ["dist", "war", "war"])
        self.assertTrue(all(row["profile"] == "tomcat" and row["version"] == "1.0.0" for row in rows))
        self.assertEqual(rows[0]["artifacts"][0]["sha256"], digest)
        self.assertEqual(Path(rows[0]["artifacts"][0]["path"]), archive)
        self.assertEqual(self.query("--project", "other")["builds"], [])
        self.assertEqual(self.query("--limit", "1")["builds"], rows[:1])
        self.assertEqual(self.query("--tag", "company/hisq:custom")["builds"], rows[1:2])

    def build_args(self, name):
        context = self.root / "context"
        context.mkdir(exist_ok=True)
        (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        output = self.root / name
        return output, ["build", str(context), "-t", "example/app:1", "-o", str(output),
                        "--format", "both", "--offline", "--no-cache", "--quiet", *self.config_args]

    def test_build_both_records_one_attempt_and_check_does_not_record(self):
        output, args = self.build_args("app.tar")
        self.invoke(main.main, ["build", str(self.root / "context"), "--check"])
        self.assertEqual(self.query()["builds"], [])
        self.assertFalse(self.ledger.exists())
        self.invoke(main.main, args)
        self.invoke(main.main, args, status=1)
        row, = self.query()["builds"]
        self.assertEqual(row["mode"], "build")
        self.assertEqual([(item["format"], Path(item["path"])) for item in row["artifacts"]],
                         [("docker", output), ("oci", self.root / "app.oci.tar")])

    def test_write_failure_rolls_back_both_formats_and_can_retry(self):
        output, args = self.build_args("app.tar")
        self.ledger.parent.mkdir()
        self.ledger.write_text("not a directory", encoding="utf-8")
        self.invoke(main.main, args, status=1)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "app.oci.tar").exists())
        self.ledger.unlink()
        self.assertEqual(self.query()["builds"], [])
        self.invoke(main.main, [*args, "--no-build-records"])
        self.assertTrue(output.is_file())
        self.assertEqual(self.query()["builds"], [])

    def test_concurrent_records_do_not_overwrite_same_tag(self):
        output = self.root / "existing.tar"
        output.write_bytes(b"already verified")

        def write(_):
            return build_records.record_build(self.ledger, "app:1", "linux/amd64", "docker", output)

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(write, range(32)))
        self.assertEqual(len(set(records)), 32)
        self.assertEqual(len(self.query("--tag", "app:1")["builds"]), 32)
        self.assertEqual(set(self.ledger.iterdir()), set(records))

    def test_fast_record_failure_rolls_back_published_archives(self):
        war = self.root / "hisq.war"
        war.write_bytes(b"war")
        output = self.root / "fast.tar"
        self.ledger.parent.mkdir()
        self.ledger.write_text("not a directory", encoding="utf-8")
        self.invoke(fast.main, [str(war), "hisq", "1.0.0", "-o", str(output),
                               "--format", "both", "--quiet", *self.config_args], status=1)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "fast.oci.tar").exists())
