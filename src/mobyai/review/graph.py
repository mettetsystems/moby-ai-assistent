"""CPU-only AST graph + FTS5/BM25 seeds and adjacent-node retrieval.

Edges are static evidence, not a claim of whole-program call resolution.
No source code is executed and no parsers are downloaded during indexing.
"""
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3

SKIP = {'.git', '.venv', 'venv', 'node_modules', 'mobyMinutes', '__pycache__', 'dist', 'build', '.next', 'vendor', '.agent', 'artifacts'}
LANGUAGES = {'.js':('javascript','language'), '.jsx':('javascript','language'),
 '.ts':('typescript','language_typescript'), '.tsx':('typescript','language_tsx'),
 '.go':('go','language'), '.rs':('rust','language'), '.java':('java','language'),
 '.c':('c','language'), '.h':('c','language'), '.cpp':('cpp','language')}
TEXT = {'.py', *LANGUAGES, '.json', '.toml', '.md', '.yaml', '.yml', '.sh', '.css', '.html', '.sql', '.swift', '.kt', '.kts', '.dart', '.cs', '.rb', '.php', '.vue', '.svelte', '.ex', '.exs', '.lua'}


def source_files(root):
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.') and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            path = Path(directory)/name
            if path.is_symlink() or name.startswith('.') or path.suffix not in TEXT:
                continue
            if any(word in name.lower() for word in ('secret', 'credential', 'token', 'lock.json', 'lock.yaml')):
                continue
            count += 1
            if count > 10000:
                raise ValueError('Codebase exceeds 10,000 eligible files; choose a smaller project root')
            if path.stat().st_size <= 512_000:
                yield path


def fingerprint(root):
    root=Path(root).resolve()
    digest=hashlib.sha256()
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            path=Path(directory)/name
            digest.update(str(path.relative_to(root)).encode()+b'\0')
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
                continue
            with path.open('rb') as stream:
                while chunk:=stream.read(1024*1024): digest.update(chunk)
    return digest.hexdigest()


