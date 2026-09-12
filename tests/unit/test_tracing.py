from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import mobyai.tracing as tracing


async def test_tracing_export_failure_does_not_mask_application_error(monkeypatch):
    sdk = MagicMock()
    sdk.start_span.side_effect = RuntimeError('backend down')
    monkeypatch.setattr(tracing, '_mlflow', sdk)
    @tracing.traced('test')
    async def work():
        raise ValueError('original error')
    with pytest.raises(ValueError, match='original error'):
        await work()


async def test_traces_exclude_sensitive_payloads(monkeypatch):
    sdk = MagicMock()
    monkeypatch.setattr(tracing, '_mlflow', sdk)
    @tracing.traced('test')
    async def work(prompt):
        return SimpleNamespace(ok=True)
    await work(prompt='secret-prompt')
    assert 'secret-prompt' not in str(sdk.mock_calls)


async def test_real_mlflow_local_trace(tmp_path, monkeypatch):
    mlflow = pytest.importorskip('mlflow')
    from mobyai.models.config import TracingConfig
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('MLFLOW_TRACKING_URI', raising=False)
    monkeypatch.setattr(tracing, '_mlflow', None)
    tracing.configure(TracingConfig(enabled=True, experiment='MobyAI test'), tmp_path/'mobyai.db')
    try:
        @tracing.traced('test.workload')
        async def work(project_id, prompt):
            with tracing.span('test.model', temperature=.6):
                return SimpleNamespace(ok=True)
        await work('test-project', 'do-not-export-prompt')
        tracing.flush()
        experiment = mlflow.get_experiment_by_name('MobyAI test')
        traces = mlflow.search_traces(locations=[experiment.experiment_id], return_type='list')
        from mobyai.console.traces import TraceBrowser
        browser = TraceBrowser(SimpleNamespace(tracing=TracingConfig(enabled=True, experiment='MobyAI test'),
            persistence=SimpleNamespace(database_path=tmp_path/'mobyai.db')))
        listing = browser.listing()
        assert len(listing['traces']) == 1
        detail = browser.detail(listing['traces'][0]['trace_id'])
        assert {item['name'] for item in detail['spans']} == {'test.workload', 'test.model'}
        assert detail['spans'][0]['attributes']['project_id'] == 'test-project'
        assert len(traces) == 1
        assert {s.name for s in traces[0].data.spans} == {'test.workload','test.model'}
        assert 'do-not-export-prompt' not in str(traces[0].to_dict())
    finally:
        tracing.configure(TracingConfig(), tmp_path/'mobyai.db')
