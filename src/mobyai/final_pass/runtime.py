"""Pinned Colibri CPU runtime. Prepare local models explicitly; load only during a final pass."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import sys
import time

import httpx

from mobyai.review.minutes import write, read
from mobyai.engine.lease import project_lease, project_is_running
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.models.inference import ChatMessage, ModelRequest

COLIBRI_REVISION = 'a8f2ca623ffe9de9df11d56f34d11d2d501493d3'
COLIBRI_URL = 'https://github.com/mettetsystems/colibri.git'
_status_fallback = {}
_logger = logging.getLogger(__name__)


def identity(config):
    data=config.model_dump(mode='json',exclude={'enabled'})
    data['colibri_revision']=COLIBRI_REVISION
    return sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def paths(config):
    # The configured storage location may intentionally point to another drive.
    # Resolve only that trusted base, retaining checks on managed descendants.
    root=config.directory.resolve() / identity(config)[:20]
    if root.is_symlink():
        raise ValueError('Refusing symlinked Colibri build directory')
    return root,root/'source',root/'weights'


def status(config):
    fallback=_status_fallback.get(identity(config))
    if fallback is not None:
        return {**fallback,'directory':str(config.directory / identity(config)[:20]),'model':config.model}
    root,_,_=paths(config)
    path=root/'status.json'
    result=read(path) if path.is_file() else {'state':'not_built','message':'Pre-build inference before enabling this model.'}
    if result['state']=='building' and not project_is_running(root/'runtime.db','build'):
        result={'state':'interrupted','message':'The previous build is no longer running. Retry Pre-build; completed local copies are retained.'}
    return {**result,'directory':str(root),'model':config.model,
            'logs':{name:log_tail(root/name) for name in ('build.log','inference.log')}}


def log_tail(path,limit=32768):
    """Read a bounded tail of a local log without following redirected files."""
    try:
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return {'error':'Log is not a regular file.'}
            start=max(0,info.st_size-limit)
            stream.seek(start)
            content=stream.read(limit).decode('utf-8',errors='replace').replace('\r\n','\n').replace('\r','\n')
            lines=content.splitlines()
            return {'text':'\n'.join(lines[-200:]),'bytes':info.st_size,
                    'truncated':start>0 or len(lines)>200,
                    'updated_at':datetime.fromtimestamp(info.st_mtime,timezone.utc).isoformat()}
    except FileNotFoundError:
        return {'text':'','bytes':0,'truncated':False}
    except OSError as exc:
        return {'error':f'Log unavailable: {exc.strerror}'}


def publish(config,state,message):
    root,_,_=paths(config)
    now=datetime.now(timezone.utc).isoformat()
    write(root/'status.json',{'state':state,'message':message,'identity':identity(config),'updated_at':now})
    _status_fallback.pop(identity(config),None)
    try:
        with os.fdopen(os.open(root/'build.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600),'a') as stream:
            stream.write(f'\n[{now}] {state}: {message}\n')
    except OSError as exc:
        _logger.warning('Could not append Colibri progress: %s',exc)


def publish_best_effort(config,state,message):
    """Keep failure status visible even when its status file cannot be written."""
    try:
        publish(config,state,message)
    except Exception as exc:
        _status_fallback[identity(config)]={
            'state':state,
            'message':f'{message} (Could not persist build status: {exc})',
        }
        _logger.warning('Could not persist Colibri %s status: %s; original message: %s',state,exc,message)


async def terminate(process):
    if process.returncode is not None: return
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: return
    try: await asyncio.wait_for(process.wait(),15)
    except TimeoutError:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        await process.wait()


def environment():
    # No unrelated API tokens or caller COLI_* configuration reaches this runtime.
    env={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR','HTTPS_PROXY','HTTP_PROXY','NO_PROXY') if k in os.environ}
    env['CUDA_VISIBLE_DEVICES']=''
    env['PYTHONUNBUFFERED']='1'
    env['HF_HUB_OFFLINE']='1'
    env['TRANSFORMERS_OFFLINE']='1'
    return env


async def command(argv,log,*,env=None,timeout=3600):
    with log.open('ab') as stream:
        process=await asyncio.create_subprocess_exec(*map(str,argv),stdout=stream,stderr=asyncio.subprocess.STDOUT,
            env=env or environment(),start_new_session=True)
        try:
            code=await asyncio.wait_for(process.wait(),timeout)
            if code: raise ValueError(f'Colibri preparation exited {code}; inspect {log}')
        finally:
            await terminate(process)


def local_inventory(directory):
    if not directory.is_dir() or not (directory/'config.json').is_file():
        raise ValueError('Choose a complete local Colibri-compatible model directory containing config.json.')
    files=[]
    for path in sorted(directory.rglob('*')):
        relative=path.relative_to(directory)
        if any(part in {'.cache','.git'} for part in relative.parts):
            continue
        if path.is_symlink() and path.is_dir():
            raise ValueError(f'Model contains a directory symlink: {relative}. Select its real model directory.')
        if path.is_dir():
            continue
        info=path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Model entry is not a regular file: {relative}')
        files.append({'path':str(relative),'source':str(path.resolve()),'size':info.st_size,'mtime_ns':info.st_mtime_ns})
    if len(files)<2:
        raise ValueError('Local model directory has no model payload alongside config.json.')
    fingerprint=sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    return files,fingerprint


def same_storage(files,root):
    device=root.stat().st_dev
    return all(Path(item['source']).stat().st_dev==device for item in files)


async def prepare_weights(config,root,log):
    if config.model_directory is None:
        raise ValueError('Set Local model directory in Settings. Final passes never download model weights.')
    directory=config.model_directory.resolve()
    if root.is_relative_to(directory):
        raise ValueError('Choose runtime storage outside the source model directory.')
    publish(config,'building',f'Inspecting local model: {directory}')
    files,fingerprint=await asyncio.to_thread(local_inventory,directory)
    # The user selects the NVMe destination; matching filesystems avoid duplicate
    # weights. Check resolved files too, since HF snapshots can link to another drive.
    same_device=same_storage(files,root)
    copy=config.model_placement=='copy' or (config.model_placement=='auto' and not same_device)
    weights=root/'weights'/fingerprint if copy else directory
    if copy and any(p.is_symlink() for p in (root/'weights',weights)):
        raise ValueError('Refusing symlinked model staging directory')
    pending=[]
    for item in files:
        target=weights/item['path']
        if copy:
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise ValueError('Refusing symlinked model staging path')
            info=target.stat() if target.is_file() else None
            if info is None or (info.st_size,info.st_mtime_ns)!=(item['size'],item['mtime_ns']):
                pending.append(item)
    total=sum(item['size'] for item in files)
    needed=sum(item['size'] for item in pending)+10*1024**3
    if shutil.disk_usage(root).free<needed:
        raise ValueError(f'Preparation requires {needed/1024**3:.1f} GiB free including build headroom; choose a larger NVMe storage directory.')
    publish(config,'building',f'Local model: {total/1024**3:.2f} GiB in {len(files)} files. '
            + (f'Copying {len(pending)} files to {weights}.' if copy else f'Using model in place at {weights}; no copy needed.'))
    for index,item in enumerate(pending,1):
        target=weights/item['path']
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.moby-partial')
        if temporary.is_symlink():
            raise ValueError('Refusing symlinked partial model copy')
        publish(config,'building',f'Copying file {index}/{len(pending)}: {item["path"]} ({item["size"]/1024**3:.2f} GiB).')
        await command(['cp','--reflink=auto','--preserve=timestamps','--verbose','--',item['source'],temporary],log,timeout=86400)
        info=temporary.stat()
        if (info.st_size,info.st_mtime_ns)!=(item['size'],item['mtime_ns']):
            raise ValueError('Model file changed during copying. Retry Pre-build with a stable source directory.')
        temporary.replace(target)
    if (await asyncio.to_thread(local_inventory,directory))[1]!=fingerprint:
        raise ValueError('Source model changed during preparation. Retry Pre-build.')
    return weights,{'model':config.model,'colibri_revision':COLIBRI_REVISION,
                    'source_directory':str(directory),'weights_directory':str(weights),
                    'placement':'copy' if copy else 'in_place','inventory_id':fingerprint,
                    'files':files,'smoke_test_passed':False}


def prepared_weights(root,default):
    manifest_path=root/'build-manifest.json'
    if not manifest_path.is_file():
        return default
    manifest=read(manifest_path)
    weights=Path(manifest['weights_directory'])
    for item in manifest['files']:
        info=(weights/item['path']).stat()
        if (info.st_size,info.st_mtime_ns)!=(item['size'],item['mtime_ns']):
            raise ValueError('Prepared model files changed. Run Pre-build again before using final inference.')
    return weights


@asynccontextmanager
async def inference(config,*,require_ready=True):
    root,source,weights=paths(config)
    weights=prepared_weights(root,weights)
    if require_ready and status(config)['state']!='ready':
        raise ValueError('Final inference is not ready. Use Pre-build inference in Settings.')
    # Kernel lease prevents competing console/CLI final runtimes for this model.
    with project_lease(root/'runtime.db','inference'):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        endpoint=f'http://127.0.0.1:{port}'
        import secrets
        key=secrets.token_urlsafe(32)
        env=environment();env['COLI_API_KEY']=key
        log=root/'inference.log'
        with log.open('ab') as stream:
            process=await asyncio.create_subprocess_exec(sys.executable,str(source/'c'/'coli'),'serve',
                '--model',str(weights),'--model-id',config.model,'--host','127.0.0.1','--port',str(port),
                '--ctx',str(config.context_tokens),'--cap',str(config.cache_slots),
                stdout=stream,stderr=asyncio.subprocess.STDOUT,env=env,start_new_session=True)
            provider=None
            try:
                deadline=time.monotonic()+min(600,config.timeout_seconds)
                async with httpx.AsyncClient(trust_env=False,timeout=5,headers={'Authorization':'Bearer '+key}) as client:
                    while time.monotonic()<deadline:
                        if process.returncode is not None:
                            raise ValueError(f'Colibri exited {process.returncode}; inspect {log}')
                        try:
                            response=await client.get(endpoint+'/v1/models')
                            if response.is_success and any(m.get('id')==config.model for m in response.json().get('data',[])):
                                break
                        except (httpx.HTTPError,ValueError): pass
                        await asyncio.sleep(1)
                    else: raise ValueError(f'Colibri startup timed out; inspect {log}')
                provider=OpenAICompatibleProvider(endpoint,config.model,api_key=key,
                    timeout_seconds=config.timeout_seconds,max_transport_retries=0,
                    client=httpx.AsyncClient(trust_env=False,timeout=config.timeout_seconds,
                        headers={'Authorization':'Bearer '+key}))
                yield provider
            finally:
                if provider: await provider._client.aclose()
                await terminate(process)


async def build(config,app_config):
    root,source,weights=paths(config)
    root.mkdir(parents=True,exist_ok=True)
    os.chmod(root,0o700)
    log=root/'build.log'
    with project_lease(app_config.persistence.database_path,'colibri-final-runtime'), project_lease(root/'runtime.db','build'):
        try:
            if not shutil.which('git') or not shutil.which('make') or not shutil.which('cc'):
                raise ValueError('Install git, make and a C compiler before pre-building Colibri.')
            weights,manifest=await prepare_weights(config,root,log)
            write(root/'build-manifest.json',manifest)
            publish(config,'building','Fetching the pinned Colibri engine source (no model download); see build.log for progress.')
            if not source.exists():
                await command(['git','clone','--progress','--no-checkout',COLIBRI_URL,source],log)
            await command(['git','-C',source,'checkout','--detach',COLIBRI_REVISION],log)
            # Colibri resolves the actual architecture; never infer support from a model name.
            publish(config,'building','Compiling the architecture-specific CPU engine.')
            await command([sys.executable,source/'c'/'coli','build','--model',weights],log)
            publish(config,'building','Loading inference and checking a real generated response.')
            async with inference(config,require_ready=False) as provider:
                result=await provider.generate(ModelRequest(messages=[ChatMessage(role='user',content='Reply with exactly READY.')],
                    max_tokens=16,json_mode=False,temperature=0))
                if not result.content.strip(): raise ValueError('Colibri smoke test returned no usable content.')
            write(root/'build-manifest.json',{**manifest,'smoke_test_passed':True})
            publish(config,'ready','Built and smoke-tested. Model unloads between final passes; no regular agent settings were changed.')
        except asyncio.CancelledError:
            publish_best_effort(config,'cancelled','Pre-build cancelled. Completed local model copies are retained for retry.')
            raise
        except Exception as exc:
            publish_best_effort(config,'failed',str(exc))
