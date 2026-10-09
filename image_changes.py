"""将 import/flatten 的 --change 应用为配置变更，不执行构建或创建目录。"""

import copy
import re

from build_args import expand
from dockerfile_parser import parse, valid_stop_signal
from errors import BuildError
from image_config import ImageConfig
from layer import container_path


CHANGE_COMMANDS = {"CMD", "ENTRYPOINT", "ENV", "EXPOSE", "HEALTHCHECK", "LABEL",
                   "ONBUILD", "STOPSIGNAL", "USER", "VOLUME", "WORKDIR"}


def parse_changes(changes):
    """先校验全部配置指令；拒绝 RUN/COPY 等及夹带的 FROM，避免部分应用。"""
    instructions = []
    for change in changes or ():
        if not isinstance(change, str) or not change.strip():
            raise BuildError("--change requires a nonempty Dockerfile instruction")
        parsed = parse("FROM scratch\n" + change)[1:]
        if not parsed or any(item.op not in CHANGE_COMMANDS for item in parsed):
            raise BuildError("--change supports only: " + ", ".join(sorted(CHANGE_COMMANDS)))
        instructions.extend(parsed)
    return instructions


def apply_changes(config, instructions):
    """按顺序应用元数据指令，沿用 Dockerfile 解析和变量展开约定。

    WORKDIR/VOLUME 只写配置；ONBUILD 只登记触发器，不在导入时运行。
    无变更时返回原值的拷贝，不把 ImageConfig 的内部补全写回配置。
    """
    result = copy.deepcopy(config)
    if not instructions:
        return result
    state = ImageConfig(config)
    cmd_set = False
    for instruction in instructions:
        op, value = instruction.op, instruction.value
        variables = dict(item.split("=", 1) for item in state.runtime.get("Env") or [])
        if op == "ENV":
            state.set_env([(key, expand(item, variables)) for key, item in value])
        elif op == "LABEL":
            state.set_labels([(key, expand(item, variables)) for key, item in value])
        elif op in ("CMD", "ENTRYPOINT"):
            command = ((state.runtime.get("Shell") or ["/bin/sh", "-c"]) + [value.value]
                       if value.shell_form else list(value.value))
            state.set_command("Cmd" if op == "CMD" else "Entrypoint", command)
            if op == "CMD":
                cmd_set = True
            elif not cmd_set:
                state.runtime["Cmd"] = None
        elif op == "WORKDIR":
            state.set_workdir(container_path(expand(value, variables),
                                            state.runtime.get("WorkingDir") or "/"))
        elif op == "USER":
            user = expand(value, variables)
            if not user or user.startswith(":") or user.endswith(":") or user.count(":") > 1:
                raise BuildError("Invalid USER after --change expansion")
            state.runtime["User"] = user
        elif op == "VOLUME":
            paths = [expand(item, variables) for item in value]
            if any(not path.startswith("/") for path in paths):
                raise BuildError("--change VOLUME requires absolute paths")
            state.set_volumes([container_path(path) for path in paths])
        elif op == "EXPOSE":
            ports = []
            for item in value:
                port = expand(item, variables)
                match = re.fullmatch(r"([0-9]{1,5})(?:/(tcp|udp|sctp))?", port, re.I)
                if match is None or not 1 <= int(match.group(1)) <= 65535:
                    raise BuildError("Invalid EXPOSE after --change expansion")
                ports.append("{}/{}".format(int(match.group(1)), (match.group(2) or "tcp").lower()))
            state.set_exposed(ports)
        elif op == "HEALTHCHECK":
            if value.command is None:
                state.runtime["Healthcheck"] = {"Test": ["NONE"]}
            else:
                command = value.command
                test = (["CMD-SHELL", command.value] if command.shell_form
                        else ["CMD"] + list(command.value))
                state.runtime["Healthcheck"] = dict(value.options, Test=test)
        elif op == "STOPSIGNAL":
            signal = expand(value, variables).upper()
            if not valid_stop_signal(signal):
                raise BuildError("Invalid STOPSIGNAL after --change expansion")
            state.runtime["StopSignal"] = signal
        elif op == "ONBUILD":
            state.runtime.setdefault("OnBuild", []).append(value.raw)
        else:
            raise BuildError("Invalid --change instruction: " + op)
    result["config"] = copy.deepcopy(state.runtime)
    return result
