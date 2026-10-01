# Test Suite

## Structure

### Root-level tests
Broad framework tests: metaclass behavior, event system, sandbox, error formatting, actor, decorator/strategy wiring, method definition, media handling, storage compatibility, skill registry, visibility, and module imports.

### `agentdoc/` - Agent documentation and visibility
`doc(self)` rendering, visibility rules, referenced types, annotated parameters and fields, truncating `pformat`/`pprint`, metadata, protocols, and fixture modules under `agentdoc/fixtures/`.

### `agents/` - Agent-level behavior
Agent imports, render configuration, method summarization, and summarization agents.

### `acceptance/` - Installed packages and offline workflows
Real console entrypoints, viewer startup/assets/shutdown, and ACP coding with
real file edits, shell cancellation/recovery, and a local stdio MCP server.
Also runs `nooa doctor --json --smoke` and verifies it leaves user configuration
directories untouched. Memory checks packaged skill discovery, offline NumPy
retrieval, cross-process SQLite persistence/update, and database release on detach.
Bench and RLM agents edit and verify a Unicode file, export results, trajectories,
behavior metrics, UTF-8 logs and traces, then release their shell and model client.
The Harbor runner's help must not import the agent/model runtime.
Model responses are scripted and Harbor's fixed container output paths are
redirected to temporary directories; execution, storage and exporters are real.
No model credentials are needed. This does not validate Harbor container
orchestration, optional memory backends, or a public native Windows sandbox.
The clean-install runner also copies the internal Job Object, spawn and LPAC
suites described below.

### `atif/` - Agent Trace Interchange Format
ATIF schema validation, exporter state machines, event typing, nested traces, standalone entrypoints, and end-to-end CodeAct traces.

### `capability/` - Capability routing
Router repeated runs, class method replacement, scorer behavior, structured output scoring, and support agents/data under `capability/agents/` and `capability/data/`.

### `config/` - Configuration resolution
Execution, strategy, summarizer, blocked, and resolved configuration behavior.

### `context_blocks/` - Context block rendering
Context block models, renderers, scoped blocks, cached rendering, event formatting, stats, and truncation.

### `coordinator/` - Code validation
AST validation, forbidden features, and retry logic.

### `core_runtime/` - Task lifecycle
Task queuing and serialization, code caching (ONCE vs AGENT lifetime), execution, LLM client reuse, and implemented plan behavior.

### `edge_cases/` - Boundary conditions
Generation lock contention, nested generation, child agent edge cases, sandbox edge cases, signal edge cases, missing await detection, builtin shadowing, task wrappers, and agent initialization.

### `external/` - Public API surface
Decorator semantics, stub layer, agent method requirements, provider configuration, and end-to-end notebook scenarios (gold standard for user-facing behavior).

### `helpers/` - Shared support modules
Reusable helper modules for cross-module inheritance, OpenTelemetry assertions, and signature utilities. These are support files, not standalone test suites.

### `integration/` - Cross-cutting tests
Nested generation, concurrent traces, hook failure traces, nested agent history, CodeAct structured output, archival behavior, journal fanout, import round-trips, nested bug fixtures under `integration/nested_bug/`, and fixture data under `integration/fixtures/`.

### `onboarding/` - Model onboarding
Tests for evaluating model performance on framework capabilities (code generation, REPL behavior, validation retry, working context), with sample inputs under `onboarding/test_data/`. Used for model onboarding optimization. See `tests/onboarding/README.md`.

### `performance/` - Performance benchmarks
Client creation overhead.

### `provider_compat/` - Provider compatibility
Provider compatibility checks across model backends and API shapes.

### `runtime/` - Runtime internals
Context building, event manager, code execution, hooks, pure Python executor/REPL, structured output executor, async deadlock prevention, span relationships, truncation behavior, and runtime evaluation.

### `runtime/sandbox/` - Sandbox executor
Sandbox executor behavior, broker deadlines, CodeAct sandbox integration, guard enforcement, sandbox config, nested async proxies, read-only module state, teardown behavior, and implicit return handling.

### `storage/` - Storage backends
In-memory storage, snapshot markers, serialization, snapshot variables, and snapshot persistence.

