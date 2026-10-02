"""Shared command and Python environment rules for planning and implementation."""

COMMAND_ENVIRONMENT_GUIDANCE = """\
Command execution and Python environments:
- run_command, run_test and saved test_commands execute argument lists without an
  implicit shell. Do not use source, cd, export, pipes, redirects, && or || at the
  top level. Use separate commands and run_command's cwd parameter when needed.
  Saved test_commands and run_test execute from the project root; use tool options
  or project-relative script paths for checks in subdirectories.
- Inspect the project manifests and existing environment before creating one.
  Use inspect_python_environment for metadata and direct interpreter commands for
  runtime checks. Reuse a working environment; a failed check does not establish
  that venv creation failed. Recreate only when evidence shows it is necessary.
- Choose a compatible interpreter when creating the environment, then use that
  environment's interpreter explicitly for installation and validation, for example
  .venv/bin/python -m pip install -r backend/requirements.txt and
  .venv/bin/python -m pytest tests. Honor the actual environment path and backend
  dependency policy. Do not install project dependencies into host Python by mistake.
- Creating a venv does not activate it. Activation and shell state do not persist
  between calls. Do not add a shell just to activate a venv; invoke its Python directly.
- Check each required package with .venv/bin/python -m pip show <package> as separate
  checks, or use importlib.metadata.version for every required distribution. Presence does
  not prove importability or application behavior; run imports and tests when the
  acceptance criteria require them. Keep all checks on the selected interpreter.
- Distinguish invalid command syntax, missing dependencies, and application failures.
  'Executable not found: source' means an invalid command, not a missing Python
  toolchain. Preserve installed packages and repair the command without weakening
  the acceptance criteria. Do not reinstall dependencies or increase sampling/timeouts
  to repair shell syntax. The Planner must correct invalid saved test_commands;
  the Worker must report that contract error with evidence instead of repeating setup.
"""
