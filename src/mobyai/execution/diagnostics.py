"""Classify observed failures without pretending sampling can repair the environment."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureDiagnosis:
    category: str
    instruction: str
    sampling_eligible: bool = True


def diagnose(text: str) -> FailureDiagnosis:
    lower = text.lower()
    if any(x in lower for x in ('context budget exhausted','context length','context window')):
        return FailureDiagnosis('context_capacity', 'Required context exceeds capacity. Preserve progress and reduce or compact context before resuming.', False)
    if 'missing script:' in lower:
        return FailureDiagnosis("npm_missing_script", "Inspect package.json scripts in the command's working directory and the actual test files. Add the missing script targeting real tests, or correct the planned command to an existing equivalent. Do not use --if-present, a no-op test, or merely rename the task. Verify the exact harness command before claiming completion.")
    if any(x in lower for x in ('eresolve', 'unable to resolve dependency tree')):
        return FailureDiagnosis("npm_dependency_conflict", "Inspect package.json, the lockfile, Node/npm versions and peer dependency ranges. Choose compatible versions; do not hide conflicts with --force or --legacy-peer-deps.")
    if any(x in lower for x in ('eai_again', 'enotfound', 'econnrefused', 'econnreset', 'etimedout', 'connecterror', 'readtimeout')):
        return FailureDiagnosis("network", "Check registry/endpoint reachability and proxy configuration. A network failure is not evidence of a code defect. Preserve the error and pause for environment repair if connectivity cannot be restored.", False)
    if any(x in lower for x in ('eacces', 'eperm', 'e401', 'e403', 'permission denied', 'unauthorized')):
        return FailureDiagnosis("access", "Check the failing path or registry credential configuration. Do not use sudo, alter credentials, or relax permissions automatically; request operator intervention.", False)
    if any(x in lower for x in ('command not found', 'executable not found', 'ebadengine')):
        return FailureDiagnosis("toolchain", "Verify the required executable and supported runtime version. Report the missing toolchain instead of repeatedly rewriting application code.", False)
    if any(x in lower for x in ('out of memory', 'context length', 'context window')):
        return FailureDiagnosis("capacity", "Check model context/VRAM and request size. Changing sampling does not resolve resource exhaustion.", False)
    return FailureDiagnosis("implementation", "Inspect the exact failing command and test assertion, form a new repair hypothesis, make a minimal change, and rerun the same validation.")
