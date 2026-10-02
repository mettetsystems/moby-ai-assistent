"""Validate direct process commands without silently invoking a shell."""
import re
import shlex


class CommandSyntaxError(ValueError):
    """A command requires implicit shell interpretation that the runner lacks."""


_BUILTINS = frozenset({"source", ".", "cd", "export", "unset", "alias", "unalias"})
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "<<<", "|&", "&>"})


def _invalid(detail: str) -> CommandSyntaxError:
    return CommandSyntaxError(
        f"Invalid command syntax: {detail}. Commands use argv, not shell syntax. "
        "Use separate commands and the run_command cwd parameter (saved tests run "
        "from the project root). For Python, use .venv/bin/python -m pip or "
        ".venv/bin/python -m pytest directly; activation does not persist between calls. "
        "Correct the command while preserving its validation; do not recreate the venv."
    )


def validate_argv(parts: list[str]) -> None:
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise _invalid("expected a non-empty argv list of strings")
    if parts[0] in _BUILTINS:
        raise _invalid(f"{parts[0]!r} is a shell builtin, not an executable")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        raise _invalid("a leading environment assignment is not an executable")
    for part in parts[1:]:
        if part in _OPERATORS or re.fullmatch(r"\d*(?:>>?|<<?|>&|<&)\S*", part):
            raise _invalid(f"unsupported shell operator {part!r}")


def _check_string_syntax(value: str) -> None:
    # Respect quotes and escapes in e.g. python -c 'print(1 > 0)' and grep 'a|b'.
    # Checking the original string also catches attached operators: pytest&&echo.
    quote = None
    escaped = False
    for char in value.strip():
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "&|;<>\n\r":
            raise _invalid(f"unquoted shell operator {char!r}")


def split_command(value: object) -> list[str]:
    if isinstance(value, str) and value.strip():
        _check_string_syntax(value)
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
    elif isinstance(value, list):
        parts = list(value)
    else:
        raise _invalid("expected an argv list or a non-empty command string")
    validate_argv(parts)
    return parts
