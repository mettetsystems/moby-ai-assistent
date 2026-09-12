import json
import httpx
import pytest
from mobyai.research.search import ResearchSearch, clean_query

async def test_cache_and_cross_instance_pacing(tmp_path):
    calls, waits = [], []
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={'result':{'hits':{'hit':[{'info':{'title':'Paper','url':'https://dblp.org/rec/a','year':'2024'}}]}}})
    async def sleep(seconds): waits.append(seconds)
    kw = dict(transport=httpx.MockTransport(handle),clock=lambda:1000,sleep=sleep)
    a = ResearchSearch(tmp_path/'cache.db',**kw)
    assert (await a.search('graph search','graph error'))['results'][0]['title'] == 'Paper'
    assert (await a.search('graph search','graph error'))['cached']
    b = ResearchSearch(tmp_path/'cache.db',**kw)
    await b.search('graph algorithm','graph error')
    assert len(calls) == 2
    assert waits == [0,10]

async def test_budget_and_retry_after(tmp_path):
    async def sleep(_): pass
    def handle(_): return httpx.Response(429,headers={'Retry-After':'300'})
    engine=ResearchSearch(tmp_path/'cache.db',transport=httpx.MockTransport(handle),clock=lambda:1000,sleep=sleep)
    assert (await engine.search('error signature','signature'))['status']=='rate_limited'
    assert (await engine.search('error signature','signature'))['retry_after_seconds']==301

async def test_three_search_budget(tmp_path):
    async def sleep(_): pass
    engine=ResearchSearch(tmp_path/'cache.db',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[]})),clock=lambda:1000,sleep=sleep)
    for i in range(3):
        assert (await engine.search(f'query {i}','same error','semantic_scholar'))['status']=='ok'
    assert (await engine.search('query 4','same error','semantic_scholar'))['status']=='error_budget_exhausted'

async def test_manual_sources_do_not_claim_retrieval(tmp_path,monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    engine=ResearchSearch(tmp_path/'cache.db')
    for source in ['google_scholar','papers_with_code','citeseerx','baidu_scholar','scielo','web']:
        result=await engine.search('Gradle wrapper error','wrapper failure',source)
        assert result['status']=='manual_search_required'
        assert not result['results']
        assert result['search_url'].startswith('https://')

async def test_validation_and_redaction(tmp_path):
    assert '/home/user/project' not in clean_query('failure /home/user/project token=abcd')
    assert 'abcd' not in clean_query('failure /home/user/project token=abcd')
    with pytest.raises(ValueError): clean_query('full\nlog')
    with pytest.raises(ValueError): await ResearchSearch(tmp_path/'cache.db').search('valid','valid','arbitrary-server')

async def test_stdio_mcp_bridge_without_external_request(tmp_path,monkeypatch):
    pytest.importorskip('mcp')
    from mobyai.research.client import research_search
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    monkeypatch.setenv('MOBYAI_RESEARCH_CACHE',str(tmp_path/'cache.db'))
    result=await research_search({'query':'Gradle wrapper','error':'wrapper error','source':'google_scholar'})
    assert result['status']=='manual_search_required'

async def test_challenge_stops_requests_without_bypass(tmp_path):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,headers={'Content-Type':'text/html'},text='challenge')
    async def sleep(_): pass
    engine=ResearchSearch(tmp_path/'cache.db',transport=httpx.MockTransport(handle),clock=lambda:1000,sleep=sleep)
    assert (await engine.search('wrapper error','wrapper error'))['status']=='access_blocked'
    assert (await engine.search('wrapper error','wrapper error'))['status']=='rate_limited'
    assert len(calls)==1

async def test_worker_dispatches_research_through_mcp(tmp_path,monkeypatch):
    from mobyai.execution.tools import ToolDispatcher
    from mobyai.execution.workspace import Workspace
    from mobyai.models.worker import WorkerToolCall
    async def fake(arguments):
        assert arguments['source']=='zenodo'
        return {'status':'ok','results':[{'title':'Study','url':'https://zenodo.org/records/123'}]}
    monkeypatch.setattr('mobyai.research.client.research_search',fake)
    dispatcher=ToolDispatcher.from_workspace(Workspace(tmp_path))
    from mobyai.research.access import research_access
    with research_access(True):
        result=await dispatcher.execute(WorkerToolCall(name='research_search',arguments={'query':'tempo matching','error':'matching error','source':'zenodo'}))
    assert result.ok
    assert 'https://zenodo.org/records/123' in result.output

async def test_shell_cd_gets_actionable_tool_feedback(tmp_path):
    from mobyai.execution.tools import ToolDispatcher
    from mobyai.execution.workspace import Workspace
    from mobyai.models.worker import WorkerToolCall
    dispatcher=ToolDispatcher.from_workspace(Workspace(tmp_path))
    result=await dispatcher.execute(WorkerToolCall(name='run_command',arguments={'command':['cd','android','&&','./gradlew','--version']}))
    assert not result.ok
    assert 'cwd' in result.error
