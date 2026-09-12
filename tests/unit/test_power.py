import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from mobyai.engine.power import keep_awake


def fake_process(ready=b'ready\n'):
    return SimpleNamespace(stdin=Mock(), stdout=SimpleNamespace(readline=AsyncMock(return_value=ready)),
                           wait=AsyncMock(return_value=0), kill=Mock())

@pytest.mark.parametrize('outcome', ['success', 'error', 'cancel'])
async def test_inhibitor_released_on_every_exit(monkeypatch, outcome):
    process = fake_process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr('mobyai.engine.power.shutil.which', lambda _: '/usr/bin/systemd-inhibit')
    monkeypatch.setattr('mobyai.engine.power.asyncio.create_subprocess_exec', spawn)
    output = StringIO()
    try:
        async with keep_awake(enabled=True, stdout=output) as acquired:
            assert acquired
            process.stdin.close.assert_not_called()
            if outcome == 'error':
                raise ValueError('failure')
            if outcome == 'cancel':
                raise asyncio.CancelledError()
    except (ValueError, asyncio.CancelledError):
        pass
    process.stdin.close.assert_called_once()
    process.wait.assert_awaited_once()
    assert '--what=idle:sleep' in spawn.call_args.args
    assert 'released' in output.getvalue()

async def test_disabled_does_not_spawn(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr('mobyai.engine.power.asyncio.create_subprocess_exec', spawn)
    async with keep_awake(enabled=False, stdout=StringIO()) as acquired:
        assert not acquired
    spawn.assert_not_called()

@pytest.mark.parametrize('available', [True, False])
async def test_denied_or_missing_warns_without_claiming_protection(monkeypatch, available):
    process = fake_process(b'')
    monkeypatch.setattr('mobyai.engine.power.shutil.which', lambda _: '/bin/systemd-inhibit' if available else None)
    monkeypatch.setattr('mobyai.engine.power.asyncio.create_subprocess_exec', AsyncMock(return_value=process))
    output = StringIO()
    async with keep_awake(enabled=True, stdout=output) as acquired:
        assert not acquired
    assert 'inactive' in output.getvalue()
    assert 'released' not in output.getvalue()
