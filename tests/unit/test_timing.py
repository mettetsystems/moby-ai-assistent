from datetime import UTC, datetime, timedelta
from mobyai.console.timing import runtime_view
from mobyai.models.event import EventRecord

BASE = datetime(2026, 9, 12, tzinfo=UTC)

def event(name, seconds):
    return EventRecord(timestamp=BASE + timedelta(seconds=seconds), component='runtime', event=name)

def test_resume_excludes_pause_and_unions_console_cli():
    events = [event('workload_started', 0), event('execution_started', 1),
              event('execution_finished', 9), event('workload_exited', 10),
              event('execution_started', 100), event('execution_finished', 120)]
    result = runtime_view(events)
    assert result['active_seconds'] == 30
    assert result['elapsed_seconds'] == 120
    assert not result['running']
    assert not result['estimated']

def test_running_uses_saved_start_on_every_refresh():
    events = [event('execution_started', 10)]
    assert runtime_view(events, running=True, now=BASE+timedelta(seconds=25))['active_seconds'] == 15
    assert runtime_view(events, running=True, now=BASE+timedelta(seconds=30))['active_seconds'] == 20

def test_crashed_session_freezes_at_last_evidence():
    result = runtime_view([event('execution_started', 10), event('tool_finished', 20)], now=BASE+timedelta(days=2))
    assert result['active_seconds'] == 10
    assert result['estimated']
    assert not result['running']

def test_missing_history_not_fabricated():
    assert runtime_view([event('prompt_imported', 0)])['active_seconds'] is None

def test_repeated_start_does_not_count_idle_gap():
    result = runtime_view([event('execution_started', 0), event('tool_finished', 10),
                           event('execution_started', 100), event('execution_finished', 110)])
    assert result['active_seconds'] == 20
    assert result['estimated']
