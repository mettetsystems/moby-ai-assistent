"""Optional MLflow spans. Payloads, source code, secrets and prompts are not exported."""
from contextlib import contextmanager
from functools import wraps
import inspect
import logging
import os

_log = logging.getLogger(__name__)
_mlflow = None


def configure(config, database_path):
    global _mlflow
    _mlflow = None
    if not config.enabled and os.environ.get("MOBYAI_MLFLOW_ENABLED") != "1":
        return
    try:
        import mlflow
    except ImportError as exc:
        raise ValueError("MLflow tracing is enabled but not installed. Run: pip install -e '.[tracing]'") from exc
    mlflow.set_tracking_uri(tracking_uri(config, database_path))
    mlflow.set_experiment(config.experiment)
    _mlflow = mlflow


def tracking_uri(config, database_path):
    return (os.environ.get("MLFLOW_TRACKING_URI") or config.tracking_uri
            or "sqlite:///" + str(database_path.parent.resolve() / "mlflow.db"))


def flush():
    if _mlflow is not None:
        try:
            _mlflow.flush_trace_async_logging()
        except Exception:
            _log.warning("MLflow trace flush failed", exc_info=False)


def attribute(span, key, value):
    if span is not None:
        try:
            span.set_attribute(key, value)
        except Exception:
            _log.warning("MLflow span attribute failed", exc_info=False)


def failed(span, kind):
    if span is not None:
        try:
            span.set_status("ERROR")
            span.set_attribute("error.type", kind)
        except Exception:
            _log.warning("MLflow error annotation failed", exc_info=False)


@contextmanager
def span(name, **attributes):
    manager = active = None
    if _mlflow is not None:
        try:
            manager = _mlflow.start_span(name=name)
            active = manager.__enter__()
            for key, value in attributes.items():
                attribute(active, key, value)
        except Exception:
            manager = None
            _log.warning("MLflow span creation failed; workload continues", exc_info=False)
    try:
        yield active
    except BaseException as exc:
        # Avoid automatic export of exception messages (they can contain secrets).
        failed(active, type(exc).__name__)
        raise
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                _log.warning("MLflow span export failed; workload continues", exc_info=False)


def traced(name):
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        async def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            metadata = {}
            for key in ("project_id", "task_id"):
                value = arguments.get(key)
                if isinstance(value, str):
                    metadata[key] = value
            project = arguments.get("project")
            if project is not None:
                metadata["project_id"] = project.project_id
            call = arguments.get("call")
            if call is not None:
                metadata["tool"] = call.name.value
            with span(name, **metadata) as active:
                result = await function(*args, **kwargs)
                for key in ("exit_code", "timed_out", "ok", "passed", "duration_seconds"):
                    value = getattr(result, key, None)
                    if isinstance(value, (bool, int, float)):
                        attribute(active, key, value)
                        if (key in {"ok", "passed"} and not value) or (key == "exit_code" and value != 0):
                            failed(active, "validation_failure")
                return result
        return wrapped
    return decorate
