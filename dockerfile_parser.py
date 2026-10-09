"""严格解析已支持的 Dockerfile 语义；不支持的行为明确报错。"""

import json
import posixpath
import re
import shlex
from dataclasses import dataclass

from errors import BuildError, UnsupportedInstruction


@dataclass(frozen=True)
class Instruction:
    """保存类型化指令、原文和源行号，供构建执行与错误定位使用。"""
    op: str
    value: object
    raw: str
    line: int


@dataclass(frozen=True)
class Command:
    """区分 shell 与 JSON exec 形式，保留命令的运行语义。"""
    shell_form: bool
    value: object


@dataclass(frozen=True)
class Transfer:
    """保存 COPY/ADD 的源、目标和解析后的 flag，供后续解析上下文。"""
    sources: tuple
    destination: str
    chown: str = None
    chmod: int = None
    from_stage: str = None
    exclude: tuple = ()
    link: bool = False
    parents: bool = False
    unpack: bool = None
    checksum: str = None
    inline: str = None
    inline_expand: bool = True


@dataclass(frozen=True)
class From:
    """保存基础镜像引用、可选阶段别名和平台覆盖值。"""
    reference: str
    alias: str = None
    platform: str = None


@dataclass(frozen=True)
class Run:
    """保存 RUN 命令及临时挂载、网络和安全选项。"""
    command: Command
    mounts: tuple
    network: str = None
    security: str = None

    @property
    def shell_form(self):
        """保留 RUN 命令是否需要由当前 SHELL 解释的信息。"""
        return self.command.shell_form

    @property
    def value(self):
        """暴露解析后的 RUN 命令内容，供构建执行器消费。"""
        return self.command.value


@dataclass(frozen=True)
class Healthcheck:
    """保存健康检查命令及已验证的时间、重试参数。"""
    command: Command = None
    options: tuple = ()


SUPPORTED = {"FROM", "COPY", "ADD", "ENV", "ARG", "WORKDIR", "CMD", "ENTRYPOINT",
             "RUN", "USER", "SHELL", "EXPOSE", "LABEL", "VOLUME", "HEALTHCHECK",
             "STOPSIGNAL", "ONBUILD", "MAINTAINER"}

LINUX_SIGNALS = {"SIGHUP", "SIGINT", "SIGQUIT", "SIGILL", "SIGTRAP", "SIGABRT",
                 "SIGBUS", "SIGFPE", "SIGKILL", "SIGUSR1", "SIGSEGV", "SIGUSR2",
                 "SIGPIPE", "SIGALRM", "SIGTERM", "SIGSTKFLT", "SIGCHLD",
                 "SIGCONT", "SIGSTOP", "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGURG",
                 "SIGXCPU", "SIGXFSZ", "SIGVTALRM", "SIGPROF", "SIGWINCH",
                 "SIGIO", "SIGPWR", "SIGSYS"}


def valid_stop_signal(value):
    """规范化 Linux 停止信号，拒绝无法用于镜像配置的名称或编号。"""
    return (value.upper() in LINUX_SIGNALS or
            (value.isdecimal() and 1 <= int(value) <= 64))


def _logical_lines(text):
    pending = ""
    start = 0
    escape = "\\"
    seen_instruction = False
    lines = text.splitlines()
    position = 0
    while position < len(lines):
        number = position + 1
        raw = lines[position]
        position += 1
        stripped = raw.strip()
        if not stripped or (stripped.startswith("#") and not pending):
            if stripped.startswith("#") and not seen_instruction:
                directive = re.match(r"#\s*(syntax|escape|check)\s*=\s*(.*)$", stripped, re.I)
                if directive:
                    name, setting = directive.group(1).lower(), directive.group(2).strip()
                    if name == "escape":
                        if setting not in ("\\", "`"):
                            raise BuildError("Line {}: # escape must be backslash or backtick".format(number))
                        escape = setting
                    elif name == "syntax":
                        if not re.fullmatch(r"docker/dockerfile(?::[A-Za-z0-9_.-]+)?", setting):
                            raise UnsupportedInstruction("Line {}: custom Dockerfile frontend is unsupported".format(number))
                    elif not re.fullmatch(r"skip=[A-Za-z0-9_,;-]+", setting, re.I):
                        raise UnsupportedInstruction("Line {}: # check directive is unsupported: {}".format(number, setting))
            continue
        if not pending:
            start = number
        continuation = stripped.endswith(escape)
        pending += stripped[:-1].rstrip() + " " if continuation else stripped
        if not continuation:
            logical = pending.strip()
            seen_instruction = True
            markers = _heredoc_markers(logical)
            if markers and logical.split(None, 1)[0].upper() in ("RUN", "COPY", "ADD"):
                if len(markers) > 1 and logical.split(None, 1)[0].upper() != "RUN":
                    raise UnsupportedInstruction("Line {}: COPY/ADD accepts one heredoc".format(start))
                body = []
                for marker in markers:
                    delimiter = marker.group(3)
                    while position < len(lines):
                        candidate = lines[position]
                        position += 1
                        comparison = candidate.lstrip("\t") if marker.group(1) else candidate
                        body.append(comparison)
                        if comparison == delimiter:
                            break
                    else:
                        raise BuildError("Line {}: unterminated heredoc {}".format(start, delimiter))
                logical += "\n" + "\n".join(body) + "\n"
            yield start, logical
            pending = ""
    if pending:
        raise BuildError("Dockerfile ends with a continuation at line {}".format(start))


