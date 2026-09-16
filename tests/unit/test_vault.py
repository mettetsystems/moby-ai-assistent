import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from mobyai import vault
from mobyai.console.server import ConsoleService, serve_console
from mobyai.models.config import AppConfig
from mobyai.runtime import Runtime
from mobyai.state.memory import InMemoryStateStore


@pytest.fixture
def backend(monkeypatch):
    items = {}
    class Item:
        def __init__(self, attrs, value): self.attrs, self.value = attrs, value
        def get_secret(self): return self.value
        def delete(self): items.pop(self.attrs['provider'])
    class Backend:
        def search_items(self, attrs):
            return [item for item in items.values() if item.attrs == attrs]
        def create_item(self, label, attrs, value, replace=False):
            assert replace
            items[attrs['provider']] = Item(attrs, value)
    @contextmanager
    def fake_collection(): yield Backend()
    monkeypatch.setattr(vault, 'collection', fake_collection)
    return items


def test_store_replace_metadata_and_remove(backend, monkeypatch):
    v = vault.Vault()
    v.save('github', 'dummy-first')
    v.save('github', 'dummy-replacement')
    assert len(backend) == 1
    assert v.get('github') == 'dummy-replacement'
    monkeypatch.setattr(backend['github'], 'get_secret', lambda: pytest.fail('status must not read secrets'))
    status = v.status()
    assert len(status['providers']) == 10
    assert status['available']
    assert 'dummy' not in json.dumps(status)
    assert next(p for p in status['providers'] if p['id'] == 'github')['configured']
    v.remove('github')
    assert not backend


@pytest.mark.parametrize('provider,value', [('other','abc'),('github',''),('github','a b'),('github','x'*8193),('github',None)])
def test_invalid_credentials_never_written(backend, provider, value):
    with pytest.raises(vault.VaultError): vault.Vault().save(provider,value)
    assert not backend


def test_locked_vault_and_backend_errors_sanitized(monkeypatch):
    secretstorage = pytest.importorskip("secretstorage")
    monkeypatch.setattr(secretstorage, 'dbus_init', lambda: SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(secretstorage, 'check_service_availability', lambda _:True)
    monkeypatch.setattr(secretstorage, 'get_default_collection', lambda _:SimpleNamespace(is_locked=lambda:True))
    status=vault.Vault().status()
    assert not status['available'] and 'locked' in status['message']
    assert all(p['configured'] is None for p in status['providers'])
    def fail(*args): raise RuntimeError('dummy-sensitive-value')
    monkeypatch.setattr(secretstorage, 'get_default_collection', fail)
    assert 'dummy-sensitive-value' not in json.dumps(vault.Vault().status())


def test_local_api_csrf_origin_and_never_returns_values(tmp_path, backend):
    config=AppConfig.from_yaml('config/default.yaml')
    runtime=Runtime(config, InMemoryStateStore(),None,None,None,None,owns_store=False)
    service=ConsoleService(runtime,tmp_path/'config.yaml',caller=lambda coro:None)
    server=serve_console(service,host='127.0.0.1',port=0)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_address[1]}',trust_env=False) as client:
            status=client.get('/api/secrets')
            assert status.headers['cache-control']=='no-store'
            headers={'X-MobyAI-Vault-CSRF':status.json()['csrf_token']}
            payload={'action':'save','provider':'github','value':'dummy-sensitive-value'}
            assert client.post('/api/secrets',json=payload).status_code==403
            assert client.post('/api/secrets',json=payload,headers={**headers,'Origin':'https://attacker.invalid'}).status_code==403
            assert client.get('/api/secrets',headers={'Host':'attacker.invalid'}).status_code==403
            assert not backend
            response=client.post('/api/secrets',json=payload,headers=headers)
            assert response.status_code==200 and 'dummy' not in response.text
            assert 'dummy' not in client.get('/api/secrets').text
            assert client.post('/api/secrets',json={'action':'remove','provider':'github'},headers=headers).status_code==400
            assert client.post('/api/secrets',json={'action':'remove','provider':'github','confirmation':'github'},headers=headers).status_code==200
            assert not backend
    finally:
        server.shutdown()
        server.server_close()


def test_huggingface_vault_fallback_preserves_legacy(tmp_path, monkeypatch):
    from mobyai.inference.huggingface import resolve_huggingface_token
    config=AppConfig.from_yaml('config/default.yaml')
    config.huggingface.token_file=tmp_path/'missing-token'
    monkeypatch.delenv('HF_TOKEN',raising=False)
    monkeypatch.delenv('HUGGING_FACE_HUB_TOKEN',raising=False)
    monkeypatch.setattr(vault,'optional_token',lambda provider:'dummy-vault-token')
    assert resolve_huggingface_token(config)=='dummy-vault-token'
    monkeypatch.setenv('HF_TOKEN','dummy-env-token')
    assert resolve_huggingface_token(config)=='dummy-env-token'


@pytest.mark.parametrize('source,expected', [('semantic_scholar','semantic_scholar'),('web','brave'),('dblp',None),('zenodo',None),(None,None)])
async def test_research_only_requests_selected_vault_credential(monkeypatch, source, expected):
    mcp=pytest.importorskip('mcp')
    from contextlib import asynccontextmanager
    import mcp.client.stdio
    from mobyai.research.client import call_research
    requested=[]
    def token(provider):
        requested.append(provider)
        return 'dummy-' + provider
    monkeypatch.setattr(vault,'optional_token',token)
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    monkeypatch.delenv('SEMANTIC_SCHOLAR_API_KEY',raising=False)
    @asynccontextmanager
    async def stdio(params):
        keys={k:v for k,v in params.env.items() if k.endswith('API_KEY')}
        key='SEMANTIC_SCHOLAR_API_KEY' if expected=='semantic_scholar' else 'BRAVE_SEARCH_API_KEY'
        assert keys==({key:'dummy-'+expected} if expected else {})
        yield None,None
    class Session:
        def __init__(self,*args): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def initialize(self): pass
        async def call_tool(self,*args):
            return SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text='{"status":"ok"}')])
    monkeypatch.setattr(mcp.client.stdio,'stdio_client',stdio)
    monkeypatch.setattr(mcp,'ClientSession',Session)
    result=await call_research('research_search',{'source':source} if source else {})
    assert result['status']=='ok'
    assert requested==([expected] if expected else [])
