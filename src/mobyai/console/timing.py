"""Durable workload timing, combining overlapping CLI and console sessions."""
from datetime import UTC, datetime


def runtime_view(events, *, running=False, now=None):
    now = now or datetime.now(UTC)
    intervals = []
    opened = {}
    estimated = False
    last = None
    starts = {"workload_started": "console", "execution_started": "execution"}
    ends = {"workload_exited": "console", "execution_finished": "execution"}
    for event in sorted(events, key=lambda e: e.timestamp):
        at = event.timestamp
        if event.event in starts:
            kind = starts[event.event]
            if kind in opened:
                intervals.append((opened.pop(kind), last or at))
                estimated = True
            opened[kind] = at
        elif event.event in ends:
            start = opened.pop(ends[event.event], None)
            if start is not None:
                intervals.append((start, at))
        last = at
    for start in opened.values():
        intervals.append((start, now if running else max(start, last or start)))
        estimated |= not running
    if not intervals:
        return {"active_seconds": None, "elapsed_seconds": None, "running": False, "estimated": False}
    merged = []
    for start, end in sorted(intervals):
        end = max(start, end)
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return {
        "active_seconds": sum((end-start).total_seconds() for start, end in merged),
        "elapsed_seconds": (merged[-1][1]-merged[0][0]).total_seconds(),
        "running": bool(opened) and running,
        "estimated": estimated,
    }
