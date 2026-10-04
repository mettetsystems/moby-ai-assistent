"""Validate direct process commands without silently invoking a shell."""
import re
import shlex
from pathlib import Path


class CommandSyntaxError(ValueError):
    """A command is incompatible with finite, direct process execution."""


class ServiceCommandError(CommandSyntaxError):
    """An open-ended server was submitted as a finite operation."""


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


def validate_argv(parts: list[str], *, allow_service: bool = False) -> None:
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise _invalid("expected a non-empty argv list of strings")
    if parts[0] in _BUILTINS:
        raise _invalid(f"{parts[0]!r} is a shell builtin, not an executable")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        raise _invalid("a leading environment assignment is not an executable")
    for part in parts[1:]:
        if part in _OPERATORS or re.fullmatch(r"\d*(?:>>?|<<?|>&|<&)\S*", part):
            raise _invalid(f"unsupported shell operator {part!r}")
    from mobyai.execution.service_check import COMMAND, parse_check
    if parts[0] == COMMAND:
        try:
            options = parse_check(parts[1:])
            if options["command"][0] == COMMAND:
                raise ValueError("nested service checks are not supported")
            validate_argv(options["command"], allow_service=True)
        except ValueError as exc:
            raise ServiceCommandError(f"Invalid service lifecycle: {exc}") from exc
    elif not allow_service and _foreground_service(parts):
        raise ServiceCommandError(
            "Invalid service lifecycle: a foreground server does not finish. "
            "Do not retry it with a longer timeout or launch it in a separate tool call. "
            "Use a finite test that owns startup, readiness, assertions and cleanup, or "
            "mobyai-service-check --url http://127.0.0.1:8000/healthz "
            "--startup-seconds 10 --observe-seconds 30 -- <server argv>. "
            "Choose an unused local port, preserve the required endpoint/status and "
            "observation duration, and correct saved test_commands through the Planner."
        )


def _foreground_service(parts: list[str]) -> bool:
    """Recognize known server entry points; never guess from arbitrary filenames."""
    program = Path(parts[0]).name
    args = parts[1:]
    # Unwrap common Python flags without mistaking a -c body or script arguments
    # for an interpreter module invocation.
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program):
        while args and args[0] in {"-u", "-B", "-E", "-I", "-s", "-S"}:
            args = args[1:]
        if len(args) >= 2 and args[0] == "-m":
            program, args = args[1], args[2:]
        elif args and Path(args[0]).name == "manage.py":
            program, args = "django-admin", args[1:]
        else:
            return False
    if any(flag in args for flag in {"--help", "-h", "--version"}):
        return False
    if program in {"uvicorn", "hypercorn", "gunicorn", "http.server"}:
        # Gunicorn has legitimate finite configuration-check modes.
        return not (program == "gunicorn" and any(flag in args for flag in {"--check-config", "--print-config"}))
    return ((program == "flask" and "run" in args)
            or (program == "django-admin" and "runserver" in args))


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
