"""离线生成 SLSA provenance、DSSE 签名，并核对镜像和附属证明文件。"""

import argparse
import base64
import binascii
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import ed25519
from compat import unlink_missing
from errors import BuildError
from image_reader import sha256_file
from sbom import canonical_json


PAYLOAD_TYPE = "application/vnd.in-toto+json"


def artifact_paths(output):
    """根据镜像归档路径推导 SBOM、provenance 和签名的附属文件路径。"""
    output = Path(output)
    return (output.with_name(output.name + suffix) for suffix in
            (".spdx.json", ".provenance.json", ".dsse.json"))


def context_digest(context):
    """将上下文的路径、权限、链接目标和内容规范化后计算整体摘要。"""
    root = Path(context)
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        details = path.lstat()
        mode = stat.S_IMODE(details.st_mode)
        if path.is_symlink():
            entries.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_file():
            entries.append([relative, "file", mode, sha256_file(path)])
        elif path.is_dir():
            entries.append([relative, "directory", mode])
        else:
            raise BuildError("Unsupported context entry in provenance: " + relative)
    return hashlib.sha256(canonical_json(entries)).hexdigest()


def create_provenance(products, sbom_bytes, dockerfile, context, bases,
                      tag, target_stage, run_enabled, run_network, epoch,
                      run_sandbox="legacy", target_platform="linux/amd64",
                      allow_emulated_run=False):
    """生成 SLSA 来源证明，记录输出摘要以及实际使用的构建输入。"""
    subjects = [{"name": kind + ":image.tar",
                 "digest": {"sha256": sha256_file(partial)}}
                for kind, partial, destination in products]
    dependencies = [{"uri": "dockerfile:Dockerfile",
                     "digest": {"sha256": sha256_file(dockerfile)}},
                    {"uri": "context:build-context",
                     "digest": {"sha256": context_digest(context)}}]
    for reference, path in sorted(bases.items()):
        digest = path["sha256"] if isinstance(path, dict) else sha256_file(path)
        dependencies.append({"uri": "base:" + reference,
                             "digest": {"sha256": digest}})
    return {"_type": "https://in-toto.io/Statement/v1",
            "subject": subjects,
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://pyimagebuilder.invalid/build/v1",
                    "externalParameters": {
                        "tag": tag, "targetStage": target_stage,
                        "runEnabled": run_enabled, "runNetwork": run_network,
                        "runSandbox": run_sandbox,
                        "targetPlatform": target_platform,
                        "allowEmulatedRun": allow_emulated_run,
                        "sourceDateEpoch": epoch,
                        "formats": [item[0] for item in products]},
                    "internalParameters": {},
                    "resolvedDependencies": dependencies},
                "runDetails": {
                    "builder": {"id": "https://pyimagebuilder.invalid/builder/phase9"},
                    "metadata": {},
                    "byproducts": [{"name": "sbom:spdx-2.3",
                                    "digest": {"sha256": hashlib.sha256(sbom_bytes).hexdigest()}}]}}}


def pae(payload_type, payload):
    """按 DSSE PAE 规则编码载荷类型和原始字节，避免签名内容存在歧义。"""
    kind = payload_type.encode("utf-8")
    return (b"DSSEv1 " + str(len(kind)).encode() + b" " + kind + b" " +
            str(len(payload)).encode() + b" " + payload)


def sign_envelope(payload, seed):
    """使用本地 Ed25519 私钥种子签署 provenance，并返回 DSSE 信封。"""
    public = ed25519.public_key(seed)
    signature = ed25519.sign(seed, pae(PAYLOAD_TYPE, payload))
    return {"payloadType": PAYLOAD_TYPE,
            "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [{"keyid": hashlib.sha256(public).hexdigest(),
                            "sig": base64.b64encode(signature).decode("ascii")}]}


