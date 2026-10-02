# Prepared Whirlpool sandbox

Verified local image:
`sha256:73e483e00cdd034f4f1e659e21d36bb64f96736ca0c2b3c0ae584970304034d9`

Built and cached by the local MCP automatic preparer. The Containerfile and
requirements.txt here are a readable snapshot; `src/mobyai/sandbox/builder.py`
owns the automatic recipe. requirements.lock records the installed Python versions.

Python 3.14.7, Node 22.23.1, npm 10.9.8, pip dependency consistency and headless Qt
passed checks inside the restricted offline backend. Cached image reuse also passed.

Workload validation on a temporary source copy found:
- Batch-download tests: 21 passed before a quota assertion failed (test expects
  10 TB; current implementation reports a 10 GB quota).
- A manifest-validation test allocates an 11 GB bytes object; the process exited
  137 under the 4 GB sandbox memory limit. Full workload validation did not pass.

The original workload was not edited, resumed, or switched to sandbox mode. Its
.venv symlinks were rejected during the original verification; the sandbox now
supports ordinary project symlinks. Existing environments still require compatible
container runtimes and libraries. Host container integration
remains outside the offline worker sandbox. See ../../../docs/sandbox.md for policy.
