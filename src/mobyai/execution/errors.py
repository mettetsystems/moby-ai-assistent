"""Execution-layer errors. The harness rejects unsafe paths and commands."""


class WorkspaceError(Exception):
    """Unsafe or invalid workspace operation."""


class PathEscapeError(WorkspaceError):
    """A path resolved outside the allowed project root."""


class DestructiveCommandError(WorkspaceError):
    """A command that would delete or destroy data without an explicit gate."""


class PatchError(WorkspaceError):
    """Unified diff could not be applied safely."""
