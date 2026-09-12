"""Task-local authority for outbound research; local lookup is always available."""
from contextlib import contextmanager
from contextvars import ContextVar
internet_allowed = ContextVar('research_internet_allowed',default=False)
@contextmanager
def research_access(allowed):
    token=internet_allowed.set(allowed)
    try: yield
    finally: internet_allowed.reset(token)