def _words(value, line):
    try:
        if value.lstrip().startswith("["):
            words = json.loads(value)
            if not isinstance(words, list) or not all(isinstance(x, str) for x in words):
                raise BuildError("Line {}: expected JSON array of strings".format(line))
            return words
        return shlex.split(value)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BuildError("Line {}: invalid instruction syntax: {}".format(line, exc)) from exc


def _expanded_words(value, line):
    """去除词语法时保留字面量美元符，避免后续展开误读单引号或转义中的变量。"""
    if value.lstrip().startswith("["):
        # JSON 自己解释转义；不能再套用 shell 引号规则改变数组里的原始字符串。
        return _words(value, line)
    marker = "__PYIMAGEBUILDER_LITERAL_DOLLAR__"
    while marker in value:
        marker += "_"
    protected = []
    quote = None
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and quote != "'" and index + 1 < len(value):
            following = value[index + 1]
            protected.append(marker if following == "$" else character + following)
            index += 2
            continue
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        protected.append(marker if character == "$" and quote == "'" else character)
        index += 1
    # 展开器已经支持 \$；保护标记只用于穿过 shlex，不会进入归档路径。
    return [word.replace(marker, "\\$") for word in _words("".join(protected), line)]


def _duration(value, line):
    units = {"ns": 1, "us": 1000, "ms": 1000000, "s": 1000000000,
             "m": 60 * 1000000000, "h": 3600 * 1000000000}
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise BuildError("Line {}: invalid HEALTHCHECK duration: {}".format(line, value))
    result = sum(int(float(number) * units[unit]) for number, unit in parts)
    if result < 0 or result > 2**63 - 1:
        raise BuildError("Line {}: HEALTHCHECK duration out of range".format(line))
    return result


def _heredoc_interpreter(command, script, line):
    words = _words(command, line)
    if not words:
        raise BuildError("Line {}: empty heredoc interpreter".format(line))
    program = posixpath.basename(words[1] if posixpath.basename(words[0]) == "env" and len(words) > 1
                                 else words[0])
    if program in ("sh", "bash", "ash", "dash", "zsh") or re.fullmatch(r"python(?:[23](?:\.[0-9]+)?)?", program):
        return Command(False, words + ["-c", script])
    if len(words) >= 2 and posixpath.basename(words[0]) == "busybox" and words[1] == "sh":
        return Command(False, words + ["-c", script])
    raise UnsupportedInstruction("Line {}: heredoc interpreter needs an explicit supported shell or Python".format(line))


def _heredoc_markers(header):
    markers = []
    quote = None
    index = 0
    while index < len(header):
        char = header[index]
        if char == "\\" and index + 1 < len(header):
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if header.startswith("<<", index):
            marker = re.match(r"<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", header[index:])
            if marker:
                markers.append(marker)
                index += marker.end()
                continue
        index += 1
    return markers


