import os
from pathlib import Path
import pytest
from mobyai.execution.workspace import Workspace
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.execution.errors import WorkspaceError, PathEscapeError
from mobyai.sandbox.backend import validate_tree, container_args
from mobyai.sandbox.policy import SandboxConfig

async def test_listing_and_search_survive_external_dangling_and_cyclic_links(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'real.txt').write_text('hello')
    (root/'alias.txt').symlink_to('real.txt')
    (root/'broken').symlink_to('missing')
    (root/'loop').symlink_to('loop')
    (tmp_path/'outside').write_text('private')
    (root/'external').symlink_to(tmp_path/'outside')
    files=WorkspaceFilesystem(Workspace(root))
    assert await files.list_directory()==['alias.txt','broken','external','loop','real.txt']
    assert await files.read_file('alias.txt')=='hello'
    await files.write_file('alias.txt','updated')
    assert (root/'alias.txt').is_symlink()
    assert (root/'real.txt').read_text()=='updated'
    with pytest.raises(PathEscapeError):await files.read_file('external')
    with pytest.raises(PathEscapeError):await files.write_file('external','no')
    for name in ('broken','loop'):
        with pytest.raises(WorkspaceError):await files.read_file(name)
    hits=await files.search_code('updated')
    assert hits
    assert await files.glob_files('*.txt')
    assert (tmp_path/'outside').read_text()=='private'

async def test_directory_links_and_dangling_writes_within_workspace(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'actual').mkdir()
    (root/'dir').symlink_to('actual',target_is_directory=True)
    (root/'output').symlink_to('actual/new.txt')
    files=WorkspaceFilesystem(Workspace(root))
    await files.write_file('output','new')
    assert await files.read_file('dir/new.txt')=='new'
    (root/'absolute').symlink_to(root/'actual/new.txt')
    assert await files.read_file('absolute')=='new'


def test_sandbox_does_not_dereference_links(tmp_path):
    root=tmp_path/'project';root.mkdir()
    for name,target in [('bin','/usr/bin/python3'),('loop','loop'),('missing','absent'),('outside','../outside')]:
        (root/name).symlink_to(target)
    validate_tree(root)
    (root/'regular').write_text('x');os.link(root/'regular',root/'hard')
    with pytest.raises(ValueError,match='hard links'):validate_tree(root)
    (root/'hard').unlink();os.mkfifo(root/'pipe')
    with pytest.raises(ValueError,match='special files'):validate_tree(root)


def test_both_project_aliases_protect_history(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'mobyMinutes').mkdir()
    args=container_args(SandboxConfig(enabled=True,image='sha256:'+'a'*64),Workspace(root),root,tmp_path/'deps','test',['true'])
    assert f'{root}:{root}:rw,Z' in args
    assert f'{root}/mobyMinutes:/workspace/mobyMinutes:ro,Z' in args
    assert f'{root}/mobyMinutes:{root}/mobyMinutes:ro,Z' in args
    (root/'.git').symlink_to('mobyMinutes')
    with pytest.raises(ValueError,match='Protected path'):container_args(SandboxConfig(),Workspace(root),root,tmp_path/'deps','test',['true'])
