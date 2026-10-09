"""静态检查 Dockerfile；不执行构建、不拉取镜像。"""

from pathlib import Path
from urllib.parse import urlparse

from dockerfile_parser import parse
from errors import BuildError, UnsupportedInstruction
from image_store import external_bases
from layer import context_sources


def check(dockerfile, context, platform="linux/amd64", target=None, build_args=None):
    """检查支持的语法和可解析的本地 COPY/ADD 输入，不执行 RUN 或访问仓库。"""
    dockerfile, context = Path(dockerfile).resolve(), Path(context).resolve()
    if not dockerfile.is_file():
        raise BuildError("Dockerfile not found: " + str(dockerfile))
    if not context.is_dir():
        raise BuildError("Build context not found: " + str(context))
    try:
        instructions = parse(dockerfile.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise BuildError("Dockerfile must be UTF-8") from exc
    if not any(item.op == "FROM" for item in instructions):
        raise BuildError("Dockerfile needs a FROM instruction")
    for item in instructions:
        if item.op == "FROM":
            break
        if item.op != "ARG":
            raise BuildError("Line {}: {} appears before first FROM".format(item.line, item.op))
    ignore = context / ".dockerignore"
    if ignore.is_file():
        try:
            rules = ignore.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise BuildError(".dockerignore must be UTF-8") from exc
        if any(line.strip() and not line.lstrip().startswith("#") for line in rules):
            raise UnsupportedInstruction(".dockerignore rules are not implemented; refusing an incorrect context")
    bases = external_bases(dockerfile, platform, target, build_args or {})
    headers = [item for item in instructions if item.op == "FROM"]
    if target is None:
        target_index = len(headers) - 1
    elif str(target).isdecimal():
        target_index = int(target)
    else:
        target_index = next(index for index, item in enumerate(headers)
                            if item.value.alias and item.value.alias.lower() == str(target).lower())
    missing = []
    dynamic = []
    checked = []
    stage_index = -1
    for instruction in instructions:
        if instruction.op == "FROM":
            stage_index += 1
            if stage_index > target_index:
                break
        if instruction.op not in ("COPY", "ADD"):
            continue
        transfer = instruction.value
        if transfer.from_stage is not None or transfer.inline is not None:
            continue
        for source in transfer.sources:
            if "$" in source:
                dynamic.append({"line": instruction.line, "source": source})
                continue
            if instruction.op == "ADD" and urlparse(source).scheme in ("http", "https", "git"):
                dynamic.append({"line": instruction.line, "source": source})
                continue
            matches = context_sources(context, source)
            if not matches:
                missing.append({"line": instruction.line, "source": source})
            else:
                checked.append({"line": instruction.line, "source": source, "matches": len(matches)})
    if missing:
        raise BuildError("Missing local COPY/ADD sources: " + ", ".join(
            "line {}: {}".format(item["line"], item["source"]) for item in missing))
    return {"status": "ok", "mode": "static", "dockerfile": str(dockerfile),
            "context": str(context), "instructions": len(instructions),
            "stages": len(headers), "target_stage_index": target_index,
            "external_bases": {key: sorted(value) for key, value in bases.items()},
            "local_sources_checked": checked, "unresolved_sources": dynamic,
            "note": "No base image, RUN, remote ADD, or expanded source was validated."}
