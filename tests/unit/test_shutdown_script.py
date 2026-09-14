import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
spec = importlib.util.spec_from_file_location('shutdown_script', SCRIPTS/'shutdown.py')
shutdown = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(shutdown)
finally:
    sys.path.remove(str(SCRIPTS))


def test_already_stopped_does_not_send_signal(monkeypatch, capsys):
    monkeypatch.setattr(shutdown,'listener_pids',lambda port:[])
    monkeypatch.setattr(shutdown.os,'kill',lambda *args:pytest.fail('No signal expected'))
    shutdown.shutdown(Path('/config'),8743,10)
    assert 'No MobyAI console' in capsys.readouterr().out


def test_unrelated_listener_is_never_stopped(monkeypatch):
    monkeypatch.setattr(shutdown,'listener_pids',lambda port:[123])
    monkeypatch.setattr(shutdown,'process_identity',lambda *args: (_ for _ in ()).throw(ValueError('Not this console')))
    monkeypatch.setattr(shutdown.os,'kill',lambda *args:pytest.fail('No signal expected'))
    with pytest.raises(ValueError,match='Not this console'):
        shutdown.shutdown(Path('/config'),8743,10)


def test_workload_is_paused_with_bound_proof_before_console_exits(monkeypatch):
    listeners=iter([[123],[]]); calls=[]; monitor=iter([
        {'can_stop':True,'stopping':False},
        {'can_stop':False,'stopping':True},
        {'can_stop':False,'stopping':False},
    ])
    monkeypatch.setattr(shutdown,'listener_pids',lambda port:next(listeners))
    monkeypatch.setattr(shutdown,'process_identity',lambda *args: (['console'],42))
    monkeypatch.setattr(shutdown.time,'sleep',lambda _:None)
    monkeypatch.setattr(shutdown,'exited',lambda *args:True)
    def request(base,path,payload=None):
        calls.append((path,payload))
        if path=='/api/projects':return {'projects':[{'project_id':'demo'}]}
        if path.startswith('/api/monitor'):return {'workload':next(monitor)}
        if path.endswith('prepare-stop'):return {'project_id':'demo','token':'proof'}
        return {'status':'stopping'}
    monkeypatch.setattr(shutdown,'request',request)
    monkeypatch.setattr(shutdown.os,'kill',lambda pid,sig:calls.append(('signal',(pid,sig))))
    shutdown.shutdown(Path('/config'),8743,10)
    assert ('/api/workloads/stop',{'project_id':'demo','confirmation':'demo','token':'proof'}) in calls
    assert calls[-1]==('signal',(123,signal.SIGINT))
    assert len([p for p,_ in calls if p=='/api/workloads/stop'])==1


def test_copy_timeout_keeps_console_running(monkeypatch):
    monkeypatch.setattr(shutdown,'listener_pids',lambda port:[123])
    monkeypatch.setattr(shutdown,'process_identity',lambda *args:(['console'],42))
    monkeypatch.setattr(shutdown,'activity',lambda base:[('demo',{'can_stop':False,'stopping':False,'relocation':{'state':'copying'}})])
    ticks=iter([0,11])
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(shutdown.os,'kill',lambda *args:pytest.fail('No signal expected'))
    with pytest.raises(ValueError,match='Console left running'):
        shutdown.shutdown(Path('/config'),8743,10)


def test_identity_change_prevents_signal(monkeypatch):
    monkeypatch.setattr(shutdown,'listener_pids',lambda port:[123])
    identities=iter([(['console'],42),(['other'],43)])
    monkeypatch.setattr(shutdown,'process_identity',lambda *args:next(identities))
    monkeypatch.setattr(shutdown.os,'kill',lambda *args:pytest.fail('No signal expected'))
    with pytest.raises(ValueError,match='identity changed'):
        shutdown.shutdown(Path('/config'),8743,10)
