"""从可见文件树和已识别的包数据库生成 SPDX 2.3 清单。"""

import hashlib
import io
import json
import tarfile
import zipfile
from urllib.parse import quote

from errors import ArchiveError
from reproducible import timestamp


def canonical_json(value):
    """稳定排序元数据键并保留末尾换行，保证相同内容有相同摘要。"""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _file_checksums(rootfs, path):
    # RootFSIndex 应用层时已固定硬链接的内容来源；
    # 原目标路径可能已被覆盖，不能再按该路径计算 SBOM 摘要。
    entry = rootfs.entries[path]
    if entry.kind not in ("file", "hardlink") or not entry.layer:
        raise ArchiveError("Cannot inventory image file: /" + path)
    first, second = hashlib.sha1(), hashlib.sha256()
    with tarfile.open(entry.layer, "r:") as archive:
        member = archive.getmember(entry.member)
        stream = archive.extractfile(member)
        if stream is None:
            raise ArchiveError("Unreadable image file: /" + path)
        with stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                first.update(block)
                second.update(block)
    return first.hexdigest(), second.hexdigest()


def _small_file(rootfs, path, limit):
    if rootfs.kind(path) not in ("file", "hardlink"):
        return None
    return rootfs.read_file(path, limit=limit)


def _paragraphs(text):
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith((" ", "\t")):
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if fields:
            yield fields


def _detected_packages(rootfs):
    found = {}

    def add(kind, name, version, architecture=""):
        if not name or not version:
            return
        key = kind, name.lower(), version, architecture
        found[key] = key

    data = _small_file(rootfs, "var/lib/dpkg/status", 64 * 1024 * 1024)
    if data is not None:
        for item in _paragraphs(data.decode("utf-8", "replace")):
            if item.get("Status") == "install ok installed":
                add("deb", item.get("Package"), item.get("Version"),
                    item.get("Architecture", ""))

    data = _small_file(rootfs, "lib/apk/db/installed", 64 * 1024 * 1024)
    if data is not None:
        for block in data.decode("utf-8", "replace").split("\n\n"):
            fields = {}
            for line in block.splitlines():
                if len(line) >= 3 and line[1] == ":":
                    fields[line[0]] = line[2:]
            add("apk", fields.get("P"), fields.get("V"), fields.get("A", ""))

    for path, entry in sorted(rootfs.entries.items()):
        if entry.kind != "file" or not path.endswith(".dist-info/METADATA"):
            continue
        data = _small_file(rootfs, path, 1024 * 1024)
        if data is None:
            continue
        fields = next(_paragraphs(data.decode("utf-8", "replace")), {})
        add("pypi", fields.get("Name"), fields.get("Version"))

    def scan_java(data, depth=0):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in sorted(archive.infolist(), key=lambda value: value.filename):
                    name = member.filename
                    if (name.startswith("META-INF/maven/") and
                            name.endswith("/pom.properties") and member.file_size <= 64 * 1024):
                        with archive.open(member) as stream:
                            properties = stream.read(64 * 1024 + 1).decode("utf-8", "replace")
                        values = {}
                        for line in properties.splitlines():
                            if "=" in line:
                                key, value = line.split("=", 1)
                                values[key.strip()] = value.strip()
                        if values.get("groupId") and values.get("artifactId"):
                            add("maven", values["groupId"] + ":" + values["artifactId"],
                                values.get("version"))
                    elif depth == 0 and name.lower().endswith(".jar") and member.file_size <= 64 * 1024 * 1024:
                        with archive.open(member) as stream:
                            nested = stream.read(64 * 1024 * 1024 + 1)
                        if len(nested) <= 64 * 1024 * 1024:
                            scan_java(nested, 1)
        except (zipfile.BadZipFile, RuntimeError, OSError):
            pass

    for path, entry in sorted(rootfs.entries.items()):
        if entry.kind != "file" or not path.lower().endswith((".war", ".jar", ".ear")):
            continue
        try:
            data = _small_file(rootfs, path, 128 * 1024 * 1024)
        except ArchiveError:
            continue
        if data is not None:
            scan_java(data)
    return sorted(found.values())


def create_sbom(rootfs, config, tag, epoch):
    """为最终可见文件及已识别的软件包生成 SPDX 2.3 文档。"""
    files = []
    relationships = [{"spdxElementId": "SPDXRef-DOCUMENT",
                      "relationshipType": "DESCRIBES",
                      "relatedSpdxElement": "SPDXRef-Image"}]
    sha1_values = []
    for path, entry in sorted(rootfs.entries.items()):
        if entry.kind not in ("file", "hardlink"):
            continue
        sha1, sha256 = _file_checksums(rootfs, path)
        identity = "SPDXRef-File-" + hashlib.sha256(path.encode("utf-8")).hexdigest()
        files.append({"SPDXID": identity, "fileName": "./" + path,
                      "checksums": [{"algorithm": "SHA1", "checksumValue": sha1},
                                    {"algorithm": "SHA256", "checksumValue": sha256}],
                      "licenseConcluded": "NOASSERTION",
                      "licenseInfoInFiles": ["NOASSERTION"],
                      "copyrightText": "NOASSERTION"})
        sha1_values.append(sha1)
        relationships.append({"spdxElementId": "SPDXRef-Image",
                              "relationshipType": "CONTAINS",
                              "relatedSpdxElement": identity})
    verification_code = hashlib.sha1("".join(sorted(sha1_values)).encode("ascii")).hexdigest()
    packages = [{"SPDXID": "SPDXRef-Image", "name": tag,
                 "downloadLocation": "NOASSERTION", "filesAnalyzed": True,
                 "packageVerificationCode": {"packageVerificationCodeValue": verification_code},
                 "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
                 "copyrightText": "NOASSERTION"}]
    for kind, name, version, architecture in _detected_packages(rootfs):
        identity = "SPDXRef-Package-" + hashlib.sha256(
            (kind + "\0" + name + "\0" + version + "\0" + architecture).encode()).hexdigest()
        if kind == "maven":
            group, artifact = name.split(":", 1)
            purl = "pkg:maven/{}/{}@{}".format(quote(group, safe=""),
                quote(artifact, safe=""), quote(version, safe=""))
        else:
            purl = "pkg:{}/{}@{}".format(kind, quote(name, safe=""), quote(version, safe=""))
        if architecture:
            purl += "?arch=" + quote(architecture, safe="")
        packages.append({"SPDXID": identity, "name": name, "versionInfo": version,
                         "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
                         "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
                         "copyrightText": "NOASSERTION",
                         "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER",
                                           "referenceType": "purl",
                                           "referenceLocator": purl}]})
        relationships.append({"spdxElementId": "SPDXRef-Image",
                              "relationshipType": "CONTAINS",
                              "relatedSpdxElement": identity})
    scope = hashlib.sha256(canonical_json([tag, config.get("rootfs", {}).get("diff_ids", [])])).hexdigest()
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT", "name": "SBOM for " + tag,
            "documentNamespace": "https://pyimagebuilder.invalid/spdxdocs/" + scope,
            "creationInfo": {"creators": ["Tool: pyimagebuilder-phase9"],
                             "created": timestamp(epoch)},
            "documentDescribes": ["SPDXRef-Image"],
            "comment": "Inventory of visible regular files and detected dpkg/apk/Python/Maven packages; "
                       "symlinks and special files are not enumerated. Maven detection is limited "
                       "to pom.properties in WAR/JAR/EAR and one nested JAR level within size caps.",
            "packages": packages, "files": files, "relationships": relationships}
