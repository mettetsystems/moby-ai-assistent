"""Portable verified exports; never overwrite an existing destination."""
import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from mobyai.models.records import RunRecord, StepRunRecord, PlanRecord


async def export_records(store, project_id: str, destination: Path):
    bundle = await store.records.snapshot(project_id)
    pending = getattr(store, '_record_warnings', {}).get(project_id, [])
    if pending:
        for record in [bundle.run, bundle.plan, *bundle.steps]:
            record.completeness = 'partial'
            record.warnings.extend(pending)
    destination = Path(destination).absolute()
    if any(p.is_symlink() for p in [destination, *destination.parents]):
        raise ValueError('Refusing symlinked export directory')
    destination.mkdir(parents=True, exist_ok=False)
    files, unavailable = [], []

    def write(relative, body):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(body)
        path.chmod(0o600)
        files.append({'path': relative, 'sha256': sha256(body).hexdigest(), 'size_bytes': len(body)})

    for ref in bundle.artifacts:
        try:
            body = await store.records.read_artifact(ref)
        except (OSError, KeyError, ValueError) as exc:
            unavailable.append({'artifact': ref.model_dump(mode='json'), 'error': str(exc)})
            continue
        # Construct the destination from the validated hash, never a stored path.
        await asyncio.to_thread(write, f'artifacts/{ref.sha256[:2]}/{ref.sha256}', body)
    if unavailable:
        for record in [bundle.run, bundle.plan, *bundle.steps]:
            record.completeness = 'partial'
            record.warnings.append(f'{len(unavailable)} referenced artifacts are missing or corrupt; see manifest.json.')
    for path, record in [('run.json', bundle.run), ('plan.json', bundle.plan),
                         *[(f'steps/{s.id}.json', s) for s in bundle.steps]]:
        await asyncio.to_thread(write, path, record.model_dump_json(indent=2).encode('utf-8'))
    for schema in (RunRecord, StepRunRecord, PlanRecord):
        await asyncio.to_thread(write, f'schemas/{schema.__name__}.schema.json',
                                json.dumps(schema.model_json_schema(), indent=2).encode('utf-8'))
    manifest = dict(schema_version=1, exported_at=datetime.now(UTC).isoformat(),
                    run_id=str(bundle.run.id), project_id=project_id,
                    completeness=bundle.run.completeness, files=files,
                    unavailable_artifacts=unavailable)
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (destination / 'manifest.json').chmod(0o600)
    return manifest
