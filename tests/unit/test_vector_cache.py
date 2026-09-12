import sqlite3
import json
import httpx
import pytest
from mobyai.research.cache import VectorCache,CachePolicy,DAY
from mobyai.research.search import ResearchSearch


def result(query,source='zenodo'):
    return {'status':'ok','source':source,'query':query,'results':[{'title':'Evidence','url':'https://zenodo.org/records/1'}]}


def test_similar_reordered_query_reuses_evidence_but_preserves_original(tmp_path):
    cache=VectorCache(tmp_path/'db',clock=lambda:1000)
    cache.put('zenodo','music tempo heart rate matching',result('music tempo heart rate matching'))
    match=cache.get('zenodo','heart rate matching music tempo')
    assert match['cache_match']=='similar'
    assert match['query']=='music tempo heart rate matching'
    assert match['requested_query']=='heart rate matching music tempo'
    assert cache.stats()['weekly_usage'][0]['similar_hits']==1
    assert cache.get('dblp','heart rate matching music tempo') is None


def test_versions_negations_and_error_codes_do_not_cross_match(tmp_path):
    cache=VectorCache(tmp_path/'db',clock=lambda:1000,policy=CachePolicy(similarity=.1))
    for query,other in [('Gradle 8.4 wrapper','Gradle 8.5 wrapper'),('session not recording','session recording'),('Java ClassNotFoundException','Java IOException')]:
        cache.put('zenodo',query,result(query))
        assert cache.get('zenodo',other) is None


def test_expired_evidence_is_not_reused_even_if_frequently_used(tmp_path):
    now=[1000]
    cache=VectorCache(tmp_path/'db',clock=lambda:now[0])
    cache.put('zenodo','music query',result('music query'))
    now[0]+=13*DAY
    assert cache.get('zenodo','music query')
    now[0]+=2*DAY
    assert cache.get('zenodo','music query') is None


def test_retention_dry_run_hot_entries_and_rate_state(tmp_path):
    now=[1000]
    cache=VectorCache(tmp_path/'db',clock=lambda:now[0])
    cache.put('zenodo','cold query',result('cold query'))
    now[0]+=20*DAY
    cache.put('zenodo','hot query',result('hot query'))
    with sqlite3.connect(cache.path) as c:
        c.execute('create table requests (at REAL)')
        c.execute('insert into requests values (123)')
    now[0]+=10*DAY
    assert cache.prune(dry_run=True)['candidates']==1
    assert cache.stats()['entries']==2
    assert cache.prune()['deleted']==1
    assert cache.get('zenodo','hot query')
    with sqlite3.connect(cache.path) as c: assert c.execute('select count(*) from requests').fetchone()[0]==1


def test_max_age_purges_even_recently_used_metadata(tmp_path):
    now=[1000]
    cache=VectorCache(tmp_path/'db',clock=lambda:now[0],policy=CachePolicy(fresh_days=100,idle_weeks=100))
    cache.put('zenodo','hot query',result('hot query'))
    now[0]+=80*DAY
    assert cache.get('zenodo','hot query')
    now[0]+=5*DAY
    assert cache.prune()['deleted']==1


def test_capacity_prefers_recently_used_entries(tmp_path):
    now=[1000]
    cache=VectorCache(tmp_path/'db',clock=lambda:now[0],policy=CachePolicy(max_entries=2))
    cache.put('zenodo','first query',result('first query'))
    now[0]+=1;cache.put('zenodo','second query',result('second query'))
    now[0]+=1;cache.get('zenodo','first query')
    now[0]+=1;cache.put('zenodo','third query',result('third query'))
    assert cache.get('zenodo','second query') is None
    assert cache.get('zenodo','first query')
    assert cache.stats()['entries']==2


def test_migrates_existing_cache_without_network(tmp_path):
    path=tmp_path/'db';query='old query';key=VectorCache.key('zenodo',query)
    with sqlite3.connect(path) as c:
        c.execute('create table cache (key TEXT PRIMARY KEY,at REAL,body TEXT)')
        c.execute('insert into cache values (?,?,?)',(key,1000,json.dumps(result(query))))
    cache=VectorCache(path,clock=lambda:1001)
    assert cache.get('zenodo',query)['cache_match']=='exact'


async def test_similarity_cache_avoids_api_reservation(tmp_path):
    def no_network(_): raise AssertionError('network should not run')
    engine=ResearchSearch(tmp_path/'db',transport=httpx.MockTransport(no_network),clock=lambda:1000)
    engine.cache.put('zenodo','music tempo matching',result('music tempo matching'))
    assert (await engine.search('matching music tempo','matching error','zenodo'))['cached']
    with engine.connect() as c:
        assert c.execute('select count(*) from requests').fetchone()[0]==0


async def test_forced_refresh_still_respects_budget(tmp_path):
    engine=ResearchSearch(tmp_path/'db',clock=lambda:1000)
    engine.cache.put('zenodo','music query',result('music query'))
    with engine.connect() as c:
        c.execute('insert into limits values (?,?)',('zenodo',1300))
    value=await engine.search('music query','matching error','zenodo',force_refresh=True)
    assert value['status']=='rate_limited'


def test_configured_policy_validates_environment(tmp_path,monkeypatch):
    monkeypatch.setenv('MOBYAI_RESEARCH_CACHE_IDLE_WEEKS','6')
    assert VectorCache(tmp_path/'db').policy.idle_weeks==6
    monkeypatch.setenv('MOBYAI_RESEARCH_CACHE_MAX_ENTRIES','0')
    with pytest.raises(ValueError): VectorCache(tmp_path/'db2')
