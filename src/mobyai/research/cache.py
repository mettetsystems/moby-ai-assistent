"""Bounded local sparse-vector cache; no embedding service, GPU, or network."""
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

DAY = 86400


def vector(text):
    counts = Counter(re.findall(r'[\w]+(?:[.][\w]+)*', text.lower()))
    norm = math.sqrt(sum(value*value for value in counts.values())) or 1
    return {term: count/norm for term, count in counts.items()}


def guards(text):
    # Similar wording must not blur runtime versions or site restrictions.
    return sorted(re.findall(r'\b\d+(?:\.\d+)*\b|site:\S+|\b(?:not|no|without)\b', text.lower()) + re.findall(r'\b\w*(?:Error|Exception)\b|\b[A-Z][A-Z_]{3,}\b', text))


@dataclass(frozen=True)
class CachePolicy:
    fresh_days: int = 14
    idle_weeks: int = 4
    max_age_weeks: int = 12
    max_entries: int = 2000
    similarity: float = .92

    @classmethod
    def from_environment(cls):
        return cls(**{field:int(os.environ['MOBYAI_RESEARCH_CACHE_'+field.upper()])
                      for field in ('fresh_days','idle_weeks','max_age_weeks','max_entries')
                      if 'MOBYAI_RESEARCH_CACHE_'+field.upper() in os.environ})

    def __post_init__(self):
        if min(self.fresh_days,self.idle_weeks,self.max_age_weeks,self.max_entries) < 1:
            raise ValueError('Cache durations and entry limit must be positive')
        if not 0 < self.similarity <= 1:
            raise ValueError('Similarity must be in (0, 1]')


