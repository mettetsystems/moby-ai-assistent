"""Optional SSE transport. Partial model output is never executable output."""
import json
import time
import httpx
from mobyai.inference.base import InferenceError


async def stream_completion(client, url, payload, *, timeout_seconds, idle_seconds):
    try:
        return await _consume(client, url, payload, timeout_seconds=timeout_seconds, idle_seconds=idle_seconds)
    except httpx.ReadTimeout as exc:
        raise InferenceError(f"ReadTimeout: streaming connection idle for {min(timeout_seconds,idle_seconds)} seconds; inspect endpoint health. Partial output discarded.") from exc


async def _consume(client, url, payload, *, timeout_seconds, idle_seconds):
    content, reasoning = [], []
    usage, model, finish = {}, payload['model'], None
    size = 0
    started = time.monotonic()
    first = None
    last_notice = started
    timeout = httpx.Timeout(timeout_seconds, read=min(timeout_seconds, idle_seconds))
    async with client.stream('POST', url, json={**payload, 'stream': True}, timeout=timeout) as response:
        if response.status_code >= 400:
            await response.aread()
            return response
        async for line in response.aiter_lines():
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                chunk = json.loads(data)
                if not isinstance(chunk, dict) or chunk.get('error'):
                    raise ValueError('invalid stream event')
                if isinstance(chunk.get('usage'), dict):
                    usage = chunk['usage']
                model = chunk.get('model') or model
                choices = chunk.get('choices') or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get('delta') or {}
                for field, destination in (('content',content),('reasoning_content',reasoning)):
                    text = delta.get(field)
                    if text is not None:
                        if not isinstance(text,str):
                            raise ValueError('non-text delta')
                        destination.append(text)
                        size += len(text.encode('utf-8'))
                finish = choice.get('finish_reason') or finish
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                raise InferenceError('Malformed inference stream; partial output discarded') from exc
            if size > 16 * 1024 * 1024:
                raise InferenceError('Inference stream exceeded 16 MiB; partial output discarded')
            now = time.monotonic()
            if first is None:
                first = now
                await _progress('inference_stream_started', model, now-started, size)
            elif now-last_notice >= 15:
                await _progress('inference_stream_progress', model, now-started, size)
                last_notice = now
        if finish is None:
            raise InferenceError('Inference stream ended before a completion marker; partial output discarded')
    await _progress('inference_stream_finished', model, time.monotonic()-started, size)
    return httpx.Response(200,json={'model':model,'choices':[{'message':{
        'content':''.join(content),'reasoning_content':''.join(reasoning)},'finish_reason':finish}], 'usage':usage})


async def _progress(event, model, elapsed, size):
    from datetime import datetime, UTC
    from mobyai.engine.timeouts import current_policy
    from mobyai.models.event import EventRecord
    policy = current_policy()
    if policy is None:
        return
    project = await policy.store.projects.get(policy.project_id)
    await policy.store.events.append(EventRecord(timestamp=datetime.now(UTC),
        project_id=policy.project_id, task_id=project.current_task_id if project else None,
        component='inference', event=event,
        payload={'model':model,'elapsed_seconds':round(elapsed,2),'received_bytes':size}))
