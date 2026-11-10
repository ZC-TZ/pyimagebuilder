"""镜像瘦身规则：只操作虚拟文件树，保护优先，并记录可复核的删除决策。"""

import fnmatch
import posixpath
import re
import tarfile
from collections import deque
from pathlib import Path

from errors import BuildError
from file_resources import open_regular_file
from path_policy import archive_path

MAX_RULE_BYTES = 1024 * 1024
MAX_RULES = 2048
DEFAULT_PROTECTIONS = {
    "etc/ssl": "TLS configuration", "etc/pki": "certificate store",
    "etc/ca-certificates": "CA configuration", "usr/share/ca-certificates": "CA sources",
    "usr/share/zoneinfo": "timezone database", "etc/localtime": "local timezone",
    "etc/timezone": "timezone name", "etc/passwd": "runtime accounts", "etc/group": "runtime groups",
    "etc/nsswitch.conf": "name service configuration", "etc/resolv.conf": "DNS configuration",
    "lib*/libnss_*.so*": "name service libraries", "usr/lib*/libnss_*.so*": "name service libraries",
    "lib*/libresolv.so*": "DNS libraries", "usr/lib*/libresolv.so*": "DNS libraries",
}
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
ABSOLUTE = re.compile(r"/[^\s'\";|&<>()]+")
DEFAULT_LIMIT = 2_000_000_000


def normalize_pattern(value):
    """规则采用 POSIX rootfs 路径；拒绝根、父跳转、反斜杠和控制字符。"""
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096 or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BuildError("Invalid slim path pattern")
    try:
        return archive_path(value.strip().lstrip("/"), allow_root=False)
    except (ValueError, RuntimeError) as exc:
        raise BuildError("Invalid slim path pattern: " + repr(value)) from exc


