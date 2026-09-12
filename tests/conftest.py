"""Keep research tests independent of the operator's real cache and utilization."""
import pytest

@pytest.fixture(autouse=True)
def isolated_research_cache(tmp_path,monkeypatch):
    monkeypatch.setenv('MOBYAI_RESEARCH_CACHE',str(tmp_path/'research-test.sqlite3'))
