"""Assemble a budgeted Worker payload. Independent of the inference provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from hashlib import sha256

from mobyai.context.errors import ContextLookupError, ContextBudgetError
from mobyai.context.contracts import contract_is_current
from mobyai.context.imports import candidate_import_paths
from mobyai.context.log_reducer import LogReducer, ReducedLog
from mobyai.context.tokenizer import estimate_tokens, truncate_to_tokens
from mobyai.context.types import FileRole, PackedFile, WorkerContext
from mobyai.execution.errors import WorkspaceError
from mobyai.execution.filesystem import WorkspaceFilesystem
from mobyai.models.attempt import AttemptResult, TaskAttempt
from mobyai.models.contract import ModuleContract
from mobyai.models.project import Project
from mobyai.models.task import Task
from mobyai.state.repository import StateStore

DEFAULT_CONSTRAINTS: tuple[str, ...] = (
    "Modify only files required for this task.",
    "Do not write outside the project workspace.",
    "Do not leave TODO placeholders for required behavior.",
    "Return structured tool calls; do not paste file bodies as chat.",
    "Execute only the assigned atomic task.",
)

_ROLE_PRIORITY = {
    FileRole.TARGET: 0,
    FileRole.TEST: 1,
    FileRole.REQUIRED_CONTEXT: 2,
    FileRole.IMPORT: 3,
}

_FAILED_ATTEMPTS = frozenset(
    {AttemptResult.FAIL, AttemptResult.ERROR, AttemptResult.TIMEOUT}
)


class ContextPacker:
    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        store: StateStore | None = None,
        *,
        log_reducer: LogReducer | None = None,
        default_token_budget: int = 48000,
        memory_token_budget: int = 2000,
        constraints: Sequence[str] | None = None,
        prefer_import_contracts: bool = True,
    ) -> None:
        self._filesystem = filesystem
        self._store = store
        self._log_reducer = log_reducer or LogReducer()
        self.default_token_budget = default_token_budget
        self.memory_token_budget = memory_token_budget
        self.constraints = tuple(constraints) if constraints is not None else DEFAULT_CONSTRAINTS
        self.prefer_import_contracts = prefer_import_contracts

    async def build_worker_context(
        self,
        project_id: str,
        task_id: str,
        token_budget: int,
        *,
        remediation_instruction: str | None = None,
    ) -> WorkerContext:
        if self._store is None:
            raise RuntimeError("ContextPacker requires a StateStore to load project state")
        project = await self._store.projects.get(project_id)
        if project is None:
            raise ContextLookupError(project_id)
        task = await self._store.tasks.get(project_id, task_id)
        if task is None:
            raise ContextLookupError(project_id, task_id)
        attempts = await self._store.attempts.list_for_task(project_id, task_id)
        reduced_error = await self._error_from_attempts(attempts)
        dependencies = []
        for dependency_id in task.dependencies:
            dependency = await self._store.tasks.get(project_id, dependency_id)
            if dependency is None:
                raise ContextLookupError(project_id, dependency_id)
            dependencies.append({
                "task_id": dependency.task_id, "status": dependency.status.value,
                "target_files": dependency.target_files,
                "acceptance_criteria": dependency.acceptance_criteria,
                "test_commands": dependency.test_commands,
            })
        contracts = {
            item.path: item
            for item in await self._store.contracts.list_for_project(project_id)
        }
        from mobyai.engine.recovery_evidence import fingerprint
        from mobyai.memory.assembly import working_set_for
        from mobyai.memory.tool_failures import recall_tool_failures

        # Use the same persisted harness error as record_attempt_memory.
        signature = fingerprint(project.last_error, task.test_commands) if project.last_error else None
        tool_memory = await recall_tool_failures(self._store, project_id, task,
            self._filesystem.workspace, token_budget=min(600, self.memory_token_budget // 2))
        shared_memory = await working_set_for(
            self._store, project_id, task_id,
            failure_signature=signature,
            token_budget=max(0, self.memory_token_budget - estimate_tokens(tool_memory) - 1),
        )
        shared_memory = "\n".join(block for block in (tool_memory, shared_memory) if block)
        return await self.pack(
            project,
            task,
            token_budget=token_budget,
            reduced_error=reduced_error,
            remediation_instruction=remediation_instruction,
            dependency_contracts=dependencies,
            contracts=contracts,
            shared_memory=shared_memory,
        )

    async def pack(
        self,
        project: Project,
        task: Task,
        *,
        token_budget: int | None = None,
        reduced_error: ReducedLog | None = None,
        remediation_instruction: str | None = None,
        dependency_contracts: list[dict[str, object]] | None = None,
        contracts: Mapping[str, ModuleContract] | None = None,
        shared_memory: str = "",
    ) -> WorkerContext:
        budget = token_budget if token_budget is not None else self.default_token_budget
        if budget < 1:
            raise ValueError("token_budget must be >= 1")

        candidates = await self._collect_files(task, contracts or {})
        context = WorkerContext(
            project_id=project.project_id,
            task_id=task.task_id,
            objective=project.objective,
            instruction=_instruction(task),
            architecture_summary=project.architecture_summary,
            acceptance_criteria=list(task.acceptance_criteria),
            test_commands=list(task.test_commands),
            constraints=list(self.constraints),
            contract={"target_files": list(task.target_files), "test_files": list(task.test_files), "required_context": list(task.required_context), "dependencies": list(task.dependencies)},
            dependency_contracts=dependency_contracts or [],
            files=[],
            reduced_error=reduced_error,
            remediation_instruction=remediation_instruction,
            omitted_paths=[],
            token_budget=budget,
            token_count=0,
        )
        remaining = budget - estimate_tokens(context.render())
        if remaining < 0:
            context = self._shrink_core(context, budget)
            remaining = budget - estimate_tokens(context.render())

        # Task evidence has priority; reserve memory before packing source files.
        memory_budget = min(self.memory_token_budget, max(0, remaining - 2))
        memory, _ = truncate_to_tokens(shared_memory, memory_budget)
        context = context.model_copy(update={"shared_memory": memory})
        remaining = budget - estimate_tokens(context.render())

        packed: list[PackedFile] = []
        omitted: list[str] = []
        for item in candidates:
            slot = max(remaining, 0)
            cost = estimate_tokens(_format_file(item))
            if cost <= slot:
                packed.append(item)
                remaining -= cost
                continue
            can_truncate = (
                item.role in {FileRole.TARGET, FileRole.TEST}
                and slot > 8
                and bool(item.content)
                and not item.missing
            )
            if can_truncate:
                truncated = _truncate_file(item, slot)
                packed.append(truncated)
                remaining -= estimate_tokens(_format_file(truncated))
            elif item.missing and item.role == FileRole.TARGET:
                packed.append(item)
                remaining -= cost
            else:
                omitted.append(item.path)

        context = context.model_copy(update={"files": packed, "omitted_paths": omitted})
        context = _fit_to_budget(context, budget)
        return context.model_copy(
            update={"token_count": estimate_tokens(context.render())}
        )

    async def _collect_files(
        self, task: Task, contracts: Mapping[str, ModuleContract]
    ) -> list[PackedFile]:
        planned: list[tuple[str, FileRole]] = []
        for path in task.target_files:
            planned.append((path, FileRole.TARGET))
        for path in task.test_files:
            planned.append((path, FileRole.TEST))
        for path in task.required_context:
            planned.append((path, FileRole.REQUIRED_CONTEXT))

        selected: dict[str, FileRole] = {}
        ordered: list[str] = []
        for path, role in planned:
            normalized = _normalize_path(path)
            if normalized is None:
                continue
            previous = selected.get(normalized)
            if previous is None:
                selected[normalized] = role
                ordered.append(normalized)
            elif _ROLE_PRIORITY[role] < _ROLE_PRIORITY[previous]:
                selected[normalized] = role

        packed: list[PackedFile] = []
        for path in ordered:
            packed.append(await self._read_packed(path, selected[path], contracts))

        import_paths: list[str] = []
        seen_imports: set[str] = set(selected)
        for item in packed:
            if item.role != FileRole.TARGET or item.missing or not item.path.endswith(".py"):
                continue
            for candidate in candidate_import_paths(item.content, item.path):
                if candidate in seen_imports:
                    continue
                seen_imports.add(candidate)
                import_paths.append(candidate)

        for path in import_paths:
            imported = await self._read_packed(path, FileRole.IMPORT, contracts)
            if imported.missing:
                continue
            packed.append(imported)

        packed.sort(key=lambda item: (_ROLE_PRIORITY[item.role], item.path))
        return packed

    async def _read_packed(
        self,
        path: str,
        role: FileRole,
        contracts: Mapping[str, ModuleContract] | None = None,
    ) -> PackedFile:
        try:
            content = await self._filesystem.read_file(path)
        except (WorkspaceError, UnicodeDecodeError, OSError):
            return PackedFile(path=path, content="", role=role, missing=True)
        if (role is FileRole.IMPORT and self.prefer_import_contracts
                and contracts is not None and path in contracts
                and contract_is_current(contracts[path], content)):
            return PackedFile(path=path, content=contracts[path].rendered,
                              role=role, from_contract=True, source_sha256=sha256(content.encode()).hexdigest())
        return PackedFile(path=path, content=content, role=role, source_sha256=sha256(content.encode()).hexdigest())

    async def _error_from_attempts(
        self, attempts: Sequence[TaskAttempt]
    ) -> ReducedLog | None:
        failed = [item for item in attempts if item.result in _FAILED_ATTEMPTS]
        if not failed:
            return None
        latest = max(failed, key=lambda item: item.attempt_number)
        if not latest.test_log_path:
            return None
        text = await self._read_log(latest.test_log_path)
        if text is None:
            return None
        return self._log_reducer.reduce_log_text(text).model_copy(update={"source_path": latest.test_log_path})

    async def _read_log(self, path: str) -> str | None:
        try:
            return await self._filesystem.read_file(path)
        except WorkspaceError:
            return None

    def _shrink_core(self, context: WorkerContext, budget: int) -> WorkerContext:
        architecture = context.architecture_summary
        error = context.reduced_error
        if architecture:
            overflow = estimate_tokens(context.render()) - budget
            keep = max(0, estimate_tokens(architecture) - overflow)
            trimmed, _truncated = truncate_to_tokens(architecture, keep)
            architecture = trimmed or None
            context = context.model_copy(update={"architecture_summary": architecture})
        if estimate_tokens(context.render()) > budget and error is not None:
            context = context.model_copy(update={"reduced_error": error.model_copy(update={"stdout_head": "", "stdout_tail": "", "stderr_head": "", "stderr_tail": ""})})
        return context


def _fit_to_budget(context: WorkerContext, budget: int) -> WorkerContext:
    packed = list(context.files)
    omitted = list(context.omitted_paths)
    while packed and estimate_tokens(context.render()) > budget:
        last = packed[-1]
        overflow = estimate_tokens(context.render()) - budget
        if last.content and not last.missing:
            keep = max(0, estimate_tokens(last.content) - overflow - 8)
            content, truncated = truncate_to_tokens(last.content, keep)
            packed[-1] = last.model_copy(
                update={"content": content, "truncated": truncated or last.truncated}
            )
            context = context.model_copy(update={"files": list(packed)})
            if estimate_tokens(context.render()) <= budget:
                break
            if content:
                continue
        packed.pop()
        omitted.append(last.path)
        context = context.model_copy(
            update={"files": list(packed), "omitted_paths": list(omitted)}
        )
    required = estimate_tokens(context.render())
    if required > budget:
        raise ContextBudgetError(f"Required task contract and evidence need {required} estimated tokens; budget is {budget}. Increase the budget.")
    return context


def _instruction(task: Task) -> str:
    return f"{task.name}\n\n{task.description}"


def _normalize_path(path: str) -> str | None:
    cleaned = path.strip().replace("\\", "/")
    if not cleaned:
        return None
    candidate = Path(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate.as_posix()


def _format_file(item: PackedFile) -> str:
    flags: list[str] = []
    if item.missing:
        flags.append("missing")
    if item.truncated:
        flags.append("truncated")
    if item.from_contract:
        flags.append("contract")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    if item.source_sha256:
        suffix += " [sha256=" + item.source_sha256 + "]"
    return f"## File {item.path} ({item.role.value}){suffix}\n```\n{item.content}\n```"


def _truncate_file(item: PackedFile, slot_tokens: int) -> PackedFile:
    overhead = estimate_tokens(_format_file(item.model_copy(update={"content": ""})))
    body_budget = max(1, slot_tokens - overhead)
    content, truncated = truncate_to_tokens(item.content, body_budget)
    return item.model_copy(update={"content": content, "truncated": truncated or item.truncated})
