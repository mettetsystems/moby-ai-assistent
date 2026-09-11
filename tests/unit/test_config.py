from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mobyai.models.config import AgentRole, AppConfig, ServiceMode

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
DUAL_GPU_CONFIG = REPO_ROOT / "config" / "examples" / "dual_gpu.yaml"


def test_default_yaml_loads() -> None:
    config = AppConfig.from_yaml(DEFAULT_CONFIG)
    assert config.runtime.max_task_attempts == 2
    assert config.runtime.max_planner_inspect_turns == 3
    assert config.context.prefer_import_contracts is True
    assert config.models.planner.endpoint == "http://127.0.0.1:9802"
    assert config.models.worker.inference is not None
    assert config.models.worker.inference.kv_cache_dtype == "fp8"
    assert config.hardware is not None
    assert config.hardware.for_role(AgentRole.ORCHESTRATOR).device == 1
    assert config.models.for_role(AgentRole.WORKER).gpu == 0
    assert config.persistence.database_path == Path.home() / "MobyAI" / "mobyai.db"
    assert config.models.planner.model == "Qwen/Qwen3-32B"
    assert config.models.worker.model == "Qwen/Qwen3.5-35B-A3B"
    assert config.huggingface.token_file == Path.home() / "MobyAI" / "secrets" / "hf_token"


def test_dual_gpu_example_loads() -> None:
    config = AppConfig.from_yaml(DUAL_GPU_CONFIG)
    assert config.hardware is not None
    assert config.hardware.planner.device == 0
    assert config.hardware.orchestrator.device == 1


def test_omitted_services_defaults_to_external(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    del payload["services"]
    path = tmp_path / "no-services.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = AppConfig.from_yaml(path)
    assert config.services.mode is ServiceMode.EXTERNAL
    assert config.models.planner.launch == []


def test_home_directory_is_expanded(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["project"]["workspace_root"] = "~/MobyAI/projects"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = AppConfig.from_yaml(path)
    assert config.project.workspace_root == Path.home() / "MobyAI" / "projects"
    assert "~" not in str(config.project.workspace_root)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig.from_yaml(path)


def test_missing_required_model_is_rejected(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    del payload["models"]["planner"]
    path = tmp_path / "incomplete.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig.from_yaml(path)


def test_omitted_huggingface_is_optional(tmp_path: Path) -> None:
    payload = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    del payload["huggingface"]
    path = tmp_path / "no-hf.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = AppConfig.from_yaml(path)
    assert config.huggingface.token_file is None