def _read_key(path, size, label):
    try:
        raw = bytes.fromhex(Path(path).read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise BuildError("Cannot read " + label + " (expected hex): " + str(exc)) from exc
    if len(raw) != size:
        raise BuildError(label + " must contain exactly {} bytes in hex".format(size))
    return raw


def read_private_key(path):
    """读取以十六进制保存的 32 字节 Ed25519 私钥种子。"""
    return _read_key(path, 32, "Ed25519 private seed")


def read_public_key(path):
    """读取以十六进制保存的 32 字节 Ed25519 公钥。"""
    return _read_key(path, 32, "Ed25519 public key")


def keygen(private_path, public_path):
    """离线生成来源证明签名所需的密钥对。"""
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path.resolve() == public_path.resolve():
        raise BuildError("Private and public key paths must differ")
    seed = os.urandom(32)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, raw, mode in ((private_path, seed, 0o600),
                                (public_path, ed25519.public_key(seed), 0o644)):
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(raw.hex() + "\n")
    except Exception:
        for path in created:
            unlink_missing(path)
        raise


def _load_json(path, label):
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise BuildError("Invalid {}: {}".format(label, exc)) from exc


def verify_bundle(public_key, provenance_path, envelope_path, sbom_path,
                  docker_archive=None, oci_archive=None):
    """核对镜像归档、证明文件和签名是否一致；这里的 bundle 指附属证明集合。"""
    if docker_archive is None and oci_archive is None:
        raise BuildError("Provide at least one archive to verify")
    public = read_public_key(public_key)
    provenance_bytes = Path(provenance_path).read_bytes()
    envelope = _load_json(envelope_path, "DSSE envelope")
    if (not isinstance(envelope, dict) or envelope.get("payloadType") != PAYLOAD_TYPE or
            not isinstance(envelope.get("signatures"), list)):
        raise BuildError("Invalid DSSE payload type or signatures")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
    except (KeyError, ValueError, TypeError, binascii.Error) as exc:
        raise BuildError("Invalid DSSE payload") from exc
    if payload != provenance_bytes:
        raise BuildError("DSSE payload differs from provenance file")
    keyid = hashlib.sha256(public).hexdigest()
    valid = False
    for item in envelope["signatures"]:
        if not isinstance(item, dict) or item.get("keyid") != keyid:
            continue
        try:
            signature = base64.b64decode(item["sig"], validate=True)
        except (KeyError, ValueError, TypeError, binascii.Error):
            continue
        valid |= ed25519.verify(public, pae(PAYLOAD_TYPE, payload), signature)
    if not valid:
        raise BuildError("No valid signature from the supplied trusted public key")
    try:
        provenance = json.loads(payload)
        sbom = _load_json(sbom_path, "SBOM")
        if provenance["_type"] != "https://in-toto.io/Statement/v1" or \
                provenance["predicateType"] != "https://slsa.dev/provenance/v1" or \
                sbom["spdxVersion"] != "SPDX-2.3":
            raise BuildError("Unexpected provenance or SBOM format")
        subjects = {item["name"]: item["digest"]["sha256"]
                    for item in provenance["subject"]}
        if len(subjects) != len(provenance["subject"]):
            raise BuildError("Duplicate provenance subject")
        supplied = {}
        for kind, path in (("docker", docker_archive), ("oci", oci_archive)):
            if path is not None:
                path = Path(path)
                supplied[kind + ":image.tar"] = sha256_file(path)
        if subjects != supplied:
            raise BuildError("Archive digest or subject name mismatch")
        byproducts = provenance["predicate"]["runDetails"]["byproducts"]
        expected_sbom = [{"name": "sbom:spdx-2.3",
                          "digest": {"sha256": sha256_file(sbom_path)}}]
        if byproducts != expected_sbom:
            raise BuildError("SBOM digest mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise BuildError("Malformed provenance or SBOM: " + str(exc)) from exc
    return True


def main(argv=None):
    """独立脚本入口：生成密钥或验证镜像的附属证明文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    generate = actions.add_parser("keygen", help="Generate an offline Ed25519 key pair")
    generate.add_argument("--private-key", type=Path, required=True)
    generate.add_argument("--public-key", type=Path, required=True)
    verify = actions.add_parser("verify", help="Verify signed provenance and all delivered artifacts")
    for name in ("public-key", "provenance", "envelope", "sbom"):
        verify.add_argument("--" + name, type=Path, required=True)
    verify.add_argument("--docker-archive", type=Path)
    verify.add_argument("--oci-archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "keygen":
            keygen(args.private_key, args.public_key)
            print("Created public key {}".format(args.public_key))
        else:
            verify_bundle(args.public_key, args.provenance, args.envelope, args.sbom,
                          args.docker_archive, args.oci_archive)
            print("Signature, archive digest(s), and SBOM digest verified")
        return 0
    except (BuildError, OSError) as exc:
        print("Attestation failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