class VectorCache:
    def __init__(self, path, *, policy=None, clock=time.time):
        self.path = Path(path)
        self.policy, self.clock = policy or CachePolicy.from_environment(), clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, at REAL, body TEXT)')
            c.execute('''CREATE TABLE IF NOT EXISTS research_vectors (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT NOT NULL,
                vector TEXT NOT NULL, guards TEXT NOT NULL, fetched REAL NOT NULL,
                last_used REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0)''')
            c.execute('CREATE INDEX IF NOT EXISTS research_source ON research_vectors(source)')
            c.execute('CREATE TABLE IF NOT EXISTS research_usage (week INTEGER PRIMARY KEY, exact_hits INTEGER DEFAULT 0, similar_hits INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS research_maintenance (id INTEGER PRIMARY KEY, at REAL)')
            # Migrate existing cached metadata without refetching providers.
            for key,at,body in c.execute('SELECT key,at,body FROM cache WHERE key NOT IN (SELECT key FROM research_vectors)').fetchall():
                data = json.loads(body)
                if data.get('source') and data.get('query'):
                    self._index(c,key,data['source'],data['query'],at)

    def connect(self):
        return sqlite3.connect(self.path,timeout=5)

    @staticmethod
    def key(source,query):
        return hashlib.sha256((source+'\0'+query.lower()).encode()).hexdigest()

    def _index(self,c,key,source,query,at):
        c.execute('''INSERT INTO research_vectors VALUES (?,?,?,?,?,?,?,0)
            ON CONFLICT(key) DO UPDATE SET vector=excluded.vector, guards=excluded.guards,
            fetched=excluded.fetched, last_used=excluded.last_used''',
            (key,source,query,json.dumps(vector(query)),json.dumps(guards(query)),at,at))

    def get(self,source,query):
        self.prune(automatic=True)
        now=self.clock(); key=self.key(source,query)
        wanted=vector(query); best=None; score=0
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute('''SELECT v.key,v.query,v.vector,v.guards,c.body,c.at
                FROM research_vectors v JOIN cache c ON c.key=v.key
                WHERE v.source=? AND c.at>=?''',(source,now-self.policy.fresh_days*DAY)).fetchall()
            for row in rows:
                data=json.loads(row[4])
                if not data.get('results') and now-row[5]>DAY:
                    continue
                stored_vector=json.loads(row[2])
                similarity=1.0 if row[0]==key else sum(v*stored_vector.get(k,0) for k,v in wanted.items())
                if row[0]!=key and (not data.get('results') or json.loads(row[3])!=guards(query)):
                    continue
                if similarity>=self.policy.similarity and (best is None or similarity>score or row[0]==key):
                    best,score=row,similarity
                    if row[0]==key: break
            if best is None: return None
            match='exact' if best[0]==key else 'similar'
            c.execute('UPDATE research_vectors SET last_used=?,hits=hits+1 WHERE key=?',(now,best[0]))
            week=int(now//(7*DAY))
            c.execute('INSERT OR IGNORE INTO research_usage(week) VALUES (?)',(week,))
            column='exact_hits' if match=='exact' else 'similar_hits'
            c.execute(f'UPDATE research_usage SET {column}={column}+1 WHERE week=?',(week,))
            return {**json.loads(best[4]),'cached':True,'cache_match':match,
                    'similarity':round(score,4),'requested_query':query,
                    'cache_age_days':round((now-best[5])/DAY,2)}

    def put(self,source,query,result):
        now=self.clock(); key=self.key(source,query)
        with closing(self.connect()) as c, c:
            c.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(key,now,json.dumps(result)))
            self._index(c,key,source,query,now)
        self.prune(automatic=False)

    def prune(self,*,dry_run=False,automatic=False):
        now=self.clock()
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            last=c.execute('SELECT at FROM research_maintenance WHERE id=1').fetchone()
            if automatic and last and now-last[0]<DAY: return {'deleted':0}
            rows=c.execute('SELECT key,fetched,last_used,hits FROM research_vectors').fetchall()
            expired={key for key,fetched,used,hits in rows if now-used>=self.policy.idle_weeks*7*DAY or now-fetched>=self.policy.max_age_weeks*7*DAY}
            survivors=[row for row in rows if row[0] not in expired]
            # Recency dominates under pressure; hit count breaks ties.
            survivors.sort(key=lambda row:(row[2],row[3]))
            expired.update(row[0] for row in survivors[:max(0,len(survivors)-self.policy.max_entries)])
            if not dry_run:
                c.executemany('DELETE FROM cache WHERE key=?',[(key,) for key in expired])
                c.executemany('DELETE FROM research_vectors WHERE key=?',[(key,) for key in expired])
                c.execute('DELETE FROM research_usage WHERE week<?',(int(now//(7*DAY))-12,))
                c.execute('INSERT OR REPLACE INTO research_maintenance VALUES (1,?)',(now,))
            return {'dry_run':dry_run,'candidates':len(expired),'deleted':0 if dry_run else len(expired),'remaining':len(rows)-len(expired)}

    def lookup(self,text,limit=5):
        """Hybrid candidate retrieval, not proof that a cached document fixes a bug."""
        self.prune(automatic=True)
        now=self.clock(); terms=vector(text)
        with closing(self.connect()) as c:
            rows=c.execute('SELECT v.key,v.query,c.body,c.at FROM research_vectors v JOIN cache c ON v.key=c.key WHERE c.at>=?',(now-self.policy.fresh_days*DAY,)).fetchall()
        docs=[]
        for key,query,body,at in rows:
            data=json.loads(body)
            for item in data.get('results',[]):
                words=re.findall(r'[\w]+(?:[.][\w]+)*',(query+' '+item.get('title','')+' '+item.get('snippet','')).lower())
                docs.append((key,item,Counter(words),data.get('source'),at))
        if not docs:return {'status':'ok','results':[]}
        average=sum(sum(d[2].values()) for d in docs)/len(docs)
        frequencies=Counter(term for d in docs for term in d[2])
        ranked=[]
        for key,item,counts,source,at in docs:
            size=sum(counts.values()); norm=math.sqrt(sum(v*v for v in counts.values())) or 1
            cosine=sum(value*counts.get(term,0)/norm for term,value in terms.items())
            bm25=0.0
            for term in terms:
                freq=counts.get(term,0)
                if freq:
                    idf=math.log(1+(len(docs)-frequencies[term]+.5)/(frequencies[term]+.5))
                    bm25+=idf*freq*2.2/(freq+1.2*(.25+.75*size/(average or 1)))
            if cosine>=.1 and bm25>0:
                ranked.append({'cache_key':key,**item,'source':source,'age_days':round((now-at)/DAY,2),'cosine':cosine,'bm25':bm25})
        # Reciprocal rank fusion avoids comparing incompatible raw score scales.
        scores={id(item):0.0 for item in ranked}
        for metric in ('cosine','bm25'):
            for rank,item in enumerate(sorted(ranked,key=lambda x:x[metric],reverse=True),1):scores[id(item)]+=1/(60+rank)
        result=[];seen=set()
        for item in sorted(ranked,key=lambda x:scores[id(x)],reverse=True):
            if item['url'] not in seen:
                seen.add(item['url']); result.append({**item,'hybrid_score':scores[id(item)]})
            if len(result)>=limit:break
        with closing(self.connect()) as c,c:
            for key in {item['cache_key'] for item in result}:
                c.execute('UPDATE research_vectors SET last_used=?,hits=hits+1 WHERE key=?',(now,key))
        return {'status':'ok','results':result,'note':'Local cached candidates only. Compare versions and log evidence before deciding applicability; retrieval is not validation.'}

    def stats(self):
        with closing(self.connect()) as c:
            count,hits=c.execute('SELECT count(*),coalesce(sum(hits),0) FROM research_vectors').fetchone()
            return {'entries':count,'retained_entry_hits':hits,'database_bytes':self.path.stat().st_size,
                    'weekly_usage':[{'week_start_epoch':week*7*DAY,'exact_hits':exact,'similar_hits':similar}
                                    for week,exact,similar in c.execute('SELECT * FROM research_usage ORDER BY week DESC LIMIT 12')]}


def main():
    import os
    parser=argparse.ArgumentParser(description='Inspect or prune the local research vector cache. Purge defaults to dry-run.')
    parser.add_argument('action',choices=['stats','purge'])
    parser.add_argument('--path',default=os.environ.get('MOBYAI_RESEARCH_CACHE',str(Path.home()/'.cache/mobyai/research.sqlite3')))
    parser.add_argument('--idle-weeks',type=int,default=4)
    parser.add_argument('--max-age-weeks',type=int,default=12)
    parser.add_argument('--max-entries',type=int,default=2000)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    cache=VectorCache(args.path,policy=CachePolicy(idle_weeks=args.idle_weeks,max_age_weeks=args.max_age_weeks,max_entries=args.max_entries))
    print(json.dumps(cache.stats() if args.action=='stats' else cache.prune(dry_run=not args.apply),indent=2))

if __name__=='__main__': main()
