"""Local operator console. stdlib HTTP; no extra web framework."""

from __future__ import annotations

import json
import logging
import re
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
from mobyai.console.workloads import Workloads
from mobyai.console.traces import TraceBrowser, TraceViewerUnavailable
from mobyai.runtime import Runtime

UI_PATH = Path(__file__).with_name("ui.html")


class ConsoleService:
    def __init__(self, runtime: Runtime, config_path: Path, caller: Any) -> None:
        self.runtime = runtime
        self.config_path = config_path
        self._call = caller
        self.workloads = Workloads(runtime, config_path)
        self.traces = TraceBrowser(runtime.config)

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
        snapshot = self._call(build_monitor(self.runtime, project_id))
        snapshot["workload"] = self.workloads.status(project_id, snapshot["project"]["state"])
        from mobyai.review.service import view
        snapshot["review"] = view(self._call(self.runtime.store.projects.get(project_id)))
        review_events = self._call(self.runtime.store.events.list_for_project(project_id))
        for item in reversed(review_events):
            if item.event in {'logic_report_failed', 'code_review_failed', 'logic_report_requested', 'logic_report_generated', 'code_archaeology_started', 'code_archaeology_completed', 'review_plan_proposed', 'review_plan_approved'}:
                snapshot["review"]["job_status"] = item.event.replace('_', ' ') + (': '+str(item.payload['error']) if item.payload.get('error') else '')
                break
        snapshot["launch_log"] = ""
        if project_id:
            log = self.workloads.log_path(project_id)
            try:
                with log.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 65536))
                    snapshot["launch_log"] = "\n".join(stream.read().decode("utf-8", errors="replace").splitlines()[-100:])
            except OSError:
                pass
        return snapshot


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
            if parsed.path in {"/api/traces", "/api/traces/detail"}:
                query = parse_qs(parsed.query)
                try:
                    if parsed.path.endswith("/detail"):
                        result = service.traces.detail((query.get("id") or [""])[0])
                    else:
                        result = service.traces.listing((query.get("page") or [None])[0],
                                                       (query.get("status") or [""])[0])
                    self._json(200, result)
                except TraceViewerUnavailable as exc:
                    self._json(503, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except KeyError:
                    self._json(404, {"error": "Trace not found in the configured experiment"})
                except Exception as exc:
                    logging.getLogger(__name__).warning("Trace query failed (%s)", type(exc).__name__)
                    self._json(503, {"error": "Cannot read MLflow traces. Check the configured tracking backend, then retry."})
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

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            if route not in {"/api/projects", "/api/workloads/prepare-stop", "/api/workloads/stop", "/api/workloads/resume", "/api/workloads/restart", "/api/workloads/target", "/api/workloads/delete", "/api/workloads/approve-plan", "/api/workloads/report"}:
                self._json(404, {"error": "not found"})
                return
            origin = self.headers.get("Origin")
            if (origin and origin != "http://" + self.headers.get("Host", "")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._json(403, {"error": "Cross-origin launch denied"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "Use application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8_000_000:
                    self._json(413, {"error": "Import request is too large or empty"})
                    return
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                handler = {"/api/projects": service.workloads.create,
                           "/api/workloads/prepare-stop": service.workloads.prepare_stop,
                           "/api/workloads/stop": service.workloads.stop,
                           "/api/workloads/delete": service.workloads.delete,
                           "/api/workloads/approve-plan": service.workloads.approve_plan,
                           "/api/workloads/report": service.workloads.report,
                           "/api/workloads/resume": service.workloads.resume,
                           "/api/workloads/restart": service.workloads.restart,
                           "/api/workloads/target": service.workloads.change_target}[route]
                result = service._call(handler(payload))
                self._json(201 if route == "/api/projects" else 200, result)
            except (ValueError, UnicodeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(500, {"error": "Could not launch workload. Check console logs and project list before retrying."})

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
