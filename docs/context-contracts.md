# Worker context contracts and compression

The packer maps each active task into an explicit contract containing target files,
test files, required context paths, and dependency IDs. Acceptance criteria and
harness test commands remain exact. Direct dependencies contribute their stored
status, artifact paths, acceptance criteria, and test commands, not their previous
worker conversations or full source files. A missing dependency is a lookup error,
not an invented guarantee. Dependency criteria describe expectations; status is
reported separately and must not be interpreted as new validation evidence.

## Compression policy

Budgets use the existing character-based token estimate, not a model tokenizer.
The packer first reduces architecture text and generic log output. Failure command,
exit status, failed-test identifiers, assertions, traceback, and full-log path are
retained. Source bodies may be truncated or omitted, with omission paths exposed.
Exact target paths survive even when their source bodies do not fit.
If required contract/evidence cannot fit, ContextBudgetError reports the estimated
required size instead of silently exceeding the budget or deleting requirements.
This budget covers the rendered context, not system prompts, response schemas,
model output, or subsequent conversation turns.

## Retrieving omitted evidence

read_file accepts optional start_line (1-based) and max_lines (1..2000).
For example: {"path": "src/api.py", "start_line": 120, "max_lines": 40}.
Results include source_path, start_line, end_line, total_lines, and truncated.
The line range describes the selected source region. When output is token-clipped,
the returned text can have an omitted middle; request a smaller range to retrieve
it. Legacy path-only calls still return plain file text with the same output cap.
These controls bound model-visible output; the current implementation still reads
the source file into memory before selecting lines.

## Validation

Run tests/unit/test_context_packer.py and tests/unit/test_tools.py for dependency
mapping, exact constraints, failure provenance, tiny budgets, range validation,
and source retrieval. Full GPU/model evaluation remains a separate integration
exercise; these tests use deterministic local fixtures.
