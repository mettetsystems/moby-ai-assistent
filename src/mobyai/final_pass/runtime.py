"""Pinned Colibri CPU runtime. Compile/download explicitly; load only during a final pass."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time

import httpx

from mobyai.review.minutes import write, read
from mobyai.engine.lease import project_lease, project_is_running
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.models.inference import ChatMessage, ModelRequest

COLIBRI_REVISION = 'a8f2ca623ffe9de9df11d56f34d11d2d501493d3'
COLIBRI_URL = 'https://github.com/mettetsystems/colibri.git'


def identity(config):
    data=config.model_dump(mode='json',exclude={'enabled'})
    data['colibri_revision']=COLIBRI_REVISION
    return sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def paths(config):
    root=config.directory / identity(config)[:20]
    return root,root/'source',root/'weights'


def status(config):
    root,_,_=paths(config)
    path=root/'status.json'
    result=read(path) if path.is_file() else {'state':'not_built','message':'Pre-build inference before enabling this model.'}
    if result['state']=='building' and not project_is_running(root/'runtime.db','build'):
        result={'state':'interrupted','message':'The previous build is no longer running. Retry Pre-build; partial weights are retained.'}
    return {**result,'directory':str(root),'model':config.model}


def publish(config,state,message):
    root,_,_=paths(config)
    write(root/'status.json',{'state':state,'message':message,'identity':identity(config)})


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


@asynccontextmanager
async def inference(config,*,require_ready=True):
    root,source,weights=paths(config)
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
            publish(config,'building','Fetching the pinned Colibri source; see build.log for progress.')
            if not source.exists():
                await command(['git','clone','--no-checkout',COLIBRI_URL,source],log)
            await command(['git','-C',source,'checkout','--detach',COLIBRI_REVISION],log)
            publish(config,'building','Checking model download size and free space.')
            from huggingface_hub import HfApi
            from mobyai.inference.huggingface import resolve_huggingface_token
            info=await asyncio.to_thread(HfApi().model_info,config.weights_repo,files_metadata=True,
                                         token=resolve_huggingface_token(app_config))
            if not info.sha or any(f.size is None for f in info.siblings):
                raise ValueError('Cannot verify model download size; preparation stopped.')
            total=sum(f.size for f in info.siblings)
            present=sum(p.stat().st_size for p in weights.rglob('*') if p.is_file()) if weights.exists() else 0
            needed=max(0,total-present)+10*1024**3
            if shutil.disk_usage(root).free<needed:
                raise ValueError(f'Model requires about {total/1024**3:.1f} GiB plus headroom; choose a larger Colibri storage directory in Settings.')
            publish(config,'building','Downloading model weights; this can take considerable time. See build.log.')
            hf=Path(sys.executable).parent/'hf'
            if not hf.is_file(): raise ValueError('Install huggingface-hub CLI in the MobyAI virtual environment.')
            from mobyai.inference.huggingface import huggingface_environ
            env=environment();env.update(huggingface_environ(app_config))
            await command([hf,'download',config.weights_repo,'--revision',info.sha,'--local-dir',weights],log,env=env,timeout=86400)
            # Colibri resolves the actual architecture; never infer support from a model name.
            publish(config,'building','Compiling the architecture-specific CPU engine.')
            await command([sys.executable,source/'c'/'coli','build','--model',weights],log)
            publish(config,'building','Loading inference and checking a real generated response.')
            async with inference(config,require_ready=False) as provider:
                result=await provider.generate(ModelRequest(messages=[ChatMessage(role='user',content='Reply with exactly READY.')],
                    max_tokens=16,json_mode=False,temperature=0))
                if not result.content.strip(): raise ValueError('Colibri smoke test returned no usable content.')
            write(root/'build-manifest.json',{'model_revision':info.sha,'colibri_revision':COLIBRI_REVISION,'model':config.model,'weights_repo':config.weights_repo})
            publish(config,'ready','Built and smoke-tested. Model unloads between final passes; no regular agent settings were changed.')
        except asyncio.CancelledError:
            publish(config,'cancelled','Pre-build cancelled. Partial downloads are retained for retry.')
            raise
        except Exception as exc:
            publish(config,'failed',str(exc))
