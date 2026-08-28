"""Application-owned project state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from mobyai.models.project import Project, ProjectState


class InvalidTransitionError(Exception):
    def __init__(
        self,
        source: ProjectState,
        target: ProjectState,
        message: str | None = None,
    ) -> None:
        self.source = source
        self.target = target
        super().__init__(
            message or f"Invalid project state transition: {source} -> {target}"
        )


class ProjectStateMachine:
    """Explicit transition table. LLMs cannot change project state."""

    TERMINAL_STATES: ClassVar[frozenset[ProjectState]] = frozenset(
        {ProjectState.FAILED, ProjectState.COMPLETED}
    )

    ALLOWED_TRANSITIONS: ClassVar[frozenset[tuple[ProjectState, ProjectState]]] = (
        frozenset(
            {
                (ProjectState.NEW, ProjectState.PLANNING),
                (ProjectState.PLANNING, ProjectState.PLAN_READY),
                (ProjectState.PLANNING, ProjectState.FAILED),
                (ProjectState.PLAN_READY, ProjectState.READY),
                (ProjectState.READY, ProjectState.DISPATCHING),
                (ProjectState.READY, ProjectState.COMPLETED),
                (ProjectState.READY, ProjectState.FAILED),
                (ProjectState.DISPATCHING, ProjectState.WORKING),
                (ProjectState.WORKING, ProjectState.VALIDATING),
                (ProjectState.WORKING, ProjectState.FAILED),
                (ProjectState.VALIDATING, ProjectState.READY),
                (ProjectState.VALIDATING, ProjectState.RETRYING),
                (ProjectState.VALIDATING, ProjectState.ESCALATING),
                (ProjectState.RETRYING, ProjectState.DISPATCHING),
                (ProjectState.ESCALATING, ProjectState.REPLANNING),
                (ProjectState.ESCALATING, ProjectState.FAILED),
                (ProjectState.REPLANNING, ProjectState.READY),
                (ProjectState.REPLANNING, ProjectState.FAILED),
            }
        )
    )

    @classmethod
    def can_pause(cls, state: ProjectState) -> bool:
        return state not in cls.TERMINAL_STATES and state != ProjectState.PAUSED

    @classmethod
    def can_transition(cls, source: ProjectState, target: ProjectState) -> bool:
        if target == ProjectState.PAUSED:
            return cls.can_pause(source)
        if source == ProjectState.PAUSED:
            return False
        return (source, target) in cls.ALLOWED_TRANSITIONS

    @classmethod
    def transition(
        cls,
        project: Project,
        target: ProjectState,
        *,
        now: datetime | None = None,
    ) -> Project:
        timestamp = now or datetime.now(UTC)
        if project.state == ProjectState.PAUSED:
            if project.paused_from is None or target != project.paused_from:
                raise InvalidTransitionError(
                    project.state,
                    target,
                    "Resume is only allowed back to paused_from",
                )
            return project.model_copy(
                update={
                    "state": target,
                    "paused_from": None,
                    "updated_at": timestamp,
                }
            )
        if target == ProjectState.PAUSED:
            return cls.pause(project, now=timestamp)
        if (project.state, target) not in cls.ALLOWED_TRANSITIONS:
            raise InvalidTransitionError(project.state, target)
        return project.model_copy(update={"state": target, "updated_at": timestamp})

    @classmethod
    def pause(cls, project: Project, *, now: datetime | None = None) -> Project:
        timestamp = now or datetime.now(UTC)
        if not cls.can_pause(project.state):
            raise InvalidTransitionError(project.state, ProjectState.PAUSED)
        return project.model_copy(
            update={
                "state": ProjectState.PAUSED,
                "paused_from": project.state,
                "updated_at": timestamp,
            }
        )

    @classmethod
    def resume(cls, project: Project, *, now: datetime | None = None) -> Project:
        timestamp = now or datetime.now(UTC)
        if project.state != ProjectState.PAUSED:
            raise InvalidTransitionError(project.state, project.state)
        if project.paused_from is None:
            raise InvalidTransitionError(
                project.state,
                ProjectState.PAUSED,
                "Cannot resume a paused project without paused_from",
            )
        return cls.transition(project, project.paused_from, now=timestamp)

    @classmethod
    def next_after_validation_failure(
        cls,
        attempt_count: int,
        max_task_attempts: int,
    ) -> ProjectState:
        """Deterministic retry vs escalate. The model does not choose this."""
        if attempt_count < max_task_attempts:
            return ProjectState.RETRYING
        return ProjectState.ESCALATING
