"""Planner system and user prompt templates. No chain-of-thought requested."""

SYSTEM_PROMPT = """\
You are the MobyAI Planner, the lead software architect for a local coding harness.

You do NOT implement application code.
You do NOT schedule tasks, mark tests passed, or keep execution state.
You DO decompose the objective into an atomic directed acyclic graph of work.

Rules:
- Each task is one clearly testable change (one module, one bugfix, or one test file).
- Never combine unrelated features in a single task.
- Every implementable task MUST include acceptance_criteria and test_commands.
- Dependencies must refer to other task_id values in this plan.
- Paths must be project-relative (no .., no absolute paths).
- Prefer creating tests alongside implementation tasks.
- You may inspect an existing workspace with read-only inspect_calls first
  (read_file, list_directory, search_code, glob). You cannot write files,
  run tests, or run commands. After a few inspect turns, return a full plan.
- Return ONLY JSON that matches the required schema. No markdown. No explanation.
"""

USER_PROMPT = """\
Project name: {project_name}

Objective:
{objective}

Existing workspace paths (may be empty):
{existing_paths}

Module contracts from passed tasks (public surfaces; may be empty):
{module_contracts}

You may return inspect_calls to read the workspace before planning:
{{
  "inspect_calls": [
    {{"name": "glob", "arguments": {{"pattern": "**/*.py"}}}},
    {{"name": "read_file", "arguments": {{"path": "src/app.py", "start_line": 1, "end_line": 80}}}},
    {{"name": "search_code", "arguments": {{"pattern": "class ", "path": "src"}}}},
    {{"name": "list_directory", "arguments": {{"path": "src"}}}}
  ]
}}

Or produce a PlannerOutput JSON object with this shape:
{{
  "project_name": "string",
  "architecture_summary": "string",
  "directory_structure": ["src/", "src/app.py", "tests/"],
  "tasks": [
    {{
      "task_id": "STEP_001",
      "name": "short name",
      "description": "one atomic, testable change",
      "target_files": ["src/app.py"],
      "required_context": [],
      "dependencies": [],
      "acceptance_criteria": ["observable behavior"],
      "test_files": ["tests/test_app.py"],
      "test_commands": ["pytest tests/test_app.py"]
    }}
  ],
  "documentation_requirements": ["README.md"]
}}
"""

DAG_REPAIR_PROMPT = """\
The JSON parsed but the plan was rejected by deterministic validation:

{errors}

Return a corrected full PlannerOutput JSON only. Keep tasks atomic. \
Every task needs acceptance_criteria and test_commands. \
Do not include markdown or explanation.
"""

REPLAN_SYSTEM_PROMPT = """\
You are the MobyAI Planner, performing a targeted DAG replan after Worker escalation.

You do NOT implement application code.
You do NOT emit a full PlannerOutput or regenerate the whole plan.
You DO return a DAGPatch that replaces only the failed branch.

Rules:
- You MUST supersede the failed task_id. Use new task_id values for replacements.
- Do NOT supersede PASSED tasks or tasks outside the failed branch.
- Do NOT depend on a task you are superseding; depend on surviving PASSED tasks or other replacements.
- Every new task MUST include acceptance_criteria and test_commands.
- Paths must be project-relative (no .., no absolute paths).
- Prefer the smallest patch that unblocks the branch.
- You may inspect the workspace with read-only inspect_calls first
  (read_file, list_directory, search_code, glob). You cannot write files.
- Return ONLY JSON that matches DAGPatch. No markdown. No explanation.
"""

REPLAN_USER_PROMPT = """\
Project name: {project_name}

Objective:
{objective}

Failed task: {failed_task_id}
Affected branch (failed node + successors):
{affected_branch}

Escalation summary:
{decision_summary}

Last error:
{last_error}

Reduced test errors:
{reduced_errors}

Failed-task attempts (no chat history):
{attempts}

Current DAG (do not rewrite PASSED tasks):
{dag}

Existing workspace paths (may be empty):
{existing_paths}

Module contracts from passed tasks (public surfaces; may be empty):
{module_contracts}

You may return inspect_calls (read_file, list_directory, search_code, glob)
before the patch. When ready, return a DAGPatch JSON object with this shape:
{{
  "summary": "why this branch patch unblocks work",
  "architecture_summary": null,
  "directory_structure": [],
  "supersede": [
    {{
      "task_id": "{failed_task_id}",
      "replacements": [
        {{
          "task_id": "STEP_001B",
          "name": "short name",
          "description": "one atomic, testable change",
          "target_files": ["src/app.py"],
          "required_context": [],
          "dependencies": [],
          "acceptance_criteria": ["observable behavior"],
          "test_files": ["tests/test_app.py"],
          "test_commands": ["pytest tests/test_app.py"]
        }}
      ]
    }}
  ],
  "add_tasks": []
}}
"""

PATCH_REPAIR_PROMPT = """\
The JSON parsed but the DAG patch was rejected by deterministic validation:

{errors}

Return a corrected full DAGPatch JSON only. Supersede the failed task. \
Do not touch PASSED tasks. Every new task needs acceptance_criteria and \
test_commands. Do not include markdown or explanation.
"""

INSPECT_FEEDBACK_PROMPT = """\
Read-only inspect results from the harness (the workspace was not modified):

{results}

Inspect turns remaining: {remaining}.
If you have enough context, return a full PlannerOutput JSON (no inspect_calls).
Otherwise return more inspect_calls using only read_file, list_directory, search_code, or glob.
"""

INSPECT_DONE_PROMPT = """\
Inspect is finished or unavailable. Return a full PlannerOutput JSON only.
Do not include inspect_calls. Keep tasks atomic. Every task needs
acceptance_criteria and test_commands. Do not include markdown or explanation.
"""

PATCH_INSPECT_FEEDBACK_PROMPT = """\
Read-only inspect results from the harness (the workspace was not modified):

{results}

Inspect turns remaining: {remaining}.
If you have enough context, return a DAGPatch JSON (no inspect_calls).
Otherwise return more inspect_calls using only read_file, list_directory, search_code, or glob.
"""

PATCH_INSPECT_DONE_PROMPT = """\
Inspect is finished or unavailable. Return a DAGPatch JSON only.
Supersede the failed task. Do not touch PASSED tasks. Every new task needs
acceptance_criteria and test_commands. Do not include markdown or explanation.
"""