def load_patterns(values=(), files=()):
    """规则文件有读取上限，避免一次预览把任意大文件载入内存。"""
    if isinstance(values, (str, bytes)):
        raise BuildError("Slim rules must be a sequence of patterns")
    result = list(values)
    total = 0
    for filename in files:
        with open_regular_file(Path(filename).resolve()) as stream:
            raw = stream.read(MAX_RULE_BYTES + 1)
        total += len(raw)
        if total > MAX_RULE_BYTES:
            raise BuildError("Slim rule files exceed 1 MiB")
        for line in raw.decode("utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                result.append(line.strip())
    if len(result) > MAX_RULES:
        raise BuildError("Too many slim rules")
    return list(dict.fromkeys(normalize_pattern(value) for value in result))


def _parents(path):
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        yield path


def matching(entries, patterns):
    """目录命中包含后代；glob 按 fnmatchcase，大小写敏感且 * 可跨越斜杠。"""
    direct = {name: [p for p in patterns if fnmatch.fnmatchcase(name, p)] for name in entries}
    direct = {name: rules for name, rules in direct.items() if rules}
    result = {}
    for name in entries:
        rules = set(direct.get(name, ()))
        for parent in _parents(name):
            if entries[parent].kind == "dir":
                rules.update(direct.get(parent, ()))
        if rules:
            result[name] = sorted(rules)
    return result


def resolve_trace(entries, path):
    """按容器语义解析链接，记录中途链接；.. 在镜像根处夹紧，不访问宿主。"""
    pending, resolved, trace = deque(path.split("/")), [], set()
    hops = 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        name = "/".join(resolved + [part])
        entry = entries.get(name)
        if entry is None:
            return None, trace
        trace.add(name)
        if entry.kind == "symlink":
            hops += 1
            if hops > 40:
                return None, trace
            if entry.linkname.startswith("/"):
                resolved = []
            pending.extendleft(reversed(entry.linkname.split("/")))
        else:
            if pending and entry.kind != "dir":
                return None, trace
            resolved.append(part)
    return "/".join(resolved), trace


def runtime_rules(config, entries):
    """保守推断常见运行路径，不把它当成动态依赖分析或应用可运行的证明。"""
    rules = dict(DEFAULT_PROTECTIONS)
    runtime = config.get("config")
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, dict):
        raise BuildError("Image runtime config must be an object for automatic protection")
    environment = runtime.get("Env")
    if environment is None:
        environment = []
    if not isinstance(environment, list) or any(not isinstance(item, str) for item in environment):
        raise BuildError("Image Env must be an array of strings")
    env = dict(item.split("=", 1) for item in environment
               if isinstance(item, str) and "=" in item)

    def expand(value):
        for _ in range(4):
            changed = VARIABLE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), value)
            if changed == value:
                break
            value = changed
        return value

    def add(value, reason):
        if not isinstance(value, str):
            return
        value = expand(value)
        if not value.startswith("/") or "$" in value:
            return
        try:
            path = normalize_pattern(value)
        except (BuildError, ValueError, RuntimeError):
            return
        rules[path] = reason

    workdir = runtime.get("WorkingDir")
    if workdir in (None, ""):
        workdir = "/"
    if not isinstance(workdir, str):
        raise BuildError("Image WorkingDir must be a string")
    add(workdir, "runtime WorkingDir")
    volumes = runtime.get("Volumes")
    if volumes is None:
        volumes = {}
    if not isinstance(volumes, dict):
        raise BuildError("Image Volumes must be an object")
    for path in volumes:
        add(path, "runtime Volume")
    for key, value in env.items():
        if key == "HOME" or key.endswith("_HOME"):
            add(value, "runtime environment " + key)
        elif key in ("CLASSPATH", "LD_LIBRARY_PATH", "PYTHONPATH"):
            for part in value.split(":"):
                add(part, "runtime environment " + key)
    fields = [runtime.get("Entrypoint"), runtime.get("Cmd"), runtime.get("Shell")]
    health = runtime.get("Healthcheck")
    if health is None:
        health = {}
    if not isinstance(health, dict):
        raise BuildError("Image Healthcheck must be an object")
    fields.append(health.get("Test"))
    for field in fields:
        if field is not None and not isinstance(field, (str, list)):
            raise BuildError("Image runtime command must be a string or array")
        if isinstance(field, list) and any(not isinstance(item, str) for item in field):
            raise BuildError("Image runtime command entries must be strings")
        for text in field if isinstance(field, list) else [field]:
            if isinstance(text, str):
                for path in ABSOLUTE.findall(expand(text)):
                    add(path, "runtime command")
    command = runtime.get("Entrypoint") or runtime.get("Cmd") or []
    if isinstance(command, list) and command and isinstance(command[0], str):
        first = expand(command[0])
        if "/" in first:
            add(first if first.startswith("/") else posixpath.join(workdir, first), "runtime executable")
        else:
            for directory in expand(env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")).split(":"):
                candidate = posixpath.join(directory if directory.startswith("/") else posixpath.join(workdir, directory), first)
                target, _ = resolve_trace(entries, candidate)
                if target in entries:
                    add(candidate, "resolved runtime executable")
                    break
    return rules


def protected_paths(entries, rules):
    """保护匹配项、所需父目录及链接最终目标；只对明确保护的目录展开后代。"""
    protected, reasons, queue = set(), {}, deque()

    def retain(name, reason, subtree=False):
        names = [name]
        if subtree:
            names += [p for p in entries if not name or p.startswith(name + "/")]
        for path in names:
            if path not in entries:
                continue
            reasons.setdefault(path, set()).add(reason)
            if path not in protected:
                protected.add(path)
                queue.append(path)

    matches = matching(entries, list(rules))
    for name, patterns in matches.items():
        for pattern in patterns:
            retain(name, rules[pattern])
    # 字面路径可能经中途符号链接到达，不能只匹配 index 中的表面名字。
    for pattern, reason in rules.items():
        if not any(c in pattern for c in "*?["):
            target, trace = resolve_trace(entries, pattern)
            for name in trace:
                retain(name, reason)
            if target is not None:
                retain(target, reason, subtree=not target or entries[target].kind == "dir")
    while queue:
        name = queue.popleft()
        for parent in _parents(name):
            retain(parent, "ancestor of protected path")
        if entries[name].kind == "symlink":
            target, trace = resolve_trace(entries, name)
            for path in trace:
                retain(path, "protected symlink chain")
            if target is not None:
                retain(target, "protected symlink target", subtree=not target or entries[target].kind == "dir")
    return protected, reasons


def payload_sizes(index):
    """读取固定 backing member 的大小，硬链接头的 size=0 不能当成实际载荷。"""
    needed = {}
    for entry in index.entries.values():
        if entry.kind in ("file", "hardlink"):
            needed.setdefault(entry.layer, {}).setdefault(entry.member, set()).add(id(entry.inode))
    sizes = {}
    for layer, members in needed.items():
        with tarfile.open(layer, "r:") as archive:
            for member in archive:
                for inode in members.get(member.name, ()):
                    if not member.isfile():
                        raise BuildError("Slim backing member is not a regular file")
                    sizes[inode] = member.size
    if len(sizes) != len({id(e.inode) for e in index.entries.values() if e.kind in ("file", "hardlink")}):
        raise BuildError("Missing slim backing member")
    return sizes


def analyze(index, sizes, largest=20):
    files = [(name, sizes[id(entry.inode)]) for name, entry in index.entries.items()
             if entry.kind in ("file", "hardlink")]
    return {"paths": len(index.entries), "file_paths": len(files),
            "unique_payload_bytes": sum(sizes[key] for key in {id(e.inode) for e in index.entries.values()
                                                               if e.kind in ("file", "hardlink")}),
            "largest_files": [{"path": "/" + name, "bytes": size}
                              for name, size in sorted(files, key=lambda item: (-item[1], item[0]))[:largest]]}


def prune(index, deletes, keeps, config, auto_protect=True, largest=20):
    """先计算全部决策再修改索引；保留项可以重选硬链接载荷锚点。"""
    sizes = payload_sizes(index)
    before = analyze(index, sizes, largest)
    auto = runtime_rules(config, index.entries) if auto_protect else {}
    rules = dict(auto)
    rules.update((pattern, "manual keep: " + pattern) for pattern in keeps)
    protected, reasons = protected_paths(index.entries, rules)
    candidates = matching(index.entries, deletes)
    victims = set(candidates) - protected

    def row(name, blocked=False):
        entry = index.entries[name]
        result = {"path": "/" + name, "kind": entry.kind, "matched_by": candidates[name],
                  "bytes": sizes.get(id(entry.inode), 0) if entry.kind in ("file", "hardlink") else 0}
        if blocked:
            result["reasons"] = sorted(reasons.get(name, ()))
        return result

    report = {"before": before, "removed_paths": [row(p) for p in sorted(victims)],
              "protected_paths": [row(p, True) for p in sorted(set(candidates) & protected)],
              "auto_rules": [{"pattern": p, "reason": reason} for p, reason in sorted(auto.items())],
              "delete_patterns": deletes, "keep_patterns": keeps, "auto_protect": auto_protect}
    for name in victims:
        del index.entries[name]
    report["after"] = analyze(index, sizes, largest)
    report["removed_unique_bytes"] = before["unique_payload_bytes"] - report["after"]["unique_payload_bytes"]
    return report
