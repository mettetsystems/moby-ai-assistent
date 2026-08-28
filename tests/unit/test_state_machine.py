from datetime import UTC, datetime

import pytest

from mobyai.engine.state_machine import InvalidTransitionError, ProjectStateMachine
from mobyai.models.project import ProjectState
from tests.helpers import FROZEN_NOW, make_project

LATER = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)


def apply(project, target: ProjectState):
    return ProjectStateMachine.transition(project, target, now=LATER)


def test_happy_path_to_validating() -> None:
    project = make_project()
    for target in (
        ProjectState.PLANNING,
        ProjectState.PLAN_READY,
        ProjectState.READY,
        ProjectState.DISPATCHING,
        ProjectState.WORKING,
        ProjectState.VALIDATING,
    ):
        project = apply(project, target)
        assert project.state == target
        assert project.updated_at == LATER


def test_validation_pass_returns_to_ready() -> None:
    project = make_project(state=ProjectState.VALIDATING)
    project = apply(project, ProjectState.READY)
    assert project.state == ProjectState.READY


def test_validation_fail_retries_then_redispatches() -> None:
    project = make_project(state=ProjectState.VALIDATING)
    project = apply(project, ProjectState.RETRYING)
    project = apply(project, ProjectState.DISPATCHING)
    assert project.state == ProjectState.DISPATCHING


def test_validation_fail_escalates_then_replans() -> None:
    project = make_project(state=ProjectState.VALIDATING)
    project = apply(project, ProjectState.ESCALATING)
    project = apply(project, ProjectState.REPLANNING)
    project = apply(project, ProjectState.READY)
    assert project.state == ProjectState.READY


def test_ready_to_completed() -> None:
    project = make_project(state=ProjectState.READY)
    project = apply(project, ProjectState.COMPLETED)
    assert project.state == ProjectState.COMPLETED


def test_planning_failure_is_terminal() -> None:
    project = make_project(state=ProjectState.PLANNING)
    project = apply(project, ProjectState.FAILED)
    assert project.state == ProjectState.FAILED
    with pytest.raises(InvalidTransitionError) as exc:
        apply(project, ProjectState.PLANNING)
    assert exc.value.source == ProjectState.FAILED
    assert exc.value.target == ProjectState.PLANNING


def test_invalid_transition_is_rejected() -> None:
    project = make_project(state=ProjectState.NEW)
    with pytest.raises(InvalidTransitionError):
        apply(project, ProjectState.WORKING)


def test_pause_and_resume_restores_paused_from() -> None:
    project = make_project(state=ProjectState.WORKING)
    paused = ProjectStateMachine.pause(project, now=LATER)
    assert paused.state == ProjectState.PAUSED
    assert paused.paused_from == ProjectState.WORKING
    resumed = ProjectStateMachine.resume(paused, now=LATER)
    assert resumed.state == ProjectState.WORKING
    assert resumed.paused_from is None


def test_cannot_pause_completed_or_failed() -> None:
    for state in (ProjectState.COMPLETED, ProjectState.FAILED):
        project = make_project(state=state)
        with pytest.raises(InvalidTransitionError):
            ProjectStateMachine.pause(project)


def test_cannot_resume_when_not_paused() -> None:
    project = make_project(state=ProjectState.READY)
    with pytest.raises(InvalidTransitionError):
        ProjectStateMachine.resume(project)


def test_resume_requires_paused_from() -> None:
    project = make_project(state=ProjectState.PAUSED, paused_from=None)
    with pytest.raises(InvalidTransitionError):
        ProjectStateMachine.resume(project)


def test_resume_rejects_wrong_target() -> None:
    project = make_project(state=ProjectState.PAUSED, paused_from=ProjectState.WORKING)
    with pytest.raises(InvalidTransitionError):
        ProjectStateMachine.transition(project, ProjectState.READY, now=LATER)


def test_next_after_validation_failure_is_deterministic() -> None:
    assert (
        ProjectStateMachine.next_after_validation_failure(1, 2) == ProjectState.RETRYING
    )
    assert (
        ProjectStateMachine.next_after_validation_failure(2, 2)
        == ProjectState.ESCALATING
    )


def test_created_at_is_preserved_on_transition() -> None:
    project = make_project()
    updated = apply(project, ProjectState.PLANNING)
    assert updated.created_at == FROZEN_NOW
    assert updated.updated_at == LATER
