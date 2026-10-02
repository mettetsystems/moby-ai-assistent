from hashlib import sha256
from types import SimpleNamespace
import pytest
from mobyai.review import minutes
from mobyai.review.service import view


def setup(tmp_path):
    real=tmp_path/'real';real.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(real,target_is_directory=True)
    project=SimpleNamespace(project_id='p',workspace_root=alias)
    directory=real/'mobyMinutes'/'runs'/sha256(b'p').hexdigest()[:24]
    directory.mkdir(parents=True)
    return project,directory


def test_workspace_alias_read_write_and_monitor(tmp_path):
    project,directory=setup(tmp_path)
    path=project.workspace_root/'mobyMinutes'/'runs'/directory.name/'proposal.json'
    minutes.write(path,{'revision':'r1'})
    assert minutes.read(path)=={'revision':'r1'}
    assert view(project)=={'proposal':{'revision':'r1'}}
    assert not project.workspace_root.resolve().is_symlink()


def test_report_symlink_rejected_but_monitor_survives(tmp_path):
    project,directory=setup(tmp_path)
    outside=tmp_path/'outside.json';outside.write_text('{"secret":true}')
    (directory/'proposal.json').symlink_to(outside)
    minutes.write(directory/'reasoning-logic-report.json',{'summary':'valid'})
    result=view(project)
    assert 'proposal' not in result and result['logic_report']['summary']=='valid'
    assert len(result['warnings'])==1
    with pytest.raises(ValueError):minutes.write(directory/'proposal.json',{})
    assert outside.read_text()=='{"secret":true}'


def test_internal_directory_symlink_stays_blocked(tmp_path):
    real=tmp_path/'real';real.mkdir();outside=tmp_path/'outside';outside.mkdir()
    (real/'mobyMinutes').symlink_to(outside,target_is_directory=True)
    (outside/'report.json').write_text('{}')
    with pytest.raises(ValueError):minutes.read(real/'mobyMinutes/report.json')


def test_malformed_and_oversized_reports_are_warnings(tmp_path,monkeypatch):
    project,directory=setup(tmp_path)
    (directory/'proposal.json').write_text('{bad')
    (directory/'reasoning-logic-report.json').write_text('x'*20)
    monkeypatch.setattr(minutes,'MAX_READ_BYTES',10)
    result=view(project)
    assert len(result['warnings'])==2