def parse(text):
    """将受支持的 Dockerfile 语法转为类型化指令；遇到未实现语义时拒绝猜测。"""
    instructions = []
    for line_number, raw in _logical_lines(text):
        match = re.match(r"^([A-Za-z]+)\s+(.+)$", raw, re.DOTALL)
        if not match:
            raise BuildError("Line {}: malformed Dockerfile instruction".format(line_number))
        op, value = match.group(1).upper(), match.group(2).strip(" \t")
        if op not in SUPPORTED:
            raise UnsupportedInstruction("Line {}: {} is not implemented".format(line_number, op))
        if op == "FROM":
            words = value.split()
            from_platform = None
            if words and words[0].startswith("--platform="):
                from_platform = words.pop(0).partition("=")[2]
                if from_platform not in ("linux/amd64", "linux/arm64") and "$" not in from_platform:
                    raise UnsupportedInstruction("Line {}: unsupported FROM platform".format(line_number))
            if (len(words) not in (1, 3) or words[0].startswith("--") or
                    (len(words) == 3 and (words[1].upper() != "AS" or
                     not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", words[2])))):
                raise UnsupportedInstruction("Line {}: FROM requires a literal image and optional AS name".format(line_number))
            parsed = From(words[0], words[2] if len(words) == 3 else None, from_platform)
        elif op in ("COPY", "ADD"):
            flags = {}
            header, separator, heredoc_body = value.partition("\n")
            rest = header
            while rest.startswith("--"):
                option_match = re.match(r"^(--\S+)\s+(.+)$", rest)
                if option_match is None:
                    raise UnsupportedInstruction("Line {}: malformed {} option".format(line_number, op))
                token, rest = option_match.groups()
                key, equals, argument = token.partition("=")
                allowed = (("--chown", "--chmod", "--from", "--exclude", "--link", "--parents")
                           if op == "COPY" else
                           ("--chown", "--chmod", "--exclude", "--link", "--unpack", "--checksum"))
                if key not in allowed or (key in flags and key != "--exclude"):
                    raise UnsupportedInstruction("Line {}: unsupported {} option: {}".format(line_number, op, token))
                if key in ("--link", "--parents") and not equals:
                    argument = "true"
                elif not equals or not argument:
                    raise UnsupportedInstruction("Line {}: {} option must be literal".format(line_number, op))
                if key == "--exclude":
                    flags.setdefault(key, []).append(argument)
                else:
                    flags[key] = argument
                rest = rest.lstrip()
            if "--chmod" in flags and "$" not in flags["--chmod"] and not re.fullmatch(r"[0-7]{3,4}", flags["--chmod"]):
                raise UnsupportedInstruction("Line {}: only octal --chmod is supported".format(line_number))
            if "--chown" in flags and (flags["--chown"].startswith(":") or
                                         flags["--chown"].endswith(":") or
                                         flags["--chown"].count(":") > 1):
                raise BuildError("Line {}: invalid --chown value".format(line_number))
            words = _expanded_words(rest, line_number)
            if len(words) < 2:
                raise UnsupportedInstruction("Line {}: {} needs literal source(s) and destination".format(line_number, op))
            for key in ("--link", "--parents", "--unpack"):
                if key in flags and flags[key].lower() not in ("true", "false"):
                    raise BuildError("Line {}: {} requires true or false".format(line_number, key))
            if "--checksum" in flags and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", flags["--checksum"]):
                raise UnsupportedInstruction("Line {}: only HTTP SHA-256 checksum is supported".format(line_number))
            inline = None
            inline_expand = True
            if separator:
                if op != "COPY" or any(key in flags for key in ("--from", "--parents", "--exclude", "--unpack", "--checksum")):
                    raise UnsupportedInstruction("Line {}: unsupported heredoc transfer flags".format(line_number))
                if len(words) != 2:
                    raise BuildError("Line {}: heredoc COPY/ADD requires one destination".format(line_number))
                marker = re.search(r"<<-?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", rest)
                if marker is None:
                    raise BuildError("Line {}: invalid COPY/ADD heredoc".format(line_number))
                inline = "".join(heredoc_body.splitlines(keepends=True)[:-1])
                inline_expand = not bool(marker.group(1))
            parsed = Transfer(tuple(words[:-1]), words[-1], flags.get("--chown"),
                              (int(flags["--chmod"], 8) if "$" not in flags["--chmod"] else flags["--chmod"])
                              if "--chmod" in flags else None,
                              flags.get("--from"), tuple(flags.get("--exclude", ())),
                              flags.get("--link", "false").lower() == "true",
                              flags.get("--parents", "false").lower() == "true",
                              (flags["--unpack"].lower() == "true") if "--unpack" in flags else None,
                              flags.get("--checksum"), inline, inline_expand)
        elif op == "ENV":
            words = _expanded_words(value, line_number)
            if not words or any("=" not in word or word.startswith("=") for word in words):
                raise UnsupportedInstruction("Line {}: only literal ENV KEY=value is supported".format(line_number))
            parsed = [word.split("=", 1) for word in words]
        elif op == "LABEL":
            words = _expanded_words(value, line_number)
            if not words or any("=" not in word or word.startswith("=") for word in words):
                raise UnsupportedInstruction("Line {}: only literal LABEL KEY=value is supported".format(line_number))
            parsed = [word.split("=", 1) for word in words]
        elif op == "ARG":
            declarations = _expanded_words(value, line_number)
            if not declarations or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:=.*)?", item)
                                       for item in declarations):
                raise BuildError("Line {}: ARG requires NAME or NAME=default".format(line_number))
            parsed = tuple(item.partition("=") for item in declarations)
        elif op == "EXPOSE":
            words = _expanded_words(value, line_number)
            if not words:
                raise BuildError("Line {}: EXPOSE needs a port".format(line_number))
            parsed = []
            for word in words:
                match_port = re.fullmatch(r"([0-9]{1,5})(?:/(tcp|udp|sctp))?", word, re.IGNORECASE)
                if "$" in word:
                    parsed.append(word)
                    continue
                if match_port is None or not 1 <= int(match_port.group(1)) <= 65535:
                    raise UnsupportedInstruction("Line {}: unsupported EXPOSE port: {}".format(line_number, word))
                parsed.append("{}/{}".format(int(match_port.group(1)),
                                              (match_port.group(2) or "tcp").lower()))
        elif op == "VOLUME":
            words = _expanded_words(value, line_number)
            if not words or any(not word.startswith("/") and "$" not in word for word in words):
                raise UnsupportedInstruction("Line {}: VOLUME requires literal absolute paths".format(line_number))
            parsed = words
        elif op == "WORKDIR":
            words = _expanded_words(value, line_number)
            if len(words) != 1:
                raise UnsupportedInstruction("Line {}: WORKDIR must be one path".format(line_number))
            # 使用已去除语法引号和转义的路径，避免将它们作为目录名写入镜像。
            parsed = words[0]
        elif op == "USER":
            words = _expanded_words(value, line_number)
            if (len(words) != 1 or words[0].startswith(":") or
                    words[0].endswith(":") or words[0].count(":") > 1):
                raise UnsupportedInstruction("Line {}: USER must be a literal user[:group]".format(line_number))
            parsed = words[0]
        elif op == "SHELL":
            if not value.startswith("["):
                raise UnsupportedInstruction("Line {}: SHELL requires a JSON array".format(line_number))
            parsed = _words(value, line_number)
            if not parsed:
                raise BuildError("Line {}: empty SHELL".format(line_number))
        elif op == "STOPSIGNAL":
            words = _expanded_words(value, line_number)
            if len(words) != 1:
                raise BuildError("Line {}: STOPSIGNAL requires one signal".format(line_number))
            if not valid_stop_signal(words[0]) and "$" not in words[0]:
                raise BuildError("Line {}: invalid STOPSIGNAL".format(line_number))
            # ARG/ENV 名称区分大小写；信号名称由构建器在变量展开后规范化。
            parsed = words[0]
        elif op == "MAINTAINER":
            parsed = value
        elif op == "HEALTHCHECK":
            if value.upper() == "NONE":
                parsed = Healthcheck()
            else:
                options = {}
                remaining = value
                while remaining.startswith("--"):
                    token, gap, remaining = remaining.partition(" ")
                    if not gap:
                        raise BuildError("Line {}: HEALTHCHECK needs CMD".format(line_number))
                    key, equals, argument = token[2:].partition("=")
                    if not equals or key not in ("interval", "timeout", "start-period",
                                                  "start-interval", "retries") or key in options:
                        raise UnsupportedInstruction("Line {}: invalid HEALTHCHECK option: {}".format(line_number, token))
                    if key == "retries":
                        if not argument.isdecimal() or int(argument) < 1:
                            raise BuildError("Line {}: HEALTHCHECK retries must be positive".format(line_number))
                        options["Retries"] = int(argument)
                    else:
                        options[{"interval": "Interval", "timeout": "Timeout",
                                 "start-period": "StartPeriod", "start-interval": "StartInterval"}[key]] = _duration(argument, line_number)
                    remaining = remaining.lstrip()
                if not remaining.upper().startswith("CMD "):
                    raise BuildError("Line {}: HEALTHCHECK requires CMD or NONE".format(line_number))
                command_text = remaining[4:].strip()
                if not command_text:
                    raise BuildError("Line {}: empty HEALTHCHECK CMD".format(line_number))
                command = Command(False, _words(command_text, line_number)) if command_text.startswith("[") else Command(True, command_text)
                parsed = Healthcheck(command, tuple(options.items()))
        elif op == "ONBUILD":
            nested = parse("FROM scratch\n" + value)
            if len(nested) != 2 or nested[1].op in ("FROM", "ONBUILD", "MAINTAINER"):
                raise UnsupportedInstruction("Line {}: invalid ONBUILD instruction".format(line_number))
            parsed = nested[1]
        else:
            mounts = []
            network = security = None
            if op == "RUN":
                while value.startswith("--"):
                    token, separator, value = value.partition(" ")
                    if not separator or not value.strip():
                        raise BuildError("Line {}: RUN flag needs a command".format(line_number))
                    if token.startswith("--mount="):
                        options = {}
                        for part in token[len("--mount="):].split(","):
                            key, equals, argument = part.partition("=")
                            if not equals or not key or not argument or key in options:
                                raise BuildError("Line {}: invalid RUN mount option".format(line_number))
                            options[key] = argument
                        if options.get("type") not in ("cache", "secret"):
                            raise UnsupportedInstruction("Line {}: only cache/secret RUN mounts are supported".format(line_number))
                        mounts.append(options)
                    elif token.startswith("--network=") and network is None:
                        network = token.partition("=")[2]
                        if network not in ("default", "none", "host"):
                            raise BuildError("Line {}: invalid RUN network".format(line_number))
                    elif token.startswith("--security=") and security is None:
                        security = token.partition("=")[2]
                        if security not in ("sandbox", "insecure"):
                            raise BuildError("Line {}: invalid RUN security".format(line_number))
                    else:
                        raise UnsupportedInstruction("Line {}: unsupported RUN flag: {}".format(line_number, token))
                    value = value.lstrip()
            if op == "RUN" and "\n" in value:
                header, _, script = value.partition("\n")
                marker = re.match(r"<<-?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?:\s+(.*))?$", header)
                if marker is None or "<<" in (marker.group(3) or ""):
                    parsed = Command(True, value)
                else:
                    script = "".join(script.splitlines(keepends=True)[:-1])
                    interpreter = marker.group(3)
                    if interpreter:
                        if re.search(r"[><&|;]", interpreter):
                            parsed = Command(True, value)
                        else:
                            try:
                                parsed = _heredoc_interpreter(interpreter, script, line_number)
                            except UnsupportedInstruction:
                                parsed = Command(True, value)
                    elif script.startswith("#!"):
                        shebang = script.splitlines()[0][2:].strip()
                        parsed = _heredoc_interpreter(shebang, script, line_number)
                    else:
                        parsed = Command(True, script)
                parsed = Run(parsed, tuple(mounts), network, security)
                instructions.append(Instruction(op, parsed, raw, line_number))
                continue
            if value.startswith("["):
                words = _words(value, line_number)
                if not words:
                    raise BuildError("Line {}: empty {}".format(line_number, op))
                parsed = Command(False, words)
            else:
                parsed = Command(True, value)
            if op == "RUN":
                parsed = Run(parsed, tuple(mounts), network, security)
        instructions.append(Instruction(op, parsed, raw, line_number))
    if not instructions or not any(item.op == "FROM" for item in instructions):
        raise BuildError("Dockerfile must contain FROM")
    names = set()
    count = 0
    for item in instructions:
        if item.op == "FROM":
            if item.value.alias is not None:
                alias = item.value.alias.lower()
                if alias in names or alias.isdecimal():
                    raise BuildError("Line {}: duplicate or numeric stage name".format(item.line))
                names.add(alias)
            count += 1
        elif count == 0 and item.op != "ARG":
            raise BuildError("Instruction before first FROM")
    return instructions
