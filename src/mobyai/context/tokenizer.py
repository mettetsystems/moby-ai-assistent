"""Lightweight token estimates. Not a model-specific tokenizer."""

from __future__ import annotations

_CHARS_PER_TOKEN = 4
_TRUNCATION_MARKER = "\n...[truncated]...\n"


def estimate_tokens(text: str) -> int:
    """Approximate tokens as ceil(chars / 4). Empty text is zero."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """Keep a head/tail slice that fits ``max_tokens``. Returns (text, truncated)."""
    if max_tokens <= 0:
        return "", True
    if estimate_tokens(text) <= max_tokens:
        return text, False
    budget_chars = max(max_tokens * _CHARS_PER_TOKEN - len(_TRUNCATION_MARKER), 1)
    head = (budget_chars * 2) // 3
    tail = budget_chars - head
    if tail < 1:
        return text[:budget_chars] + _TRUNCATION_MARKER, True
    return text[:head] + _TRUNCATION_MARKER + text[-tail:], True