def build(root, destination):
    root, destination = Path(root).resolve(), Path(destination)
    temporary = destination.with_suffix('.building.sqlite3')
    if destination.is_symlink() or temporary.is_symlink():
        raise ValueError('Unsafe graph path')
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    db.executescript('CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,line INT,end_line INT,kind TEXT,name TEXT,body TEXT); CREATE TABLE edges(source TEXT,target TEXT,kind TEXT); CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,body); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);')
    warnings, files, symbols, references = [], {}, {}, []
    count = 0
    def node(id, path, start, end, kind, name, body):
        db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?)',(id,path,start,end,kind,name,body[:6000]))
        db.execute('INSERT INTO search VALUES(?,?)',(id, name+' '+path+' '+body[:6000]))
    def edge(a,b,kind):
        db.execute('INSERT INTO edges VALUES(?,?,?)',(a,b,kind))
    try:
        for path in source_files(root):
            count += 1
            rel = path.relative_to(root).as_posix()
            source = path.read_text(encoding='utf-8',errors='replace')
            lines = source.splitlines()
            file_id = 'file:'+rel
            files[rel] = file_id
            node(file_id,rel,1,len(lines),'file',path.stem,source)
            previous = file_id
            def add_symbol(name,start,end,kind):
                nonlocal previous
                id = f'{rel}:{start}:{name}'
                node(id,rel,start,end,kind,name,'\n'.join(lines[start-1:end]))
                edge(file_id,id,'contains'); edge(previous,id,'neighbor')
                previous=id
                symbols.setdefault(name,[]).append(id)
                return id
            if path.suffix == '.py':
                try:
                    tree = ast.parse(source)
                except SyntaxError as exc:
                    warnings.append(f'{rel}: Python parse error at line {exc.lineno}'); continue
                for item in ast.walk(tree):
                    if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                        id=add_symbol(item.name,item.lineno,item.end_lineno,type(item).__name__)
                        for child in ast.walk(item):
                            if isinstance(child,ast.Call):
                                name=getattr(child.func,'id',None) or getattr(child.func,'attr',None)
                                if name: references.append((id,name,'call_candidate'))
                    if isinstance(item,(ast.Import,ast.ImportFrom)):
                        names=[a.name for a in item.names] if isinstance(item,ast.Import) else [item.module or '']
                        for name in names: references.append((file_id,name,'import'))
            elif path.suffix in LANGUAGES:
                try:
                    from tree_sitter import Language, Parser
                    language, factory=LANGUAGES[path.suffix]
                    module=importlib.import_module('tree_sitter_'+language)
                    tree=Parser(Language(getattr(module,factory)())).parse(source.encode())
                except ImportError:
                    warnings.append(f'{rel}: AST parser unavailable; install mobyai[archaeology]'); continue
                if tree.root_node.has_error: warnings.append(f'{rel}: tree-sitter reports syntax errors; graph is partial')
                stack=[(tree.root_node,file_id)]
                while stack:
                    item, owner=stack.pop()
                    if item.type in {'function_declaration','function_definition','method_definition','method_declaration','class_declaration','class_definition','struct_item','function_item','interface_declaration','variable_declarator','type_alias_declaration'}:
                        name=item.child_by_field_name('name')
                        if name: owner=add_symbol(name.text.decode(errors='replace'),item.start_point.row+1,item.end_point.row+1,item.type)
                    if item.type in {'call_expression','method_invocation'}:
                        function=item.child_by_field_name('function') or item.child_by_field_name('name')
                        if function:
                            name=function.text.decode(errors='replace').split('.')[-1]
                            references.append((owner,name,'call_candidate'))
                    if item.type in {'import_statement','import_declaration','import_from_statement'}:
                        text=item.text.decode(errors='replace')
                        for name in re.findall(r'[\"\']([^\"\']+)[\"\']',text): references.append((file_id,name,'import'))
                    stack.extend((child,owner) for child in reversed(item.named_children))
            elif path.suffix not in {'.json','.toml','.md','.yaml','.yml'}:
                warnings.append(f'{rel}: lexical/file indexing only (no AST adapter)')
        for owner,name,kind in references:
            if kind == 'call_candidate':
                for target in symbols.get(name,[])[:20]: edge(owner,target,kind)
            else:
                owner_path=owner.removeprefix('file:')
                candidates={name.replace('.','/')+'.py', name.replace('.','/')+'/__init__.py'}
                if name.startswith('.') and '/' in name:
                    base=os.path.normpath(str(Path(owner_path).parent/name))
                    candidates.update(base+suffix for suffix in ('','.js','.ts','.tsx','/index.js','/index.ts'))
                for candidate in candidates:
                    if candidate in files: edge(owner,files[candidate],'import')
        db.execute('CREATE INDEX edges_source ON edges(source)')
        db.execute('CREATE INDEX edges_target ON edges(target)')
        metadata={'files':count,'inventory':sorted(files),'nodes':db.execute('SELECT count(*) FROM nodes').fetchone()[0], 'warnings':warnings,
                  'fingerprint':fingerprint(root),'method':'AST symbols + file containment + imports + candidate calls + lexical neighbors; BM25 seeds + one-hop bidirectional graph expansion'}
        for key,value in metadata.items(): db.execute('INSERT INTO metadata VALUES(?,?)',(key,json.dumps(value)))
        db.commit()
    finally:
        db.close()
    temporary.replace(destination)
    return metadata


def retrieve(path, query, limit=12):
    terms=list(dict.fromkeys(re.findall(r'\w+',query)))[:32]
    with sqlite3.connect(f'file:{Path(path)}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        seeds=db.execute('SELECT id FROM search WHERE search MATCH ? ORDER BY bm25(search) LIMIT ?',(' OR '.join('"'+t+'"' for t in terms) or '"function"',limit)).fetchall()
        ids=[row['id'] for row in seeds]
        if not ids: ids=[r[0] for r in db.execute('SELECT id FROM nodes WHERE kind="file" ORDER BY path LIMIT ?',(limit,))]
        expanded=list(ids)
        for id in ids:
            for row in db.execute('SELECT target FROM edges WHERE source=? UNION SELECT source FROM edges WHERE target=? LIMIT 8',(id,id)):
                if row[0] not in expanded: expanded.append(row[0])
        result=[]
        remaining=24000
        for id in expanded[:limit*3]:
            row=db.execute('SELECT * FROM nodes WHERE id=?',(id,)).fetchone()
            if row:
                item=dict(row)
                size=min(2000,remaining)
                if size<=0: break
                item['excerpt_truncated']=len(item['body'])>size
                item['body']=item['body'][:size]
                remaining-=len(item['body'])
                result.append({**item,'retrieval':'seed' if id in ids else 'graph_neighbor'})
        return result
