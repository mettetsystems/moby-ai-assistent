"""Bounded request-local sampling; never mutate the operator's saved settings."""
from contextlib import contextmanager
from contextvars import ContextVar

_stage = ContextVar("mobyai_recovery_stage", default=0)


@contextmanager
def recovery_stage(stage: int):
    token = _stage.set(max(_stage.get(), stage, 0))
    try:
        yield
    finally:
        _stage.reset(token)


def current_stage():
    return _stage.get()


# At most six configured targets, with three independent changes per target.
MAX_RECOVERY_STAGES = 18


def sampling_steps(baseline, profiles):
    """Expand targets into cumulative, single-setting changes; omit no-ops."""
    sampling = baseline.model_copy()
    steps = []
    for profile in profiles:
        for field in ("temperature", "top_k", "top_p"):
            value = getattr(profile, field)
            if getattr(sampling, field) != value:
                sampling = sampling.model_copy(update={field: value})
                steps.append(sampling)
    return steps
