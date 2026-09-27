from pathlib import Path

import pytest

from mobyai.agents.prompts.settings import PromptSnapshot, default_prompts
from mobyai.agents.worker import WorkerAgent
from mobyai.console.settings import (
    OverlayWatcher, apply_role_settings, save_settings_overlay, serialize_role_settings,
)
from mobyai.context.types import WorkerContext
from mobyai.models.config import AppConfig, overlay_path_for

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / 'config/default.yaml'


def config():
    return AppConfig.from_yaml(DEFAULT_CONFIG)


def test_settings_round_trip_and_restore_default(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(DEFAULT_CONFIG.read_text())
    saved = AppConfig.from_yaml(path)
    defaults = default_prompts()
    assert serialize_role_settings(saved)['system_prompts'] == defaults
    overrides = {key: f'Custom {key}\nKeep {{literal braces}}.' for key in defaults}
    apply_role_settings(saved, {'system_prompts': overrides})
    save_settings_overlay(path, saved)
    loaded = AppConfig.from_yaml(path)
    assert serialize_role_settings(loaded)['system_prompts'] == overrides
    apply_role_settings(loaded, {'system_prompts': {'worker': None}})
    save_settings_overlay(path, loaded)
    assert serialize_role_settings(AppConfig.from_yaml(path))['system_prompts']['worker'] == defaults['worker']


@pytest.mark.parametrize('value', ['', '   ', 42, [], {'unknown': 'text'}])
def test_invalid_prompt_settings_are_rejected(value):
    current = config()
    payload = value if isinstance(value, (dict, list)) else {'worker': value}
    with pytest.raises(ValueError):
        apply_role_settings(current, {'system_prompts': payload})
    assert current.system_prompts.worker is None


async def test_live_reload_does_not_change_worker_or_other_roles_mid_attempt(tmp_path):
    current = config()
    path = tmp_path / 'config.yaml'
    path.write_text(DEFAULT_CONFIG.read_text())
    watcher = OverlayWatcher(overlay_path_for(path), current)
    prompts = PromptSnapshot(current, watcher.maybe_reload)
    prompts.begin_attempt()
    original = prompts.active.copy()

    class Capture:
        async def generate(self, request, schema):
            self.requests.append(request)
        requests = []

    generator = Capture()
    worker = WorkerAgent(generator)
    worker.prompts = prompts
    context = WorkerContext(project_id='proj', task_id='STEP_001', objective='Do work', instruction='Implement', token_budget=2000, token_count=0,
                            acceptance_criteria=['Pass'], test_commands=['pytest'])
    await worker.implement(context)
    saved = config()
    apply_role_settings(saved, {'system_prompts': {key: 'Edited ' + key for key in original}})
    save_settings_overlay(path, saved)
    # Inference refresh runs between turns; it must only update pending settings.
    watcher.maybe_reload()
    await worker.implement(context)
    assert generator.requests[0].messages[0].content == generator.requests[1].messages[0].content
    assert prompts.active == original
    assert current.system_prompts.worker == 'Edited worker'
    prompts.begin_attempt()
    await worker.implement(context)
    assert generator.requests[2].messages[0].content.startswith('Edited worker\n')
    assert prompts.active == {key: 'Edited ' + key for key in original}
