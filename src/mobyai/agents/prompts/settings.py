"""Saved prompt overrides and attempt-boundary snapshots."""
from mobyai.models.config import AppConfig


def default_prompts() -> dict[str, str]:
    from mobyai.agents.prompts.planner import SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT
    from mobyai.agents.prompts.worker import SYSTEM_PROMPT as worker
    from mobyai.agents.prompts.orchestrator import SYSTEM_PROMPT as orchestrator
    from mobyai.final_pass.service import FINAL_FAIL_PROMPT, FINAL_REVIEW_PROMPT
    return dict(planner=SYSTEM_PROMPT, replan=REPLAN_SYSTEM_PROMPT,
                worker=worker, orchestrator=orchestrator,
                final_fail=FINAL_FAIL_PROMPT, final_review=FINAL_REVIEW_PROMPT)


def effective_prompts(config: AppConfig) -> dict[str, str]:
    defaults = default_prompts()
    return {key: getattr(config.system_prompts, key) or value for key, value in defaults.items()}


class PromptSnapshot:
    def __init__(self, config: AppConfig, refresh=None):
        self.config = config
        self.refresh = refresh
        self.active = effective_prompts(config)

    def begin_attempt(self):
        if self.refresh is not None:
            self.refresh()
        self.active = effective_prompts(self.config)

    def get(self, key):
        return self.active[key]
