"""Read-only, paginated trace browsing without a second web server."""
from __future__ import annotations

import os
from typing import Any

from mobyai.tracing import tracking_uri

PAGE_SIZE = 25
MAX_SPANS = 1000
DISPLAY_ATTRIBUTES = frozenset({
    "project_id", "task_id", "tool", "model", "temperature", "top_p", "top_k",
    "prompt_tokens", "completion_tokens", "finish_reason", "exit_code", "timed_out",
    "ok", "passed", "duration_seconds", "error.type", "http.status_code", "attempt",
})


class TraceViewerUnavailable(Exception):
    pass


class TraceBrowser:
    def __init__(self, config):
        self.config = config

    def _client(self):
        try:
            from mlflow import MlflowClient
        except ImportError as exc:
            raise TraceViewerUnavailable("Install MLflow with: .venv/bin/python -m pip install -e '.[tracing]'") from exc
        return MlflowClient(tracking_uri=tracking_uri(self.config.tracing, self.config.persistence.database_path))

    def _experiment(self, client):
        return client.get_experiment_by_name(self.config.tracing.experiment)

    def listing(self, page_token: str | None = None, status: str = "") -> dict[str, Any]:
        if status not in {"", "OK", "ERROR", "IN_PROGRESS"}:
            raise ValueError("Unknown trace status")
        if page_token and len(page_token) > 4096:
            raise ValueError("Invalid trace page token")
        client = self._client()
        experiment = self._experiment(client)
        result = {"experiment": self.config.tracing.experiment,
                  "enabled": self.config.tracing.enabled or os.environ.get("MOBYAI_MLFLOW_ENABLED") == "1",
                  "traces": [], "next_page_token": None}
        if experiment is None:
            return result
        traces = client.search_traces(locations=[experiment.experiment_id],
            max_results=PAGE_SIZE, include_spans=False, page_token=page_token,
            filter_string=f"status = '{status}'" if status else None,
            order_by=["timestamp_ms DESC"])
        result["traces"] = [_summary(trace.info) for trace in traces]
        result["next_page_token"] = traces.token
        return result

    def detail(self, trace_id: str) -> dict[str, Any]:
        if not trace_id or len(trace_id) > 256:
            raise ValueError("A valid trace ID is required")
        client = self._client()
        experiment = self._experiment(client)
        if experiment is None:
            raise KeyError("Trace not found in the configured experiment")
        try:
            trace = client.get_trace(trace_id, display=False)
        except Exception as exc:
            if getattr(exc, "error_code", None) == "RESOURCE_DOES_NOT_EXIST":
                raise KeyError("Trace not found") from exc
            raise
        location = trace.info.trace_location.mlflow_experiment
        if location is None or str(location.experiment_id) != str(experiment.experiment_id):
            raise KeyError("Trace not found in the configured experiment")
        spans = sorted(trace.data.spans, key=lambda span: span.start_time_ns)
        return {**_summary(trace.info), "span_count": len(spans),
                "truncated": len(spans) > MAX_SPANS,
                "spans": [_span_view(span) for span in spans[:MAX_SPANS]]}


def _summary(info):
    return {"trace_id": info.trace_id, "name": str(info.tags.get("mlflow.traceName", "Trace"))[:200],
            "status": info.state.value, "started_ms": info.request_time,
            "duration_ms": info.execution_duration}


def _span_view(span):
    # Never forward arbitrary MLflow inputs/outputs, tags, or raw exception events.
    attributes = {key: value[:1000] if isinstance(value, str) else value
                  for key, value in span.attributes.items()
                  if key in DISPLAY_ATTRIBUTES and isinstance(value, (str, int, float, bool, type(None)))}
    return {"span_id": span.span_id, "parent_id": span.parent_id,
            "name": span.name[:200], "status": span.status.status_code.value,
            "started_ms": span.start_time_ns / 1_000_000,
            "duration_ms": ((span.end_time_ns - span.start_time_ns) / 1_000_000
                            if span.end_time_ns is not None else None),
            "attributes": attributes}
