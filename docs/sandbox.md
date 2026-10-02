# Offline worker sandbox (opt-in)

The harness controls isolation; the orchestrator recommends recovery actions and
workers submit normal command tools. A fixed local stdio MCP server provisions an
ephemeral rootless Podman container for every worker command and harness test.
Sandbox errors never fall back to executing the command on the host.

## Setup

Install the optional `sandbox` extra in the MobyAI environment. Prepare a trusted
local image with your required runtimes and pin its full image ID or digest.
A manually pinned image is never pulled automatically. Alternatively, automatic
preparation can build the trusted profile described below.
Add to the MobyAI configuration (keep it outside the target project):

```yaml
sandbox:
  enabled: true
  image: "sha256:<full-64-character-local-image-ID>"
  network: dependencies_only
  memory_mb: 4096
  cpus: 2
  pids: 256
  artifacts: []
```

Restart MobyAI and resume the project. This setting applies to CLI/UI project runs;
standalone library callers must activate the sandbox policy explicitly. Existing
installations remain in host mode unless configured. Do not enable a placeholder ID.

## Automatic image preparation

Enable automatic preparation instead of specifying an image ID:

```yaml
sandbox:
  enabled: true
  auto_prepare: true
  profile: python-qt-node
  image: ""
  network: dependencies_only
```

At CLI/UI Run or Resume, before planning or executing project commands, the harness
calls the local MCP `sandbox_prepare` tool. It reuses a verified cached image or
builds the fixed Python/Qt/Node profile from an immutable Fedora base reference.
No project files, Containerfile, model-generated shell commands, or secrets are
included in the build context. The build stage downloads vendor OS packages and
binary Python wheels from public package repositories; it does not run project code.
The resulting execution containers remain offline. Network access during trusted
image preparation is broader than the exact-artifact downloader but is not exposed
to the worker. The profile is currently Linux x86_64 and uses Python 3.14/Node 22.

The first profile covers Whirlpool's base requirements, pytest-mock and pytest-qt.
It does not automatically infer/install arbitrary project dependencies, GPU training
stacks, private packages or nested Docker tooling. New profiles/dependencies require
an explicit harness recipe change. No model can substitute its own build instructions.

A read-only/offline smoke test checks Python dependencies, Node/npm and headless Qt
before the image is cached. The recipe hash keys the cache and each workload uses
the full resulting image ID. Mutable package ranges are recorded with their resolved
versions inside `/opt/sandbox/requirements.lock` and `/opt/sandbox/rpm-packages.txt`;
a fresh build is not guaranteed byte-for-byte identical to an older one.

Preparation emits `sandbox_preparing`, `sandbox_ready`, and failure events and prints
progress every ten seconds. Logs and verification live in
`~/.cache/mobyai-sandbox/images/<recipe-hash>/`. Concurrent builds share a lock.
Preparation starts at the existing 600-second timeout and follows its recovery ladder;
failures pause with a reason before any workload command runs. Pause cancels preparation
and kills its tracked build process group; abnormal host termination may leave Podman
build cache/intermediate storage requiring operator inspection. The operator's existing
timeout approval mechanism applies at the ceiling.

## Dependency-only downloads

Worker containers have no external network, including during installation scripts.
A trusted downloader fetches only operator-configured direct wheel/npm tarball URLs
from files.pythonhosted.org or registry.npmjs.org. HTTPS, exact SHA-256 verification,
no redirects, no inherited proxy configuration, 100 MiB per artifact, and at most
20 artifacts are enforced. Dependencies are mounted read-only at `/dependencies`.
Downloads are inside the operation deadline and never execute package code online.

Each artifact requires filename, url, and sha256. Include transitive dependencies.
The worker cannot add URLs or hashes. Python can install wheels using a project-local
virtual environment and `--no-index --find-links=/dependencies`. npm offline installs
require all packages to have been prepared; arbitrary npm/pip online resolution,
private registries, source distributions and OS package downloads are unsupported.
Artifacts are downloaded afresh per command; use a prepared image for larger stacks.

## Boundaries and limitations

- Rootless execution, no capabilities, no-new-privileges, read-only image root,
  isolated networking, CPU/memory/PID limits and bounded temporary storage.
- Only the project directory is writable; existing .git, .agent and minutes folders
  receive read-only mounts. Project edits are direct, not staged for human merge.
- No home-directory, GPU, host service or Docker socket mounts. Host credentials
  are not passed into the container. Secrets already inside the project are still
  readable: use a dedicated sanitized project. Never point at your home directory.
- Ordinary symlinks are supported, including relative file/directory links, chains,
  dangling links and loops. They are not dereferenced during the host preflight;
  commands resolve them inside the container. Broken/cyclic links remain broken
  until the project repairs them, but do not reject the whole workspace.
- The same workspace is mounted at `/workspace` and at its original absolute path
  so absolute project links and generated executable shebangs can resolve. Parent
  directories and unrelated host targets are never mounted. Protected history is
  read-only through both aliases; symlinks used as protected top-level history
  directories remain rejected. Hard links, sockets and device files remain denied.
- A link to `/usr/bin/python3` uses the image's Python, not the host binary. Existing
  virtual environments still need compatible runtimes and native libraries; symlink
  support does not make arbitrary host environments portable. Links to external
  host data cannot grant access to that data.
- Host file tools support links within their allowed workspace roots. Directory
  listing preserves link names without dereferencing them. Reads/writes outside
  the allowed roots remain blocked; dangling reads and loops return tool errors.
- The MobyAI installation cannot be the sandbox target. The host file-tool layer
  retains its existing path checks; this feature isolates subprocess execution.
- Each command starts fresh. Only workspace files persist; run service startup,
  checks and shutdown together in one bounded command. Host Docker/systemd operations
  and nested container builds are unsupported. They remain operator prerequisites.
- Cancel/timeout removes the named container. External process kills or machine
  crashes can leave containers: inspect `podman ps -a --filter name=mobyai-sandbox-`
  before removing only identified stale MobyAI containers.
- Containers share the host kernel. This is not a VM or a guarantee against kernel
  vulnerabilities. Writable project storage has no disk quota; use filesystem quotas
  for untrusted workloads. SELinux project labels may change because mounts use `:Z`.

A blocked host validation command is now recorded as exit 126 with an execution-policy
reason instead of throwing an uncaught exception. The recovery classifier requests
operator review rather than repeatedly prescribing sudo or sampling changes.
