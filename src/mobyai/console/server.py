"""Local operator console. stdlib HTTP; no extra web framework."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mobyai.console.settings import (
    apply_role_settings,
    save_settings_overlay,
    serialize_role_settings,
)
from mobyai.console.snapshot import build_monitor
from mobyai.runtime import Runtime

UI_PATH = Path(__file__).with_name("ui.html")


class ConsoleService:
    def __init__(self, runtime: Runtime, config_path: Path, caller: Any) -> None:
        self.runtime = runtime
        self.config_path = config_path
        self._call = caller

    def settings(self) -> dict[str, Any]:
        return serialize_role_settings(self.runtime.config)

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        apply_role_settings(self.runtime.config, payload)
        overlay = save_settings_overlay(self.config_path, self.runtime.config)
        return {"ok": True, "overlay": str(overlay), "settings": self.settings()}

    def projects(self) -> list[dict[str, Any]]:
        return self._call(self._projects())

    async def _projects(self) -> list[dict[str, Any]]:
        ids = await self.runtime.store.projects.list_ids()
        rows: list[dict[str, Any]] = []
        for project_id in ids:
            project = await self.runtime.store.projects.get(project_id)
            if project is None:
                continue
            rows.append(
                {
                    "project_id": project.project_id,
                    "name": project.name,
                    "state": project.state.value,
                    "current_task_id": project.current_task_id,
                    "last_error": project.last_error,
                }
            )
        return rows

    def monitor(self, project_id: str) -> dict[str, Any]:
        return self._call(build_monitor(self.runtime, project_id))


def make_handler(service: ConsoleService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, UI_PATH.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/settings":
                self._json(200, service.settings())
                return
            if parsed.path == "/api/projects":
                self._json(200, {"projects": service.projects()})
                return
            if parsed.path == "/api/monitor":
                query = parse_qs(parsed.query)
                project_id = (query.get("project") or [""])[0].strip()
                if not project_id:
                    self._json(400, {"error": "project query parameter is required"})
                    return
                try:
                    self._json(200, service.monitor(project_id))
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
                return
            self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/settings":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = service.save_settings(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, result)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, code: int, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve_console(
    service: ConsoleService,
    *,
    host: str = "127.0.0.1",
    port: int = 8743,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
