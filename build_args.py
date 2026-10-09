"""解析 Dockerfile ARG，并处理构建作用域内的变量替换。"""

import re
import fnmatch

from errors import BuildError
from compat import remove_suffix


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _pattern_replace(value, modifier, variables, depth):
    if modifier.startswith("//"):
        all_matches, content = True, modifier[2:]
    else:
        all_matches, content = False, modifier[1:]
    pattern, separator, replacement = content.partition("/")
    if not pattern:
        raise BuildError("Empty Dockerfile variable replacement pattern")
    expression = fnmatch.translate(pattern)
    expression = remove_suffix(expression, r"\Z")
    replacement = expand(replacement, variables, depth + 1) if separator else ""
    return re.sub(expression, lambda _match: replacement, value,
                  count=0 if all_matches else 1)


def _pattern_trim(value, modifier):
    if modifier.startswith("##"):
        pattern, longest, front = modifier[2:], True, True
    elif modifier.startswith("#"):
        pattern, longest, front = modifier[1:], False, True
    elif modifier.startswith("%%"):
        pattern, longest, front = modifier[2:], True, False
    else:
        pattern, longest, front = modifier[1:], False, False
    if not pattern:
        raise BuildError("Empty Dockerfile variable trim pattern")
    positions = range(len(value), -1, -1) if longest else range(len(value) + 1)
    for position in positions:
        candidate = value[:position] if front else value[len(value) - position:]
        if fnmatch.fnmatchcase(candidate, pattern):
            return value[position:] if front else value[:len(value) - position]
    return value


def expand(value, variables, _depth=0):
    """按当前 ARG/ENV 作用域展开变量；不支持的修饰符或过深嵌套直接报错。"""
    if _depth > 16:
        raise BuildError("Dockerfile variable expansion is too deeply nested")
    result = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value) and value[index + 1] == "$":
            result.append("$")
            index += 2
            continue
        if value[index] != "$":
            result.append(value[index])
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "{":
            depth = 1
            end = index + 2
            while end < len(value) and depth:
                if value[end] == "{":
                    depth += 1
                elif value[end] == "}":
                    depth -= 1
                end += 1
            if depth:
                raise BuildError("Unclosed Dockerfile variable expression")
            body = value[index + 2:end - 1]
            match = NAME.match(body)
            if match is None:
                raise BuildError("Invalid Dockerfile variable expression: ${" + body + "}")
            name = match.group()
            modifier = body[match.end():]
            present = name in variables
            current = str(variables.get(name, ""))
            if not modifier:
                replacement = current
            elif modifier.startswith("/"):
                replacement = _pattern_replace(current, modifier, variables, _depth)
            elif modifier.startswith(("#", "%")):
                replacement = _pattern_trim(current, modifier)
            else:
                operator = next((prefix for prefix in (":-", ":+", "-", "+")
                                 if modifier.startswith(prefix)), None)
                if operator is None:
                    raise BuildError("Unsupported Dockerfile variable expression: ${" + body + "}")
                word = expand(modifier[len(operator):], variables, _depth + 1)
                if operator == ":-":
                    replacement = current if current else word
                elif operator == "-":
                    replacement = current if present else word
                elif operator == ":+":
                    replacement = word if current else ""
                else:
                    replacement = word if present else ""
            result.append(replacement)
            index = end
            continue
        match = NAME.match(value, index + 1)
        if match:
            result.append(str(variables.get(match.group(), "")))
            index = match.end()
        else:
            result.append("$")
            index += 1
    return "".join(result)


def arguments(items):
    """将重复的 CLI 构建参数整理为 ARG 声明可使用的值。"""
    result = {}
    for item in items or ():
        key, separator, value = item.partition("=")
        if not NAME.fullmatch(key) or not separator:
            raise BuildError("--build-arg must be NAME=value")
        result[key] = value
    return result