### `strategies/` - Generation strategies
`CodeActStrategy`, `PurePythonStrategy`, `ReflexionStrategy`, `TemplateStrategy`, argument validation, return type validation, helper method manager, and `RuntimeServices`.

### `test_mcp/` - MCP client and tool integration
MCP client, tool calls, OAuth discovery, browser detection, exception descriptions, and timeout refresh behavior.

### `tools/` - Built-in tools
Bash tool and file tool tests.

### `trace_explorer/` - Trace explorer storage and queries
Trace explorer cache, client, database queries, filtered loading, OTLP span conversion, fast path loading, and OpenInference backcompat.

### `tracing/` - Tracing pipeline
Span context isolation, exporters, journal invariants, metadata, OpenInference conformance, OTLP probing, secret scrubbing, subprocess hooks, viewer attributes, and wire-format stripping.

### `unifiedllm/` - Unified LLM client
Provider detection, cache control, context windows, retries, HTTP config/logging, JSON parsing, schema sanitization, Responses client behavior, strict schema fallback, and tool schema cleanup.

### `unit/` - Isolated unit tests
Focused coverage for context vars, actor behavior, prompts, pragma replacements, skill loading, strategy behavior, utilities, and remaining coverage gaps.

### `utils/` - Utility modules
`doc`, `logger`, `message`, and `task` utility tests.

### `viewer/` - Trace viewer API and stores
Viewer main routes, journal and OTLP stores, OTLP ingest, stress coverage, image resolution, message resolution, and block export.

---

## Running Tests

### Prerequisites

- `ripgrep` (`rg`) and `grep` must be on `PATH`. The tests in
  `tests/tools/test_shell_tools_modern.py` skipif-away without them, and a
  clean skip is indistinguishable from a pass in the summary line. Install
  with `apt install ripgrep` on Debian/Ubuntu or `brew install ripgrep` on
  macOS.
- On native Windows, install Git for Windows and ripgrep
  (`winget install BurntSushi.ripgrep.MSVC`). Run pytest from PowerShell with
  Windows Python. The shell-test setup finds Git Bash and adds its tool
  directories to the test process's `PATH`, so `grep` need not be on the
  PowerShell `PATH`. Set `NOOA_BASH` to use another MSYS2 bash.
- Bash sessions, search/file tools, and process-tree cleanup run on Windows.
  Tests that require Unix signals or the forked sandbox worker skip with a
  reason; run with `-rs` to list them.

```bash
uv run pytest                          # all tests
uv run pytest tests/runtime/ -v        # single directory
uv run pytest tests/runtime/sandbox/   # nested runtime area
uv run pytest tests/test_metaclass.py  # single file
uv run pytest -k "test_codeact" -v     # by name pattern
```

When Windows and WSL share a checkout, use separate bytecode-cache directories.
Pytest's assertion-rewritten `.pyc` files can retain the other platform's source
paths even when the source timestamps match. On Windows this can produce
`/mnt/...` traceback paths and break `inspect`-based method/ellipsis detection.
`-B` prevents cache writes but does not prevent reading existing bytecode.
For example, on Windows Python 3.12:

```powershell
uv run --no-sync python -X pycache_prefix=logs/pycache-win-py312 -m pytest -q
```

Use a different prefix for WSL (for example `logs/pycache-linux-py312`) and each
Python version. This avoids consuming shared caches without deleting them or
changing the project's environment.

Windows CI also explicitly runs these package and embedded suites:

```powershell
uv run pytest src/nooa/tools/tests src/nooa/runtime/tests/test_producers.py
uv run pytest packages/nooa-memory/tests/memory packages/nooa-bench/tests
```

Shell lifecycle checks run on Windows Python 3.12 and 3.13. They cover
cancellation, abandoned streams, repeated loop replacement, and process handle
counts. Resource warnings are failures, not suppressed:

```powershell
uv run pytest tests/tools src/nooa/tools/tests -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
```

The same CI step checks the cross-thread channel helper's event-loop cleanup:

