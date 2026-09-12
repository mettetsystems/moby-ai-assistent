from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from mobyai.console.traces import TraceBrowser, MAX_SPANS
from mobyai.console.server import ConsoleService, serve_console
from tests.unit.test_cli import _runtime


def info(experiment='1'):
    return SimpleNamespace(trace_id='trace-1', tags={'mlflow.traceName':'workload'},
        state=SimpleNamespace(value='ERROR'), request_time=1000, execution_duration=1250,
        trace_location=SimpleNamespace(mlflow_experiment=SimpleNamespace(experiment_id=experiment)))


def mock_browser(tmp_path, monkeypatch):
    _, runtime = _runtime(tmp_path)
    browser = TraceBrowser(runtime.config)
    client = MagicMock()
    client.get_experiment_by_name.return_value = SimpleNamespace(experiment_id='1')
    monkeypatch.setattr(browser, '_client', lambda:client)
    return browser, client


def test_listing_is_bounded_and_does_not_load_spans(tmp_path, monkeypatch):
    browser, client = mock_browser(tmp_path, monkeypatch)
    class Page(list):
        token = 'next'
    client.search_traces.return_value = Page([SimpleNamespace(info=info())])
    result = browser.listing('previous', 'ERROR')
    assert result['next_page_token'] == 'next'
    assert result['traces'][0]['status'] == 'ERROR'
    args = client.search_traces.call_args.kwargs
    assert args['include_spans'] is False
    assert args['max_results'] == 25
    assert args['locations'] == ['1']
    assert args['page_token'] == 'previous'
    with pytest.raises(ValueError):
        browser.listing(status="ERROR' or 1=1")


def test_details_scope_and_attribute_allowlist(tmp_path, monkeypatch):
    browser, client = mock_browser(tmp_path, monkeypatch)
    span = SimpleNamespace(name='model.request', span_id='span-1', parent_id=None,
        start_time_ns=1000000, end_time_ns=2000000,
        status=SimpleNamespace(status_code=SimpleNamespace(value='ERROR')),
        attributes={'temperature':.6, 'prompt_tokens':100, 'secret':'hidden', 'mlflow.spanInputs':'hidden'})
    client.get_trace.return_value = SimpleNamespace(info=info(), data=SimpleNamespace(spans=[span]*(MAX_SPANS+1)))
    result = browser.detail('trace-1')
    assert result['truncated']
    assert len(result['spans']) == MAX_SPANS
    assert result['spans'][0]['attributes'] == {'temperature':.6, 'prompt_tokens':100}
    assert result['spans'][0]['duration_ms'] == 1
    client.get_trace.return_value.info = info('other')
    with pytest.raises(KeyError):
        browser.detail('trace-1')


def test_trace_http_routes(tmp_path):
    path, runtime = _runtime(tmp_path)
    service = ConsoleService(runtime, path, caller=lambda coro:None)
    service.traces = MagicMock()
    service.traces.listing.return_value = {'traces':[], 'next_page_token':None}
    service.traces.detail.side_effect = KeyError('not found')
    server = serve_console(service, port=0)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_address[1]}') as client:
            assert client.get('/api/traces?status=ERROR&page=abc').status_code == 200
            service.traces.listing.assert_called_once_with('abc','ERROR')
            assert client.get('/api/traces/detail?id=unknown').status_code == 404
            service.traces.listing.side_effect = RuntimeError('backend credentials must not leak')
            response = client.get('/api/traces')
            assert response.status_code == 503
            assert 'credentials' not in response.text
    finally:
        server.shutdown()
        server.server_close()
