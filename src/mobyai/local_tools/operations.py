"""Bounded filesystem operations. No shell, package installation or user-code imports."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from mobyai.execution.errors import WorkspaceError
from mobyai.execution.workspace import Workspace

MAX_FILE_BYTES = 2_000_000
MAX_PACKAGES = 100


def read_text(workspace: Workspace, path: str | Path) -> str:
    target = workspace.resolve(path, must_exist=True)
    if not target.is_file():
        raise WorkspaceError(f"Not a file: {path}")
    with target.open("rb") as source:
        body = source.read(MAX_FILE_BYTES + 1)
    if len(body) > MAX_FILE_BYTES:
        raise WorkspaceError(f"File exceeds local tool limit of {MAX_FILE_BYTES} bytes: {path}")
    return body.decode("utf-8")


def file_info(workspace: Workspace, path: str) -> dict:
    raw = Path(path).expanduser()
    entry = raw if raw.is_absolute() else workspace.root / raw
    # Resolve the parent, not the final symlink: inspecting a venv interpreter
    # link must not grant permission to read its external target.
    entry = (workspace.resolve(entry) if entry in workspace.allowed_roots()
             else workspace.resolve(entry.parent) / entry.name)
    if entry.is_symlink():
        target = entry.resolve(strict=False)
        accessible = workspace.contains(target)
        return {"path": path, "exists": True, "kind": "symlink",
                "target": os.readlink(entry), "target_within_workspace": accessible,
                "target_exists": target.exists() if accessible else None}
    target = workspace.resolve(entry)
    if not target.exists():
        return {"path": path, "exists": False, "kind": "missing"}
    result = {"path": path, "exists": True,
              "kind": "directory" if target.is_dir() else "file" if target.is_file() else "special"}
    if target.is_file():
        result["size_bytes"] = target.stat().st_size
        if result["size_bytes"] <= MAX_FILE_BYTES:
            with target.open("rb") as source:
                body = source.read(MAX_FILE_BYTES + 1)
            if len(body) <= MAX_FILE_BYTES:
                result["sha256"] = hashlib.sha256(body).hexdigest()
    return result


def create_directory(workspace: Workspace, path: str) -> dict:
    target = workspace.resolve(path)
    existed = target.is_dir()
    target.mkdir(parents=True, exist_ok=True)
    return {"path": workspace.relative(target), "created": not existed}


def edit_file(workspace: Workspace, path: str, old_text: str, new_text: str,
              expected_sha256: str | None = None) -> dict:
    target = workspace.resolve(path, must_exist=True)
    original = read_text(workspace, target)
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise WorkspaceError("File changed since inspection; read it again before editing")
    matches = original.count(old_text)
    if not old_text or matches != 1:
        raise WorkspaceError(f"old_text must match exactly once (found {matches}); read the file and include unique context")
    updated = original.replace(old_text, new_text, 1)
    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        raise WorkspaceError("Edited file exceeds local tool size limit")
    # Atomic replacement preserves line endings and executable permissions.
    mode = target.stat().st_mode & 0o777
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.moby-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated.encode("utf-8"))
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        if read_text(workspace, target) != original:
            raise WorkspaceError("File changed during edit; read it again before retrying")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": workspace.relative(target), "replacements": 1,
            "changed": original != updated,
            "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest()}


def _json_object(workspace, path):
    value = json.loads(read_text(workspace, path))
    if not isinstance(value, dict):
        raise WorkspaceError(f"Expected a JSON object: {path}")
    return value


def _limited_mapping(value, limit=MAX_PACKAGES):
    if not isinstance(value, dict):
        return {}
    return dict(list(value.items())[:limit])


def inspect_node_project(workspace: Workspace, path: str = ".") -> dict:
    root = workspace.resolve(path, must_exist=True)
    manifest = _json_object(workspace, root / "package.json")
    result = {"path": path, "name": manifest.get("name"),
              "package_manager": manifest.get("packageManager"),
              "scripts": _limited_mapping(manifest.get("scripts")),
              "dependencies": _limited_mapping(manifest.get("dependencies")),
              "dev_dependencies": _limited_mapping(manifest.get("devDependencies")),
              "lockfiles": [], "installed": {}, "warnings": [],
              "evidence": "Workspace files only; no install, runtime execution or peer-range resolution performed."}
    for name in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb"):
        info = file_info(workspace, str(root / name))
        if info["exists"]:
            result["lockfiles"].append(name)
    dependencies = {**result["dependencies"], **result["dev_dependencies"]}
    for name in list(dependencies)[:MAX_PACKAGES]:
        if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
            result["warnings"].append(f"Invalid dependency name: {name}")
            continue
        try:
            installed = _json_object(workspace, root / "node_modules" / name / "package.json")
            result["installed"][name] = {"version": installed.get("version"),
                "peer_dependencies": _limited_mapping(installed.get("peerDependencies"), 30)}
        except (WorkspaceError, OSError, ValueError) as exc:
            result["installed"][name] = {"version": None, "note": str(exc)}
    if any(isinstance(manifest.get(key), dict) and len(manifest[key]) > MAX_PACKAGES
           for key in ("scripts", "dependencies", "devDependencies")) or len(dependencies) > MAX_PACKAGES:
        result["warnings"].append(f"Inspection limited to {MAX_PACKAGES} entries per section and installed packages")
    return result


def inspect_python_environment(workspace: Workspace, path: str = ".venv", packages=None) -> dict:
    info = file_info(workspace, path)
    result = {"path": path, "exists": info["exists"], "configuration": {},
              "interpreters": [], "installed": {}, "warnings": [],
              "evidence": "Workspace metadata only; interpreter links are not executed. Importability and sandbox runtime are unverified."}
    if not info["exists"]:
        return result
    root = workspace.resolve(path, must_exist=True)
    try:
        config = read_text(workspace, root / "pyvenv.cfg")
        result["configuration"] = dict(line.split("=", 1) for line in config.splitlines() if "=" in line)
        result["configuration"] = {key.strip(): value.strip() for key, value in result["configuration"].items()}
    except (WorkspaceError, OSError) as exc:
        result["warnings"].append(str(exc))
    for name in ("bin/python", "bin/python3", "Scripts/python.exe"):
        entry = file_info(workspace, str(root / name))
        if entry["exists"]:
            result["interpreters"].append(entry)
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    wanted = {normalize(name) for name in packages or []}
    found = {}
    scanned = 0
    # Bound scanning even when the user filters by a nonexistent distribution.
    for pattern in ("lib/python*/site-packages/*.dist-info/METADATA", "Lib/site-packages/*.dist-info/METADATA"):
        for metadata in root.glob(pattern):
            scanned += 1
            if scanned > 2000:
                result["warnings"].append("Distribution scan limited to 2000 entries")
                break
            try:
                fields = {}
                for line in read_text(workspace, metadata).splitlines():
                    if not line:
                        break
                    if line.startswith(("Name: ", "Version: ")):
                        key, value = line.split(": ", 1)
                        fields[key] = value
                name = fields.get("Name")
                if name and (not wanted or normalize(name) in wanted):
                    if len(found) < MAX_PACKAGES:
                        found[normalize(name)] = {"name": name, "version": fields.get("Version")}
                    else:
                        result["warnings"].append(f"Installed package output limited to {MAX_PACKAGES}")
                        break
            except (WorkspaceError, OSError, ValueError) as exc:
                if len(result["warnings"]) < 10:
                    result["warnings"].append(str(exc))
    result["installed"] = dict(sorted(found.items()))
    result["not_found"] = sorted(wanted - found.keys())
    return result


OPERATIONS = {function.__name__: function for function in (
    file_info, create_directory, edit_file, inspect_node_project, inspect_python_environment,
)}