```powershell
uv run --no-sync pytest src/nooa/runtime/tests/test_channels_cross_thread.py -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Windows CI also builds and installs all five wheels (`nooa`, `nooa-cli`,
`nooa-acp`, `nooa-memory`, `nooa-bench`) into a fresh temporary environment on both
Python versions, without replacing `.venv`:

```powershell
uv run --no-sync python scripts/smoke_install.py --python 3.12
uv run --no-sync python scripts/smoke_install.py --python 3.13
```

For a targeted installed-wheel check, repeat `--test-file` with native suite
filenames. Wheel provenance remains mandatory; this is not full acceptance:

```powershell
uv run --no-sync python scripts/smoke_install.py --python 3.13 --test-file test_appcontainer.py --test-file test_lpac_recovery.py --test-file test_lpac_runtime.py
```

The complete suite has a 60-minute outer execution budget; targeted selections
retain 30 minutes. These are whole-suite scheduler limits, not relaxed test
deadlines: the installed pytest configuration remains 90 seconds by default,
with the existing 180-second native-case overrides. The runner's fast tests
cover both budgets, complete selection, mandatory provenance, interpreter
isolation, failure propagation and temporary-install cleanup.

The installer uses dependency constraints exported from `uv.lock` and copies
the acceptance suite plus the self-contained Windows Job Object, spawn and LPAC tests outside
the checkout. Tests verify that imports come
from the installed wheels, run console entrypoints, load the viewer's bundled
JS/CSS over loopback, and exercise Chinese/space-containing environment, workspace,
file, and MCP script paths. The viewer uses an isolated database and a test-only
Ctrl+C launcher so graceful shutdown works on headless Windows runners too.
Configuration and credentials are not inherited by the workflow subprocesses.
Build/install may access package indexes; the workflows themselves use only
local processes and loopback HTTP. The temporary installation is removed on exit.

For faster iteration against the existing development environment:

```powershell
uv run --no-sync pytest tests/acceptance
```

Only the installed-wheel provenance check skips in this mode. The dedicated
suite treats resource and unraisable-exception warnings as failures.

The native Job Object tests use real Windows processes and do not carry the
`sandbox` marker (that marker still requires a forked worker). They pair allowed
allocations/process creation with kernel-denied cases, check aggregate memory,
CPU termination, breakaway denial, cleanup after owner exit, and failed-setup
handle release:

```powershell
uv run --no-sync pytest tests/runtime/sandbox/test_windows_job.py
uv run --no-sync pytest tests/runtime/sandbox/test_spawn_executor.py
uv run --no-sync pytest tests/runtime/sandbox/test_appcontainer.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_executor.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_codeact.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_runtime.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_brokers.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_recovery.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_workspace.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_directories.py
uv run --no-sync pytest tests/runtime/sandbox/test_windows_session.py
uv run --no-sync pytest tests/runtime/sandbox/test_windows_api.py
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_policy.py tests/runtime/sandbox/test_platform_support.py
```

The Job Object suite tests resource control, not containment. The separate LPAC
suite tests an internal isolated stdlib launcher, not a public Windows sandbox.
See [Windows sandbox status](../docs/windows-sandbox.md).

The policy suites check every current `SandboxConfig` field and runtime
reconfiguration without provisioning native resources. Public Agent calls on a
simulated no-fork host must fail before cells, tools or model requests, including
with `require=False` or disabled guards. A copied, unvalidated non-fork method
cannot select a public worker or create a workspace, and an unknown CodeAct
backend cannot fall through to in-process execution. These tests also run in
installed-wheel acceptance; see the
[Windows policy contract](../docs/windows-sandbox-policy.md).

The managed Windows session suite combines platform-independent policy/lifecycle
checks with four Windows-only Agent tests. It covers immutable grants, explicit
units/deadlines, provisioning cancellation, cleanup order and retry, one-loop
ownership and concurrent-call refusal. Real calls exercise file/directory
brokering with direct-access denial, callback deadlines/cancellation, fresh
workers, staged typed application code, workspace persistence after a timeout,
and recovery-ledger cleanup. HTTPS construction/dispatch is checked at the
managed boundary; verified TLS and network-denial controls remain in the
separate broker suite. The session suite is self-contained for wheel testing.

The public Windows API suite checks staged native policy/grant declarations
and refusal before provisioning; importing these names does not enable launch.
The four native managed-session tests enter through `WindowsSandboxSession`
with only its launch gate replaced by a pytest fixture. All native admission,
grants and cleanup remain real, including an OS socket-denial check with the
test's Python import restriction removed. This is test-only admission, not a
caller release option. Both suites run in the Windows source matrix and the
clean-wheel runner, whose provenance check includes the new Windows module.

The native Windows stability suite is opt-in and excluded from normal runs and
the default clean-wheel runner:

```powershell
uv run --no-sync python -X pycache_prefix=logs/stability-cache -m pytest tests/runtime/sandbox/test_windows_stability.py -m stress -x -o junit_family=xunit1 --junitxml=logs/windows-stability.xml -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -W error::pytest.PytestUnhandledThreadExceptionWarning
```

It runs an idle control and three I/O-contention repetitions of real managed
provisioning, application staging, worker timeout/replacement, brokered-call
cancellation, a subsequent successful call, and repeatedly cancelled close.
Two more cases cancel provisioning after real dependency copies have started.
Every case retains the 180-second acceptance limit and enrolls its runtime in
an explicit, fresh recovery ledger. Pinned process handles verify actual worker
exit; checks also cover profile deletion, Job Objects, pipe handles, stderr
threads and runtime/ledger-entry removal.

The load uses two threads on the runtime's volume. Each creates, flushes with
`fsync`, reads and removes a 32 KiB file, with at least 20 ms between cycles.
The maximum simultaneous payload is 64 KiB, the cumulative write cap is
512 MiB per case, and less than 2 GiB free space causes failure. It never fills
the disk or removes an existing directory. Five fast load-generator checks run
without `-m stress`. JUnit `stability` properties retain phase times, successful
I/O cycles/bytes, worker counts and diagnostic host handle counts. Global handle
counts are not the leak oracle: first-use imports and shared pools can change
them; the owned native resource checks are authoritative for these cases.
This is bounded contention acceptance, not disk-full, crash or cross-device
performance certification. Run supported Python versions sequentially.

When sharing a checkout between Windows and WSL, use separate Python bytecode
caches (`python -X pycache_prefix=<platform-specific-cache> -m pytest ...`).
Old pytest-rewritten bytecode can retain the other platform's source paths,
which interferes with the framework's source-based method classification.

The internal spawn suite covers real worker startup, parent-only tools and
completion callbacks, persistent cells, cancellation/recovery, bounded IPC,
and process-tree teardown. It is not marked `sandbox` and does not imply native
filesystem/network/parent isolation. The clean-install runner copies this suite
alongside the Job Object tests and runs it against installed wheels.

The LPAC suite checks a private CPython runtime, sanitized environment, read-only
input snapshots, writable workspace, distinct-profile isolation, parent token and
handle denial, child-process denial, and IPv4/IPv6 TCP/UDP loopback denial with
working host controls. A paired ordinary-AppContainer control verifies that LPAC
does not inherit ALL APPLICATION PACKAGES grants. It also checks bounded
stdout/stderr, blocked stdin timeout, failed startup, profile cleanup retries,
Unicode paths and junction-safe cleanup. It does not use the fork-only `sandbox`
marker. No shared interpreter ACLs or global firewall settings are changed.
Both LPAC launch paths are checked for job membership at the native creation
boundary. Owner subprocesses exit without Python cleanup before the suspended
constructor returns and after user code starts; pinned process handles verify
kernel termination in all four cases. Closed/invalid jobs and rejected job-list
attributes must fail without retry or post-creation assignment. These checks
cover process ownership, not recovery of orphaned runtime trees/profiles.
The separate recovery suite enrolls new runtimes in an explicit host-private
store. It checks active leases, concurrent recovery, native ACL denial, profile
name conflicts, record/payload substitution, hardlinks, junctions, long paths,
retryable cleanup and real stdlib/framework use. Owner subprocesses exit during
registration and staging. Uncommitted records are preserved; tests use independent
test-only receipts to clean their own deliberately uncommitted profiles.
Legacy temporary files are never scanned or adopted. The recovery suite runs
without the fork-only marker and is included in native CI and clean-wheel tests.

The workspace suite pairs explicit `read` and `read_write` private-directory
grants. It checks kernel-denied creation, mutation, deletion, alternate streams,
temporary files and ACL/owner changes, plus allowed reads and writable controls.
Real persistent workers retain permissions after replacement, and real Agent
calls can complete after a denied write. Invalid modes fail before provisioning;
ACL setup failure cleans owned resources. Both modes use the same staged runtime
and input read-only permissions, direct network denial and parent-tool boundary.
This is not a live host-directory grant or a claim that all OS resources are
read-only. The suite runs in Windows CI and installed-wheel acceptance.

The directory-broker suite tests named live host directories without changing
host ACLs. It covers bounded multi-page listings, reads and replacement of existing
files, strict relative paths, non-inherited directory pins, root/ancestor rename
denial, junction substitution, hardlinks present at open, concurrent-write
exclusion, cancellation/draining and cleanup. An actual LPAC Agent uses explicitly
granted directory methods while direct access to the same host paths is denied.
Windows does not prevent same-user host code from adding hardlinks after an open;
the host remains trusted. This is not a direct Worker directory grant or a public
`FileRule` mapping. The suite runs in native CI and installed-wheel acceptance.

The separate `test_lpac_executor.py` suite covers the staged core dependency
closure and persistent framework worker: Unicode staging paths, async tasks and
thread wakeups, persistent values, explicit parent callback grants, refusal of
host-object traversal/transfer, real completion callbacks, cancellation, recovery,
partial frames, forged pickle messages, raw stderr limits and failed startup.
It is copied into both installed-wheel acceptance runs.

The internal CodeAct suite stages the import-safe `lpac_test_app.py` fixture and
explicitly installed PyYAML, and drives real Agent calls using scripted model
responses. It covers typed
parameters, nested dataclasses/enums, prefill/pre-ellipsis cells, persistent imports
and variables, approved tools and documentation, inline completion, return-value
validation, cancellation, timeout recovery, startup failure and deterministic
worker cleanup. Module name collisions and explicit package-parent requirements
are checked without launching workers. Public policy and backend changes are
refused instead of silently ignored. Both the test and fixture are copied into
the clean-wheel acceptance suite. PyYAML's native parser is exercised while
direct host-file/socket access and runtime writes remain denied. The staging
unit suite checks dependency constraints, extras, markers, cycles, editable and
hook rejection, resource copying, paths beyond `MAX_PATH`, collisions and failed
readiness. Broker policy tests cover bound defaults, positional/keyword/raw calls,
async tools, predicate errors and non-boolean decisions. Public
CodeAct/SandboxConfig selection remains unsupported.

The broker suite tests exact-file handles against junction replacement, forbidden
paths, read-only writes, byte limits and cancellation/close races. Its temporary
TLS server uses test-generated certificates to check pinned destination selection,
SNI/Host, certificate validation, environment isolation, redirects, body limits
and connection cleanup without public internet access. A real LPAC worker calls
both brokers while direct host file/network access remains denied; the CodeAct
suite also checks real Agent calls through the file broker.

Worker/CodeAct resource checks inspect the actual job at native creation, before
the suspended worker resumes, and pair unrestricted allocation with kernel-denied allocation.
They also cover invalid/native-setup/low-memory startup failures, cumulative
worker CPU exhaustion, restart with unchanged limits and disabled recovery.
Real Agent calls exercise memory and CPU limits without reinterpreting public
Linux headroom/CPU fields. These are per-worker budgets, not session-wide quotas;
all checks are included in the existing native and clean-wheel suites.

Shared execution tests also ensure that sandbox imports are not stripped merely
because a name exists in the host namespace. Restrictions still run before a
cell reaches the worker.

When sharing a checkout with WSL, keep bytecode caches separate. Pytest's
rewritten bytecode can retain `/mnt/...` source paths that native Python cannot
read, breaking source inspection even though the source files are unchanged:

```powershell
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP "nooa-pycache-windows"
uv run pytest
```
