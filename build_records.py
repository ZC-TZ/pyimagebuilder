"""记录本机 CLI 打包产物；台账独立于镜像库和 tar 文件。"""

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from compat import unlink_missing
from errors import BuildError
from file_resources import fdopen_owned
from settings import load_settings


RECORD_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.json\Z")


def records_directory(settings):
    configured = os.environ.get("PYIMAGEBUILDER_BUILD_RECORDS_DIR") or settings.get("buildRecordsDir")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".pyimagebuilder" / "build-records"


def record_build(directory, tag, platform, image_format, output, *, mode="build",
                 oci_output=None, digest=None, size=None, project=None, version=None,
                 package_type=None, profile=None):
    """每次构建独占一份记录；并发写入互不覆盖，读取者只看到已提交的 JSON。"""
    now = datetime.now(timezone.utc)
    identity = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    primary = Path(output)
    artifacts = [{
        "format": "oci" if image_format == "oci" else "docker",
        "path": str(primary), "bytes": primary.stat().st_size if size is None else size,
    }]
    if digest is not None:
        artifacts[0]["sha256"] = digest
    if image_format == "both":
        secondary = Path(oci_output)
        artifacts.append({"format": "oci", "path": str(secondary), "bytes": secondary.stat().st_size})
    record = {"schemaVersion": 1, "id": identity,
              "completedAt": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
              "mode": mode, "tag": tag, "platform": platform, "artifacts": artifacts}
    if mode == "fast":
        record.update(project=project, version=version, packageType=package_type, profile=profile)

    target = directory / (identity + ".json")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix=".build-record-", suffix=".part", dir=directory)
    try:
        with fdopen_owned(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, target)
    finally:
        unlink_missing(pending)
    return target


def list_builds(directory, project=None, tag=None, limit=None):
    """按打包完成时间倒序查询，忽略仍在写入的临时文件。"""
    try:
        paths = sorted((p for p in directory.iterdir() if RECORD_NAME.fullmatch(p.name)),
                       reverse=True)
    except FileNotFoundError:
        return []
    rows = []
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(row, dict) or row.get("schemaVersion") != 1 or
                    row.get("id") != path.stem or
                    not all(isinstance(row.get(key), str)
                            for key in ("completedAt", "tag", "platform", "mode")) or
                    not isinstance(row.get("artifacts"), list) or not row["artifacts"] or
                    not all(isinstance(item, dict) and isinstance(item.get("path"), str) and
                            item.get("format") in ("docker", "oci") for item in row["artifacts"])):
                raise ValueError("Invalid build record")
        except (OSError, ValueError) as exc:
            raise BuildError("Cannot read build record {}: {}".format(path, exc)) from exc
        if project is not None and row.get("project") != project:
            continue
        if tag is not None and row["tag"] != tag:
            continue
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog="main.py builds", description="List locally recorded image builds")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--project", help="Filter by Fast project name")
    parser.add_argument("--tag", help="Filter by exact image tag")
    parser.add_argument("--limit", type=int, help="Maximum number of matching builds, newest first")
    parser.add_argument("--json", action="store_true", help="Print machine-readable records")
    parser.add_argument("--web", action="store_true", help="Start the local build workbench on 127.0.0.1")
    parser.add_argument("--port", type=int, help="Local web port (default 8765; 0 picks a free port)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.web and args.json:
        parser.error("--web and --json cannot be combined")
    if args.port is not None and (not args.web or not 0 <= args.port <= 65535):
        parser.error("--port requires --web and must be between 0 and 65535")
    try:
        settings, config = load_settings(args.config)
        directory = records_directory(settings)
        if args.web:
            from build_records_web import serve
            return serve(directory, 8765 if args.port is None else args.port,
                         args.project, args.tag, args.limit, settings=settings, config=config)
        rows = list_builds(directory, args.project, args.tag, args.limit)
        if args.json:
            print(json.dumps({"directory": str(directory), "builds": rows}, ensure_ascii=True, indent=2))
        else:
            print("Build records: {}".format(directory))
            if not rows:
                print("No matching builds.")
            for row in rows:
                mode = "fast/" + row["profile"] if isinstance(row.get("profile"), str) else row["mode"]
                print("{}  {}  {}  {}".format(row["completedAt"], row["tag"], row["platform"], mode))
                for item in row["artifacts"]:
                    print("  [{}] {}".format(item["format"], item["path"]))
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print("Cannot list builds: {}".format(exc), file=sys.stderr)
        return 1
