"""Keep research tests independent of the operator's real cache and utilization."""
import pytest

@pytest.fixture(autouse=True)
def isolated_research_cache(tmp_path,monkeypatch):
    monkeypatch.setenv('MOBYAI_RESEARCH_CACHE',str(tmp_path/'research-test.sqlite3'))

@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch):
    # Tests must never consume credentials from the operator's real desktop vault.
    monkeypatch.setattr('mobyai.vault.optional_token', lambda provider: None)
