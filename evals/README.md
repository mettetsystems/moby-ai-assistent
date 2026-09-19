# Agent improvement evaluations

The default benchmark runs only scripted responses, real local file tools, and
real Python unit tests in fresh temporary workspaces. It never opens the normal
project database, starts a model server, or runs work in an existing project.

```bash
.venv/bin/python -m mobyai.evaluation --trials 3 --output /tmp/moby-baseline.json
```

Cases cover a Python off-by-one repair, malformed worker JSON followed by repair,
and a failed file tool followed by recovery. Acceptance test contents must remain
unchanged. Reports contain prompt hashes, request counts, outcomes, elapsed time,
tool failures, and token usage where available. This deterministic baseline checks
harness behavior; it does not demonstrate improved model performance.

For real-model comparisons, explicitly opt in to contact an already running worker:

```bash
.venv/bin/python -m mobyai.evaluation --live --config config.yaml \
  --trials 5 --output /tmp/moby-live-baseline.json
```

Live mode runs the repair fixture, without scripted fault injection. Run it while
normal workloads are idle, then repeat using a separate candidate configuration.
Change one feature at a time. Compare success, preserved acceptance tests, request
count, elapsed time, and tokens. Run several trials: a single success is not proof
of improvement. This is an initial worker benchmark, not a comprehensive measure
of planner quality, full-project completion, or performance on unfamiliar repos.

Regression fixtures for replacement dependencies, timeout approval, caching,
pause/resume, streaming cancellation, context integrity, and schema handling live
in `tests/unit/test_replan.py`, `test_timeout_recovery.py`, `test_cached_recovery.py`,
`test_cli_ssh.py`, and `test_agent_improvements.py`.

Temporary directories isolate test files, not arbitrary operating-system access.
Live model tools have the same execution permissions as the normal harness.
