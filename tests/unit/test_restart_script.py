import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('restart_script', Path(__file__).resolve().parents[2]/'scripts/restart.py')
restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restart)


def test_only_matching_checkout_config_and_ui_are_accepted(tmp_path):
    repo=tmp_path/'repo'; config=tmp_path/'config.yaml'
    prefix=[str(repo/'.venv/bin/python'),str(repo/'.venv/bin/mobyai'),'--config',str(config)]
    assert restart.matching_console(prefix+['ui'], config, repo)
    assert not restart.matching_console(prefix+['run','demo'], config, repo)
    assert not restart.matching_console(prefix+['ui'], tmp_path/'other.yaml', repo)
    assert not restart.matching_console(['/usr/bin/python','-m','http.server'], config, repo)


def test_listener_owner_must_be_known(monkeypatch):
    monkeypatch.setattr(restart.shutil,'which',lambda name:'/usr/bin/ss')
    monkeypatch.setattr(restart.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='LISTEN 0 5 127.0.0.1:8743 0.0.0.0:*'))
    with pytest.raises(ValueError,match='owner cannot be verified'):
        restart.listener_pids(8743)


@pytest.mark.parametrize('workload',[
    {'can_stop':True,'stopping':False},
    {'can_stop':False,'stopping':True},
    {'can_stop':False,'stopping':False,'relocation':{'state':'copying'}},
    None,
])
def test_busy_or_unknown_activity_prevents_restart(monkeypatch,workload):
    import io,json
    class Opener:
        def open(self,url,timeout):
            payload={'projects':[{'project_id':'demo'}]} if url.endswith('/api/projects') else {'workload':workload}
            return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(restart,'build_opener',lambda *a:Opener())
    with pytest.raises(ValueError):
        restart.check_idle('http://localhost:8743')
