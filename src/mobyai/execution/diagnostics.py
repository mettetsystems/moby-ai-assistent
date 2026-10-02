"""Classify observed failures without pretending sampling can repair the environment."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class FailureDiagnosis:
    category: str
    instruction: str
    sampling_eligible: bool = True
    locally_repairable: bool = False


def diagnose(text: str) -> FailureDiagnosis:
    lower = text.lower()
    if ('invalid command syntax:' in lower or
            re.search(r'executable not found:\s*(?:source|cd|export|unset|\.)(?=[.\s]|$)', lower)):
        return FailureDiagnosis(
            "command_syntax",
            "Repair the saved validation command through the Planner while preserving "
            "its acceptance criteria. The runner uses argv, not an implicit shell: "
            "source/cd and shell operators are not executable commands. Use the project "
            "venv interpreter directly for installation and validation. Inspect the "
            "existing environment; do not recreate it, install a shell builtin, or "
            "change sampling to repair command syntax.",
            False, True,
        )
    if 'sandbox unavailable:' in lower or 'execution policy blocked command:' in lower:
        return FailureDiagnosis("execution_policy", "The execution environment or policy blocked validation. Request operator review; do not bypass isolation, use host sudo, or change sampling.", False)

    if any(x in lower for x in ('context budget exhausted','context length','context window')):
        return FailureDiagnosis('context_capacity', 'Required context exceeds capacity. Preserve progress and reduce or compact context before resuming.', False)
    if 'timeout approval required' in lower:
        return FailureDiagnosis('timeout_approval', 'The operation reached its approved time limit. Pause for operator review.', False)
    if any(x in lower for x in ('command timed out', 'harness tests timed out', 'timed_out: true')):
        return FailureDiagnosis('timeout', 'Inspect progress, hangs and interactive prompts. The harness increases the same operation timeout in bounded steps. Split the work if needed and preserve validation. Do not change sampling or disable tests to hide a timeout.', False, True)
    if 'missing script:' in lower:
        return FailureDiagnosis("npm_missing_script", "Inspect package.json scripts in the command's working directory and the actual test files. Add the missing script targeting real tests, or correct the planned command to an existing equivalent. Do not use --if-present, a no-op test, or merely rename the task. Verify the exact harness command before claiming completion.")
    if any(x in lower for x in ('eresolve', 'unable to resolve dependency tree')):
        return FailureDiagnosis("npm_dependency_conflict", "Use inspect_node_project to inspect package.json, lockfiles, installed versions and peer requirements. Verify Node/npm versions with the existing command tool. Choose a compatible declared version set before retrying installation. Do not repeat npm ci/install against unchanged manifests, delete the lockfile, or hide conflicts with --force/--legacy-peer-deps. Preserve test scope; do not change sampling to repair dependency constraints.", False, True)
    # Match complete codes: FileNotFoundError contains the substring ENOTFOUND.
    if re.search(r'\b(?:eai_again|enotfound|econnrefused|econnreset|etimedout|connecterror|readtimeout)\b', lower):
        return FailureDiagnosis("network", "Check registry/endpoint reachability and proxy configuration. A network failure is not evidence of a code defect. Preserve the error and pause for environment repair if connectivity cannot be restored.", False)
    if any(x in lower for x in ('eacces', 'eperm', 'e401', 'e403', 'permission denied', 'unauthorized')):
        return FailureDiagnosis("access", "Check the failing path or registry credential configuration. Do not use sudo, alter credentials, or relax permissions automatically; request operator intervention.", False)
    if re.search(r'\bfilenotfounderror\b', lower):
        return FailureDiagnosis(
            "missing_file",
            "Inspect the failing filesystem operation, path, working directory, and fixture setup. "
            "If a test writes a temporary file before creating its parent directory, create that "
            "directory in the fixture before writing, preserving the missing-file cases and assertions. "
            "For reads, verify the required input and its producer; do not fabricate data or add "
            "production filesystem side effects merely to satisfy a test. Report unavailable external "
            "inputs explicitly. Rerun the same validation after a justified minimal repair. "
            "Do not change sampling or timeouts to fix a missing path.",
            False, True,
        )
    node_tool = r"(?:jest|vitest|mocha|eslint|tsc|webpack|vite)"
    if re.search(r"executable not found:\s*" + node_tool + r"(?=[.\s]|$)|(?:^|[\s/])" + node_tool + r":\s*(?:command )?not found", lower):
        return FailureDiagnosis("npm_missing_executable", "Inspect package.json, package-manager/lockfile and the failing working directory. Install declared development dependencies with the matching package manager (npm ci --include=dev with a valid lockfile, otherwise npm install --include=dev). Use the existing equivalent npm test script or installed project-local executable; preserve test scope and assertions. Do not install globally or use npx to download an undeclared tool. Rerun the exact validation and report actual results.", False, True)
    if any(x in lower for x in ('command not found', 'executable not found', 'ebadengine')):
        return FailureDiagnosis("toolchain", "Verify the required executable and supported runtime version. Report the missing toolchain instead of repeatedly rewriting application code.", False)
    if any(x in lower for x in ('out of memory', 'context length', 'context window')):
        return FailureDiagnosis("capacity", "Check model context/VRAM and request size. Changing sampling does not resolve resource exhaustion.", False)
    return FailureDiagnosis("implementation", "Inspect the exact failing command and test assertion, form a new repair hypothesis, make a minimal change, and rerun the same validation.")
