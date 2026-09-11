"""Context assembly errors. Independent of inference and persistence backends."""


class ContextLookupError(LookupError):
    def __init__(self, project_id: str, task_id: str | None = None) -> None:
        self.project_id = project_id
        self.task_id = task_id
        if task_id is None:
            super().__init__(f"Project not found: {project_id}")
        else:
            super().__init__(f"Task not found: {project_id}/{task_id}")


class ContextBudgetError(ValueError):
    """Required task evidence cannot fit the requested estimated token budget."""
