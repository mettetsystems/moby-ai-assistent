"""Extract a public Python surface. The Worker does not write this."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import ast
import hashlib

from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.models.contract import ModuleContract
from mobyai.state.repository import StateStore


def extract_python_contract(path: str, source: str) -> dict[str, Any] | None:
    """Return functions/classes payload, or None if the source is not parseable."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(node.name):
                functions.append(_function_payload(node, skip_first=False))
        elif isinstance(node, ast.ClassDef):
            if _is_public(node.name):
                classes.append(_class_payload(node))
    return {"functions": functions, "classes": classes}


def render_contract(path: str, payload: dict[str, Any]) -> str:
    lines = [path]
    for item in payload.get("functions") or []:
        lines.append(_format_function(item, indent=""))
    for item in payload.get("classes") or []:
        bases = item.get("bases") or []
        suffix = f"({', '.join(bases)})" if bases else ""
        marker = " schema" if item.get("schema") else ""
        lines.append(f"class {item.get('name', '')}{suffix}{marker}")
        init_args = item.get("init_args") or []
        if init_args:
            lines.append(f"  __init__({', '.join(init_args)})")
        for field in item.get("fields") or []:
            lines.append(f"  {field}")
        for method in item.get("methods") or []:
            lines.append(_format_function(method, indent="  "))
    if len(lines) == 1:
        lines.append("(no public surface)")
    return "\n".join(lines)


async def record_module_contracts(
    store: StateStore,
    filesystem: WorkspaceFilesystem,
    *,
    project_id: str,
    task_id: str,
    paths: Sequence[str],
    now: datetime,
) -> list[ModuleContract]:
    """Upsert contracts for jailed Python files. Syntax errors leave the prior row."""
    recorded: list[ModuleContract] = []
    for path in _unique_python_paths(paths):
        try:
            source = await filesystem.read_file(path)
        except (WorkspaceError, OSError, UnicodeDecodeError):
            continue
        payload = extract_python_contract(path, source)
        if payload is None:
            continue
        payload["source_sha256"] = source_digest(source)
        contract = ModuleContract(
            project_id=project_id,
            path=path,
            task_id=task_id,
            rendered=render_contract(path, payload),
            payload=payload,
            updated_at=now,
        )
        await store.contracts.upsert(contract)
        recorded.append(contract)
    return recorded


def _unique_python_paths(paths: Sequence[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        cleaned = raw.strip().replace("\\", "/")
        if not cleaned:
            continue
        candidate = Path(cleaned)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        posix = candidate.as_posix()
        if not posix.endswith(".py") or posix in seen:
            continue
        seen.add(posix)
        ordered.append(posix)
    return ordered


def _is_public(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _function_payload(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, skip_first: bool
) -> dict[str, Any]:
    return {
        "name": node.name,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": [_decorator_name(d) for d in node.decorator_list],
        "args": _format_args(node.args, skip_first=skip_first),
        "returns": _unparse(node.returns),
    }


def _class_payload(node: ast.ClassDef) -> dict[str, Any]:
    bases = [_unparse(base) for base in node.bases]
    schema = _is_schema_class(node, bases)
    init_args: list[str] = []
    methods: list[dict[str, Any]] = []
    fields: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name == "__init__":
                init_args = _format_args(child.args, skip_first=True)
            elif _is_public(child.name):
                methods.append(_function_payload(child, skip_first=not any(
                    _decorator_name(d).split(".")[-1] == "staticmethod" for d in child.decorator_list
                )))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            if _is_public(child.target.id):
                annotation = _unparse(child.annotation)
                fields.append(
                    f"{child.target.id}: {annotation}" if annotation else child.target.id
                )
    return {
        "name": node.name,
        "bases": [item for item in bases if item],
        "init_args": init_args,
        "methods": methods,
        "fields": fields,
        "schema": schema,
    }


def _is_schema_class(node: ast.ClassDef, bases: list[str]) -> bool:
    for deco in node.decorator_list:
        name = _decorator_name(deco)
        if name in {"dataclass", "dataclasses.dataclass"} or name.endswith(".dataclass"):
            return True
    return any(base == "BaseModel" or base.endswith(".BaseModel") for base in bases)


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _unparse(node.func)
    return _unparse(node)


def _format_args(args: ast.arguments, *, skip_first: bool) -> list[str]:
    parts: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        if skip_first and index == 0:
            continue
        value = _format_arg(arg)
        if default is not None:
            value += " = " + ast.unparse(default)
        parts.append(value)
        if index + 1 == len(args.posonlyargs):
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + _format_arg(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        value = _format_arg(arg)
        if default is not None:
            value += " = " + ast.unparse(default)
        parts.append(value)
    if args.kwarg is not None:
        parts.append("**" + _format_arg(args.kwarg))
    return parts


def _format_arg(arg: ast.arg) -> str:
    annotation = _unparse(arg.annotation)
    if annotation:
        return f"{arg.arg}: {annotation}"
    return arg.arg


def _format_function(item: dict[str, Any], *, indent: str) -> str:
    args = ", ".join(item.get("args") or [])
    returns = item.get("returns") or ""
    suffix = f" -> {returns}" if returns else ""
    prefix = "async " if item.get("async") else ""
    decorators = "".join(f"{indent}@{d}\n" for d in item.get("decorators", []))
    return decorators + f"{indent}{prefix}def {item.get('name', '')}({args}){suffix}"


def _unparse(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (TypeError, ValueError):
        return ""


def source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def contract_is_current(contract: ModuleContract, source: str) -> bool:
    return contract.payload.get("source_sha256") == source_digest(source)
