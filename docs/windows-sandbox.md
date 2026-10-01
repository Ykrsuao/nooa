# Native Windows Sandbox Status

Windows resource control, an internal spawn/IPC runner, an LPAC standard-library
launcher, an internal persistent LPAC framework worker and an explicitly granted
internal Agent/CodeAct strategy are implemented. Explicit installed application
dependencies and parent-side tool argument policies are also supported internally. The
internal broker layer provides exact-file handles and fixed-endpoint HTTPS access. The
directory broker adds bounded live listing/read/replace operations without host ACL edits. The
internal worker and Agent strategy accept native Windows job memory/CPU budgets. The
LPAC launchers assign their job atomically during native process creation. The
internal runtime supports opt-in recovery of committed orphaned trees and profiles. The
private workspace has explicit internal read-only or read/write ACL modes. The
public sandbox executor is **not available on Windows yet**: `SandboxConfig.start_method` remains
`fork`, and both `require=True` and `require=False` fail when fork is unavailable.
No configuration silently switches a sandbox request to in-process execution.

The separate `nooa.runtime.sandbox.windows` namespace now declares
`WindowsSandboxPolicy`, `WindowsSandboxSession` and native grant types.
Policy construction is available, but entering the public session always fails
before provisioning. There is no caller enable flag or environment bypass.
See the [staged interface contract](windows-sandbox-policy.md#staged-public-windows-interface).

The [2026-10-01 upstream integration record](upstream-integration-20261001.md)
documents the selected upstream revision, overlap decisions, recovery snapshot
and verification of the combined source.

## Implemented: Resource Control

`src/nooa/_win_job.py` owns the shared, parent-side `ProcessJob`. Bash retains its
existing behavior through the compatibility import in `tools/_win_bash.py`;
ordinary shell sessions do not opt into new resource limits.

| Option | Enforced meaning |
| --- | --- |
| `memory_limit_bytes` | Absolute committed-memory cap for each process AND the entire job, including interpreter/launcher overhead. Not RSS or extra allocation headroom. |
| `cpu_time_limit_s` | Aggregate user-mode CPU time over the job's lifetime. Not wall-clock time, kernel-mode time, or a fresh per-cell budget. |
| `active_process_limit` | Maximum simultaneously active processes, including the root and any launcher helpers. |

Zero disables an optional resource limit. The job always has kill-on-close,
never grants breakaway, and is unnamed with a non-inheritable handle. Limits are
installed before assignment, and installation/assignment errors raise rather
than falling back to an unbounded process. The interface does not allow limits
to be raised after construction. Callers must close the job deterministically.

These flags are kernel-enforced resource controls, **not a complete security
sandbox**. Without token and object-access isolation, a same-user worker must
not be assumed unable to access its parent, duplicate handles, read credentials,
read/write arbitrary files, or use the network.

### Verification

`tests/runtime/sandbox/test_windows_job.py` exercises real native processes:

- Allocation succeeds without a budget and raises `MemoryError` with a budget.
- Parent and child allocations share one aggregate job budget.
- Child creation succeeds normally but fails at a one-process limit.
- `CREATE_BREAKAWAY_FROM_JOB` cannot opt a descendant out of the job.
- A running CPU loop is terminated by its job CPU limit.
- Closing the job kills the root and descendants; abrupt owner exit also reaps
  the owned process without running Python cleanup.
- Invalid limits and native setup failures do not leak temporary handles.

The workload waits behind trusted startup code until assignment. Tests use the
base interpreter for these minimal workers: on Windows, the virtual environment's
`python.exe` redirects to another process and consumes an additional process slot.
The installed-wheel acceptance runner copies this independent test suite and
runs it on Windows Python 3.12 and 3.13. The existing Bash lifecycle tests guard
the compatibility import and unchanged shell behavior.

## Implemented: Internal Spawn Worker And IPC

`runtime/sandbox/_spawn.py` contains `_SpawnExecutor`, an internal acceptance
backend requiring `unsafe_no_isolation=True`. It is not exported or selected by
CodeAct, CLI configuration or `SandboxConfig`. Do not use it for untrusted code.

The implementation reuses the existing cell loop, result DTOs, proxy classes and
parent-side broker:

- Bootstrap carries value snapshots and import/type descriptions, not the live
  agent, clients, `CurrentCall` or callback closures. Unsupported namespace values
  and non-importable/local definitions fail explicitly, rather than disappearing.
- Tools, framework callbacks (including the original `return_result` closure),
  and agent introspection run in the parent. Sync/async classification comes from
  that live parent, including for otherwise picklable tools. `_call` in the worker
  contains only a return-type snapshot, not the runtime call object.
- Only msgpack is decoded from worker messages; the parent never unpickles them.
  Spawn replies are limited to 32 MiB per frame. Malformed, oversized and
  out-of-sequence replies retire the worker.
- A bounded `hello`/bootstrap/`ready` handshake separates startup from the cell
  deadline. Job assignment and PID verification precede application bootstrap.
  CPython's Windows multiprocessing launcher bypasses the venv redirector.
- Cancellation, deadline failure and async close kill the job, drain the original
  IPC reader, cancel cooperative parent async calls and close process handles.
  Recovery is lazy: the next cell either starts an empty namespace or is refused
  when `recovery="disabled"`. Sync close rejects an active cell; use async close.
- Optional job budgets keep their own absolute/lifetime units. They do not reuse
  or reinterpret the Linux `max_memory_mb` / `max_cpu_seconds` configuration.

### Limits Of This Stage

This is process execution and resource control, **not containment**. It inherits
the parent environment, uses same-user permissions, and has unrestricted file,
network and parent-process access. Application module imports execute trusted
top-level code; imported modules must be import-safe and applications still need
the standard multiprocessing `if __name__ == "__main__"` entrypoint guard.
Multiprocessing's own preparation/main-module import can run before job assignment.
An isolated launcher must address this before any security claim.

Only declared data values cross as agent attribute snapshots. Ordinary live
objects remain proxies. Arbitrary local classes, closures as module globals, and
opaque method parameters are not supported bootstrap values. Closure callbacks
are supported via the explicit framework callback registry instead.
Parent tools retain the existing broker trust model: synchronous tools must not
block the event loop, and async tools must cooperate with cancellation. A worker
timeout does not forcibly interrupt arbitrary parent Python code.

`tests/runtime/sandbox/test_spawn_executor.py` runs real Windows processes without
the fork-only `sandbox` marker. It covers typed arguments, persistent cells,
parent-side mutation, completion signals, introspection, cancellation during
startup/execution, broker and cell deadlines, closed/disabled states, malformed
and hostile IPC, startup/assignment failures, and process-tree cleanup. It is
also copied into the clean-wheel acceptance suite for Python 3.12 and 3.13.

Public CodeAct backend selection and agent-facing sandbox context remain deferred;
neither capability probes nor doctor advertise these internal runners.

## Implemented: Internal LPAC Standard-Library Launcher

`runtime/sandbox/_appcontainer.py` owns `_AppContainerPython`; native profile, ACL
and process primitives live in `_win_appcontainer.py`. This is a separate
acceptance launcher. The internal framework integration described below reuses
the shared cell lifecycle, but **neither launcher is selected by public CodeAct
configuration**.

- Each instance owns a new AppContainer profile and a private runtime tree.
  Only trusted base CPython binaries and standard-library files are copied;
  site-packages, user source trees, configuration and secrets are not included.
  Source reparse points are rejected.
- Protected ACLs grant this profile read/execute access to the runtime and byte
  snapshots of inputs. Its disposable workspace defaults to modify access and
  an inherited low-integrity label; explicit `workspace_access="read"` instead
  grants read/execute only. No shared interpreter ACLs are changed.
- `pythonw.exe -I -S -B` starts with LPAC and the token-level child-process ban
  already installed. Exactly three stdio handles are inherited. The process is
  created suspended in a one-process kill-on-close job using the native job-list
  creation attribute, then resumed.
  Native setup failure raises without executing user code or using a fallback.
- The environment contains only Windows system paths and temporary/application
  data paths pointing into the workspace. Stdio transport has a shared output
  byte budget and a wall-clock timeout; blocked input and excess output trigger
  teardown. Calls on one instance are serialized.
- Clean close deletes the owned profile and private tree. Failed profile deletion
  retains ownership for retry. Tree cleanup rejects a substituted staging root
  and does not traverse workspace directory junctions.

### Capability And Runtime Constraints

The launcher grants the single `registryRead` capability needed for LPAC DLL
initialization; it does **not** grant network capabilities. OS-granted resources
and registry ACEs still exist, so this is not a claim of an exact filesystem-only
allowlist or a zero-capability token. A synthetic private HKCU canary remains
unreadable in the tests.

On the tested Windows build, console `python.exe` initialization conflicts with
the child-process ban even with `CREATE_NO_WINDOW`; `pythonw.exe` retains usable
redirected stdio without that conflict. `LOCALAPPDATA` is required for process
creation, but a synthetic workspace path suffices. The host's real profile path
is not inherited.

### Negative Tests

`tests/runtime/sandbox/test_appcontainer.py` uses real Windows processes and does
not carry the fork-only `sandbox` marker. Its checks include:

- Successful stdlib imports and stdin/stdout with a sanitized environment.
- Allowed snapshot reads/workspace writes paired with denied input/runtime
  writes, unrelated-file access, input ACL/owner changes, and access to another
  instance's workspace.
- An ordinary AppContainer control can read an ALL APPLICATION PACKAGES-granted
  canary, while LPAC cannot. This proves the LPAC distinction beyond merely
  checking that a token is an AppContainer.
- IPv4/IPv6 TCP/UDP loopback denial with working host socket controls; denial
  must be `WSAEACCES`, not a timeout or connection refusal.
- Denied parent memory, process-control and token-duplication access; an unrelated
  inheritable file handle is not transferred; child-process creation is blocked.
- Timeout, blocked stdin, stdout/stderr overflow, reusable launcher state, native
  startup/assignment/resume failures, repeated handle counts, profile deletion
  retry, and Unicode/junction-safe cleanup.

### Earlier Verification: Standard-Library Milestone

Native Windows 11 Home build 26200, 2026-09-29, before the persistent LPAC
framework worker was added:

- LPAC suite: 36 passed, including against both clean installations.
- Combined sandbox-area regression: 200 passed, 2 fork-only tests skipped;
  69 tests behind the fork-only marker were not selected.
- Embedded runtime regression: 167 passed, 1 skipped. Two test-owned background
  event loops were not being closed; their cleanup is fixed and regression-tested.
- Expanded runtime/config/shell regression: 1918 passed, 21 skipped, 69 fork-only
  tests deselected; 9 existing API deprecation warnings remained.
- Five-wheel installation outside the checkout: 99/99 passed on Python 3.12.13
  and 99/99 on Python 3.13.12. Temporary installations were removed; the project
  `.venv` was not replaced.
- Resource, unraisable-exception and thread warnings were treated as failures.
  Scoped Ruff, Pyright, SPDX, YAML and whitespace checks passed. Linux kernel
  containment was not re-tested on this Windows host.

Windows CI and the external five-wheel acceptance runner include the LPAC suite
on Python 3.12 and 3.13.

An initial expanded run also failed the existing spawn process-tree teardown
test once. Standalone, five additional independent repetitions, combined sandbox,
both installed-wheel suites and the expanded rerun passed. Its cause was not
established; no spawn production fix is claimed.

## Implemented: Internal Persistent LPAC Framework Worker

`runtime/sandbox/_lpac.py` provides `_LpacExecutor`. It reuses the internal spawn
executor's handshake, cell loop, result DTOs and cancellation/recovery lifecycle,
but replaces multiprocessing creation with the LPAC suspended-process launcher.
The old `_SpawnExecutor` remains an unrestricted, explicitly unsafe experiment.

### Private Dependencies And Asyncio

`_lpac_runtime.stage_framework()` accepts a live `_AppContainerPython`, not an
arbitrary destination interpreter. It copies NOOA's installed core dependency
closure from distribution manifests into that runtime's protected tree. Editable
NOOA copies only its package directory, never the repository. Other editable or
missing dependencies fail explicitly. Source reparse points, install hooks
(`.pth`/`.egg-link`), bytecode, hidden files and local `direct_url.json` metadata
are not transferred. Package version requirements and filename collisions are
checked before use. Only a completed staging operation marks the runtime ready.

CPython's Windows `_overlapped` extension creates sockets during import to
initialize Winsock extensions; LPAC rejects this even before an event loop is
constructed. The private runtime therefore replaces only its own
`Lib/asyncio/windows_events.py` with `_lpac_asyncio.py`. This retains CPython's
selector-loop task/timer scheduling and uses a Windows Event for cross-thread
wakeup. `await`, timers, futures and `asyncio.to_thread()` work without granting
network capabilities. Proactor loops, async descriptor I/O and async subprocess
transports are explicitly unsupported. The host interpreter and host event-loop
policy are never modified.

Staging is separate from worker startup and can involve many copied files.
There is no production staging cache or disk quota yet. The caller owns the
runtime and must close the executor before closing its profile/tree. Sessions
requiring separate filesystem identities must use separate runtime instances.

### Transport And Broker

- `_lpac_process.py` adapts the native LPAC process to the existing lifecycle.
  Assignment to the one-process job is atomic with creation and precedes resume
  and every framework import.
- `_lpac_transport.py` frames messages over the inherited stdin/stdout anonymous
  pipes. No network listener or additional inherited broker handle is needed.
  Frames are limited to 32 MiB, and incomplete frames and blocked writes have
  bounded deadlines. The parent never unpickles worker messages.
- Raw stderr is drained with a 64 KiB limit; exceeding it terminates the job.
  Raw stdout is the protocol stream: corrupting it retires the worker.
- Module globals and framework values use the existing data/import bootstrap;
  live agents and callback closures are not shipped into the child. Imports of
  unstaged application modules fail during bootstrap.
- The broker exposes only explicitly provided callbacks under exact names.
  Attribute traversal, mutation, iteration and automatic agent introspection
  are denied before host object resolution or tool-argument decoding. The internal
  CodeAct strategy below opts into documentation of granted tools only. Tool
  results must be supported data snapshots, not live host objects.
- Granting a callback grants its effects. This is not automatic filtering of
  file paths or URLs passed to a tool: the trusted callback must validate those
  arguments. Sync tools must not block the parent event loop; async tools must
  cooperate with cancellation.
- Timeout, cancellation, malformed messages and failed startup retire the
  process and drain pipe tasks. Namespace recovery follows `restart_empty` or
  `disabled`; disposable workspace files persist for the runtime's lifetime.

`tests/runtime/sandbox/test_lpac_executor.py` exercises this integration in real
LPAC processes, including forged wire messages, raw broker requests, parent-only
completion callbacks, Unicode paths and direct file/network denial after the
full core framework is loaded. It runs without the fork-only sandbox marker and
is included in native CI and both clean-wheel acceptance installations.

### Earlier Verification: Framework Worker Milestone

Native Windows 11 Home build 26200, Python 3.12.13, 2026-09-29:

- Persistent LPAC framework integration: all 19 tests passed.
- Combined sandbox-area regression: 219 passed, 2 fork-only tests skipped;
  69 tests behind the fork-only marker were not selected.
- Five-wheel installation outside the checkout: 118/118 passed on Python
  3.12.13 and 118/118 on Python 3.13.12, including all 19 integration tests on
  each version. Both acceptance commands exited successfully and removed their
  temporary installations; the project `.venv` was not replaced.
- Resource, unraisable-exception and thread warnings were treated as failures.
- Scoped Ruff lint/format, Pyright, SPDX, YAML and whitespace checks passed.
  Linux kernel containment was not re-tested on this Windows host.

The broader runtime/config/shell counts above are the earlier baseline, not a
new full-project run after this integration.

## Implemented: Internal Agent/CodeAct Integration

`runtime/sandbox/_lpac_codeact.py` provides `_LpacCodeActStrategy`, an internal
adapter at CodeAct's existing executor factory. The caller owns a prepared LPAC
runtime and explicitly supplies tool names, module exports and transport data
types. It does not register a backend or change the public Windows capability
probe, doctor result or `SandboxedExecutor`.

- `stage_framework(..., application_modules=...)` copies an explicit mapping of
  module names to `.py` files. Package `__init__.py` parents must be included
  explicitly. It rejects runtime/stdlib shadowing, Windows filename hazards,
  case-insensitive aliases, missing parents and source reparse paths. It never
  copies the surrounding project, `.env`, import hooks or an arbitrary Python
  environment. Non-core third-party distributions are staged only through the
  explicit requirements described below.
- Module exports are restored inside the worker, including ordinary application
  functions. Method parameters are data snapshots, not automatically granted
  callbacks. The explicitly declared transport types include nested annotated
  model/dataclass/enum fields. Their host constructors and validators are trusted
  application code, not isolated code; declaring a type authorizes those effects.
- Only named Agent methods become parent callbacks. No live Agent or its
  attributes enter the worker. `doc(self)`, `methods(self)` and `doc(self.tool)`
  expose prepared text for the granted tools; `variables(self)` is empty.
  Traversal and ungranted names are refused. These tool documentation calls
  currently support only the default formatting arguments; local data-type
  documentation retains the normal formatting options.
- The real CodeAct loop runs input inspection, pre-ellipsis code, persistent cells,
  parent tools and inline `return_result` through `runtime.execute_code`, retaining
  restrictions, hooks, events and parent return-value validation. Per-call workers
  close on completion, generation failure and cancellation. The owner must await
  all calls before closing the runtime; calls sharing it also share its writable
  workspace and filesystem identity.
- Sandbox cells retain explicit imports even if the host has matching globals.
  The host namespace is not proof that an isolated worker has imported a module.
  Redundant-import cleanup for the normal in-process backend is unchanged.
- Non-default public sandbox policy is refused, as is changing this strategy to
  an in-process backend. Caller-owned `session_locals` synchronization, implicit
  live attribute grants and arbitrary callback parameters are not supported.
  Use explicit tools to read or mutate approved parent state.

`tests/runtime/sandbox/test_lpac_codeact.py` drives real Agent calls with scripted
model responses and an explicitly staged import-safe application fixture. Both
are included in native CI and the external installed-wheel acceptance runner.
The shared import regression lives in `tests/runtime/test_execute_code.py`.

### Verification: Agent/CodeAct Milestone

Native Windows 11 Home build 26200, 2026-09-30:

- All 20 internal Agent/CodeAct tests passed in native Python 3.12.13 and in
  clean-wheel Python 3.12.13 and 3.13.12 installations.
- Runtime and strategy regression: 2329 passed, 20 skipped, 69 deselected;
  9 existing API deprecation warnings remained. This is not a full-project or
  Linux containment run.
- Final sequential five-wheel installation acceptance: 138/138 passed on Python
  3.12.13 and 138/138 on Python 3.13.12, including the framework bootstrap smoke
  test with the startup budget described below. Both commands exited with code
  zero and removed their own temporary installations. The full native regression
  above preceded this test-only timeout adjustment; production code was unchanged.
- Resource, unraisable-exception and thread warnings were treated as failures.
- Scoped Ruff lint/format, SPDX, YAML and whitespace checks passed. Pyright
  passed for `_lpac.py`, `_lpac_runtime.py` and `_lpac_codeact.py`. Checking
  `actor.py` still reports three message-list variance errors at lines 943,
  1017 and 1026; those lines are unchanged by this integration.

The initial concurrent clean-install rerun completed with 137 passed and one
failure on each Python version: the existing framework bootstrap smoke test
exceeded its 30-second timeout. An isolated rerun passed in 28.7 seconds; a
separate diagnostic then measured 41.8 seconds for cold framework imports and
0.016 seconds for the async/thread round trip, with a successful process exit.
The smoke test now uses the internal CodeAct strategy's 60-second startup budget.
Cell deadlines, timeout-recovery tests, permissions and production defaults are
unchanged.

An earlier interrupted verification left two known temporary installation
directories, `nooa-install-8i35mvuf` and `nooa-install-tj8lc3r0`, under the user's
temporary directory. Manual cleanup was blocked by the execution environment's
safety policy, so they were retained. This does not replace the production
orphan-recovery work listed below. The project `.venv` was not replaced.

## Implemented: Explicit Dependencies And Tool Policies

`stage_framework(..., application_requirements=("PyYAML>=6",))` extends the
private runtime with named, already installed PEP 508 requirements:

- Dependency markers, requested extras, transitive requirements and cycles are
  resolved against installed metadata. Every version constraint is checked,
  including later constraints on a previously visited distribution. Unknown
  extras, missing packages and active URL requirements are refused.
- No package manager, downloads, build hooks or environment activation run during
  staging. Manifested package resources and native extensions are copied with
  the package; they retain the private runtime's read-only ACLs.
- Added distributions with `.pth`/`.egg-link` hooks or editable-install metadata
  are refused. Existing core install hooks (including setuptools' `.pth`) remain
  excluded, preserving the earlier core bootstrap behavior; none are executed.
  Packages that depend on external data, unstaged DLLs, import hooks or unavailable
  LPAC capabilities remain unsupported. Installed distributions are trusted
  inputs, not cryptographically verified artifacts.
- Standard-library/NOOA shadowing, case-insensitive file collisions and
  module/package import-root conflicts are refused. Explicit application source
  cannot shadow installed single-file modules either. Failed initial staging
  never marks a runtime ready; close that runtime instead of using partial files.

`_LpacExecutor` and `_LpacCodeActStrategy` accept optional `tool_policies`, a
mapping from explicitly granted tool names to trusted synchronous predicates:

- The parent binds arguments against the actual callback signature, applies
  defaults, and gives the predicate a read-only mapping of parameter names to
  values. Positional, keyword and raw broker calls use the same check.
- Only literal `True` authorizes invocation. False/truthy non-boolean results,
  invalid argument binding, predicate exceptions and disguised coroutine results
  deny the call before the tool executes. Policy exception details are not sent
  to the worker. Policy names that are not granted tools are rejected.
- Checks run for synchronous and asynchronous tools. They do not add grants,
  expose predicate functions in the child, or change framework completion and
  introspection permissions. Ungated tools retain their explicitly granted
  effects, so callers must attach policies wherever argument restrictions matter.
- Predicates must be fast, synchronous, side-effect-free trusted host code.
  Declared transport constructors/validators run during argument decoding before
  the predicate and remain trusted. The mapping is shallowly read-only, not a
  second isolation boundary. Do not use these predicates to validate hostile
  application objects before their constructors have run.
- This is a policy enforcement entry point, not a generic safe filesystem or
  HTTP broker. Prefer logical resource identifiers mapped to owned handles or
  approved data. A path-prefix check followed by an ordinary open can race a
  reparse-point change; a URL hostname check alone does not prevent redirects or
  DNS rebinding. Those guarantees must live in the trusted tool implementation.

`test_lpac_runtime.py` covers closure resolution and staging failures without
launching workers. The Agent suite also stages PyYAML and exercises its native
parser in a real LPAC worker while checking direct file/network denial and
read-only extension files. Broker tests cover default binding, raw forged calls,
async tools, fail-closed predicate results and approved logical-document reads.
These suites are included in native CI and the installed-wheel acceptance runner.

### Verification: Dependencies And Policies

Native Windows, 2026-09-30:

- Python 3.12.13 focused checks: 24 staging, 26 persistent-worker and 22
  Agent/CodeAct tests passed across the targeted run and final two-test rerun.
- Shared execution and strategy regression: 970 passed. This is not a
  full-project or Linux containment run.
- Sequential five-wheel clean installations: 171/171 passed on Python 3.12.13
  and 171/171 on Python 3.13.12. Both commands exited successfully and removed
  their temporary installations, including all 33 tests added in this stage.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX, YAML and whitespace
  checks passed. Resource, unraisable-exception and thread warnings were treated
  as failures. The project `.venv` was not replaced.

The initial integration run caught a compatibility regression from rejecting
setuptools' already-excluded core `.pth` hook; core hooks remain excluded and
added dependency hooks are refused. A new Agent socket assertion initially hit
the AST guard instead of LPAC. That test now permits only ordinary socket imports
in its own configuration and verifies `WSAEACCES` from the kernel. Production
restrictions, capabilities and timeout settings were not relaxed.

## Implemented: Exact-File And Fixed-HTTPS Brokers

`_lpac_files._FileBroker` owns a mapping of logical identifiers to `_FileGrant`
objects. `_lpac_http._HttpsBroker` owns a mapping of identifiers to
`_HttpsEndpoint` objects. Applications explicitly grant their bound async methods
as executor tools, or delegate to them from granted Agent methods. Neither broker
is installed automatically, and neither live broker object enters the worker.

### File Object Grants

- A grant names an existing regular file on a fixed local drive and whether it
  is writable. UNC/device paths, streams, traversal, reserved names and non-file
  targets are refused. No file creation, directory listing, deletion or rename
  API is provided.
- Native `NtCreateFile` opens with `OBJ_DONT_REPARSE`, without create/truncate or
  write/delete sharing. Reparse traversal is refused during the open itself,
  rather than following a substituted junction and checking it afterwards.
  Final handle type/path checks and non-inheritance are also enforced.
- Reads and writes always use that owned handle, never re-open a worker-supplied
  path. The grant follows the file object, not a changing directory namespace.
  Writable grants reject pre-existing hard-link aliases. Other writers and
  file replacement are excluded while the handle is owned.
- `read(name)` returns bounded bytes. `write(name, data)` replaces bytes only in
  an explicitly writable file. The per-file byte limit defaults to 1 MiB and is
  capped at 4 MiB; it is not a session quota or an atomic/durable transaction.
  Disk-full errors may leave a partial write.
- File I/O runs off the event loop, serialized with close. Cancellation drains
  already-started I/O before returning; it does not undo a completed write.
  Slow local storage is not forcibly interrupted by a hard broker deadline.
  Owners must await calls and `aclose()` before releasing the broker.

### Fixed HTTPS Resources

- Each trusted grant specifies one exact HTTPS URL and one public unicast IP.
  The worker supplies only a resource identifier, not a URL, request body,
  header, method or destination address. Requests are GET-only.
- HTTPcore's async transport connects to the pinned numeric address while
  retaining the original hostname for Host, SNI and certificate verification.
  There is no hostname DNS lookup, retry, proxy or redirect following.
  Private/local/multicast/reserved and transition/scoped IP addresses are refused.
  A DNS/CDN address change requires a new trusted grant.
- TLS uses an explicit CA file or certifi, never environment-supplied trust
  roots. No ambient proxy, netrc, cookies or client credentials are used.
  Caller-supplied CA files remain trusted host configuration.
- Redirects and non-identity content encodings are refused. Response bytes are
  bounded (1 MiB default, at most 4 MiB), and a finite deadline covers connection,
  TLS and the complete response. Requests own and close their connection pool;
  timeout/cancellation closes the connection before returning.
- Results contain status, content type and raw bytes only. These are per-request
  restrictions, not a session-wide request quota or a generic SSRF-safe HTTP API.
  Host routing and the trusted grant configuration remain part of the trust base.

The self-contained `test_lpac_brokers.py` suite includes a deterministic junction
replacement race, handle ownership and cancellation checks, non-BMP paths,
resource limits, and real TLS handshakes against a temporary local server.
Test-only socket routing connects the approved public address to that server;
production address checks are not disabled. It verifies SNI/Host, certificate
and environment trust rejection, no redirects/cookies/proxy/DNS, body bounds
and socket closure. A real LPAC worker uses both brokers while direct host file
and socket access remain denied. The CodeAct suite also exercises the file
broker through a real Agent.

Both suites are included in native CI and installed-wheel acceptance. The wheel
runner's overall suite budget is now 1800 seconds for the expanded test list;
individual test deadlines, production cell deadlines and permissions are unchanged.
Public `SandboxConfig` still describes directory/direct-network semantics, not
these named broker capabilities. No partial or silent mapping was added.

### Verification: Broker Milestone

Native Windows, 2026-09-30:

- All 60 new broker/Agent checks passed on Python 3.12.13, including the native
  no-reparse open, real TLS, a real LPAC worker and a real Agent call.
- Shared execution and strategy regression: 970 passed. Scoped Ruff, Pyright,
  SPDX, YAML and explicit-encoding checks passed. Resource, unraisable-exception
  and thread warnings were treated as failures.
- Sequential five-wheel clean installations: 231/231 passed on Python 3.12.13
  in 942.14 seconds and 231/231 on Python 3.13.12 in 976.54 seconds. Both
  commands completed successfully and removed their temporary installations.
  This is not a full-project or Linux containment run.

The temporary test CA now includes authority/subject key identifiers and CA key
usage, and focused TLS tests enable strict X.509 verification on Python 3.12 as
well as 3.13. All 58 non-worker broker checks passed again with this stricter
fixture. This changes test certificates only, not production TLS verification.
The Python 3.12 clean-install run used the earlier certificate fixture; the
stricter fixture was separately retested on native Python 3.12 and included in
the Python 3.13 clean-install run. Production code was unchanged between runs.
`cryptography>=41.0` was added explicitly as a test dependency without replacing
the project environment or changing locked dependency versions.

## Implemented: LPAC Worker And Agent Resource Budgets

`_LpacExecutor` and `_LpacCodeActStrategy` accept `memory_limit_bytes` and
`cpu_time_limit_s`, both disabled by default (`0`). They reuse `ProcessJob`
without translating Linux `SandboxConfig` limits:

- Memory is an absolute committed-byte cap for the process and its job, including
  the interpreter, native extensions and framework bootstrap. It is not RSS,
  virtual-address-space headroom, or a guarantee that the runtime will fit.
- CPU is cumulative user-mode CPU time for the job's lifetime, including startup
  and all cells in that worker. It is not wall time, kernel CPU time or a fresh
  budget for each cell. Parent-side tools are outside both job resource budgets.
- Limits are validated and installed before creating the suspended worker; job
  assignment still precedes resume. Invalid integers, native setup failures and
  insufficient startup resources fail closed. There is no retry without limits
  and no fallback to host execution. The one-process limit and LPAC file/network
  restrictions remain in place.
- An allocation refusal may surface as a recoverable `MemoryError` while the
  worker and its namespace remain alive. Job termination retires the worker and
  closes its IPC streams. It is reported as worker death, not a falsely precise
  diagnosis that distinguishes every possible native termination cause.
- A replacement worker receives a new job with the same configured limits and
  an empty namespace. Its CPU accounting starts again. The strategy also accepts
  `recovery="disabled"` to refuse further cells after worker failure within that
  Agent call. A new Agent call still gets a new executor and job.

These options are **per-worker budgets, not session-wide quotas**. Repeated
recovery or new Agent calls can consume additional CPU and allocations. Callers
must bound their overall workflow separately; broker effects, disposable
workspace usage and parent memory are not capped by these options.

Non-default public `SandboxConfig` settings, including Linux memory/CPU limits,
workspace grants, network access and `require=False`, remain rejected by the
internal strategy even when native job budgets are supplied. Public backend
selection, context blocks and doctor diagnostics are unchanged.

The existing self-contained worker and CodeAct suites now include native job
inspection before resume, paired allowed/denied allocation, isolation with caps,
invalid/setup/low-memory startup failures, CPU exhaustion across cells, both
recovery modes and real Agent resource-limit calls. They are already included
in native CI and both installed-wheel acceptance runs.

### Verification: Worker Resource Budgets

Native Windows, 2026-09-30:

- Python 3.12.13: all 70 worker/Agent tests passed in 648.52 seconds, including
  the 21 added checks. This includes kernel limit inspection before resume,
  memory refusal, startup failures, CPU termination and both recovery modes.
- Shared execution, strategy and public-platform regression: 973 passed.
  The independent native Job Object suite also passed all 23 checks.
- Python 3.13.12 five-wheel clean installation: 252/252 passed in 1016.00
  seconds, including all new worker/Agent resource checks. The command exited
  successfully and removed its temporary installation.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX and whitespace checks
  passed. Resource, unraisable-exception and thread warnings were failures.

The project `.venv` was not replaced. Python 3.12 clean-wheel installation was
not repeated in this stage; its updated worker/Agent suite ran in the existing
native development environment. This is not a full-project or Linux containment
run.

The initial run exposed test assumptions, not permission changes: child-process
policy denial can report WinError 367 instead of `PermissionError`; synthesized
worker-death exceptions are checked by type rather than message text; and the
Agent recovery probe now uses a normal variable lookup instead of the forbidden
`globals()` builtin. Native limits and production restrictions were not relaxed.

## Implemented: Atomic LPAC Job Launch

Both `_AppContainerPython.run()` and `LpacProcess` pass their already-configured
`ProcessJob` into the native `SuspendedProcess` constructor. Its
`PROC_THREAD_ATTRIBUTE_JOB_LIST` assigns the process to that job as part of
`CreateProcessW`, rather than performing a separate assignment after creation:

- A successfully created process is job-owned even before the native creation
  call returns to the launcher's Python lifecycle. Abrupt owner exit closes the
  non-inherited job handle and terminates a suspended or running worker.
- LPAC, child-process prohibition, the explicit three-handle stdio list and
  suspended startup are unchanged. The job handle is borrowed only for creation;
  it is neither duplicated into the child nor made inheritable.
- Memory, CPU and process-count limits are installed before creation. A closed
  job, rejected creation attribute or invalid native job aborts startup. There is
  no retry without the job attribute or fallback to post-creation assignment.
- Ordinary Bash and the explicitly unsafe multiprocessing spawn experiment retain
  their existing `ProcessJob.assign()` lifecycle. This change does not make that
  spawn experiment isolated or close its own preparation/assignment window.

The native launcher suite observes job membership immediately after the real
`CreateProcessW` succeeds, before the constructor returns, for both launchers.
It tests a real invalid job handle and rejects closed jobs before creation.
Separate owner subprocesses call `os._exit()` at that exact creation checkpoint
and after user code starts. Tests pin the worker process handle before allowing
the owner to exit, verify kernel termination without Python cleanup, and retain
an emergency termination handle so a regression cannot leak the test worker.
The framework suite also reads back native resource limits at creation.
All of these checks run through the existing native CI and clean-wheel suites.

This milestone addresses process ownership, not recovery of private runtime
trees or AppContainer profiles. Its crash tests borrow a test-owned runtime
and profile that the surviving test process closes normally. The separate opt-in
recovery mechanism below tracks newly enrolled resources. Public Windows backend
selection and permission mapping remain unavailable.

### Verification: Atomic Job Launch

Native Windows, 2026-09-30:

- Before the change, both native-creation regression cases failed because the
  new process was absent from its intended job. Both now pass, alongside the
  invalid/closed-job checks and all four abrupt-owner-exit scenarios.
- Python 3.12.13 native JUnit report: 138 passed, zero errors/failures/skips,
  in 708.56 seconds across the LPAC launcher, worker, Agent and Job Object suites.
  Nine checks were added. Shared execution, strategy, platform and Bash lifecycle
  regression separately completed with 1002 passed in 26.74 seconds.
- Python 3.13.12 five-wheel installation: the initial acceptance process stopped
  without a final result after completing 145 tests in the launcher, installed
  workflows, broker and Agent modules, plus part of the worker module. This is
  not recorded as a successful uninterrupted `smoke_install.py` command.
- The same installed environment then completed the entire worker, staging,
  spawn and Job Object modules plus the wheel-provenance check: 117 passed in
  491.28 seconds, exit code zero. Together the completed modules cover all 261
  acceptance tests, with the provenance test repeated. Hashes of the four changed
  installed production modules matched the source files.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX and whitespace checks
  passed. Resource, unraisable-exception and thread warnings were failures.

The interrupted installation directory `nooa-install-vkeac15m` under the user's
temporary directory remains: an explicitly scoped, path-checked cleanup command
was rejected by the execution environment's policy. No alternative deletion was
attempted. The interrupted run may also have left LPAC staging/profile resources;
their recovery is not claimed. The project `.venv` was not replaced. This is not
a full-project or Linux containment regression.

## Implemented: Opt-In LPAC Orphan Recovery

`_AppContainerPython(recovery_directory=...)` enrolls each new runtime in an
explicit host-private store. `_AppContainerPython.recover_orphans(directory)`
reclaims only committed inactive entries in that store. This is an internal
interface, not a public sandbox backend or a background cleanup service:

```python
from pathlib import Path
from nooa.runtime.sandbox._appcontainer import _AppContainerPython

# Choose an absolute path on a fixed local drive with existing ancestors.
store = Path(r"C:\Users\example\AppData\Local\nooa-lpac-ledger")
report = _AppContainerPython.recover_orphans(store)
with _AppContainerPython(recovery_directory=store) as runtime:
    result = runtime.run("print(42)")
```

The store is created with a protected ACL granting full access only to the
current user and SYSTEM. An existing directory must already have that exact
owner/private ACL; it is not adopted by rewriting its permissions. Reparse
traversal, UNC/device paths, non-fixed drives and unsafe filename components
are refused. Ancestor and store directory handles prevent namespace replacement
while enrollment or recovery is in progress. The LPAC profile receives access
only to its payload, never to the store, ownership metadata or lease files.

### Ownership And Cleanup

- Each `runtime-<uuid>` entry contains a private `lease`, an atomically replaced
  `owner.json`, and its `payload` tree. The record binds its format version,
  entry name, payload volume/file identity, and exact `nooa.lpac.<uuid>` profile
  name. A native profile is always created exclusively; an existing name is
  never adopted or deleted after a creation conflict.
- Exclusive non-inherited file handles establish liveness, not PID numbers,
  process names or elapsed time. A store lock serializes enrollment and cleanup.
  Active entries remain untouched, including during recovery from another thread.
- Recovery requires a committed profile record and matching payload identity.
  Invalid/missing/oversized/hardlinked records, missing or aliased lease files,
  and substituted payload roots do not authorize deletion. Unrelated files and
  directories are ignored. Profile names cannot redirect cleanup to another entry.
- Normal close and recovery share the lease cleanup implementation. The profile
  is deleted before the payload; native missing-profile results allow retry after
  partial cleanup. Payload and entry directories remain pinned through deletion.
  Nested directory junctions are unlinked, not traversed.
- The report separates `recovered`, `active`, `unclaimed` and per-entry `errors`.
  Per-entry errors preserve ownership information for retry. Invalid stores and
  store-lock acquisition failures raise instead of producing an empty success.
  A normal-close failure retains its live lease for an explicit close retry.

Private stdlib/dependency copying, the worker's package import path and tree
cleanup use extended Windows paths. Nested dependency/resource names may exceed
`MAX_PATH` without changing the host's long-path registry setting. Package
`__file__` values consequently use extended spelling as well. This does not
relax source/reparse checks, grant access to additional files, or promise that
arbitrarily long launch/workspace paths or every third-party library are supported.

### Deliberate Limits

Recovery is opt-in and only applies to newly enrolled resources. Without
`recovery_directory`, the existing temporary-runtime lifecycle is unchanged.
There is no scan or adoption of legacy temporary directories, including the
interrupted installation recorded above. Empty stores and `recovery.lock` remain
intentionally so that cleanup cannot replace the shared locking namespace.

A crash before the profile record is committed leaves an `unclaimed` entry.
In particular, native profile creation and record replacement are not one atomic
transaction: a profile created immediately before a crash may need manual
investigation. Recovery does not infer ownership from its name. File flushing and
atomic replacement protect ordinary process-crash handling, not arbitrary power
loss or filesystem corruption. Disk quotas, automatic scheduling and privileged
cross-user cleanup are not implemented.

Other same-user host code is trusted. The ledger protects against LPAC workers,
corrupt records and namespace replacement, not a malicious process with the
owner's privileges. Callers must still stop/close all executors before closing
their runtime. Atomic Job Object assignment handles worker termination after
owner death; filesystem/profile cleanup is a separate operation.

`test_lpac_recovery.py` exercises actual ACL denial, exclusive leases, concurrent
recovery, profile conflicts, identity substitution, junctions, corrupt records,
retryable failures, long paths and real stdlib/framework execution. Separate
owner subprocesses exit without Python cleanup after registration, during staging
and before profile registration commits. Only the test's independent ownership
receipt permits cleanup of its intentionally uncommitted profile; production
recovery preserves such entries. This suite runs in native Windows CI and the
installed-wheel acceptance runner.

### Verification: Orphan Recovery

Native Windows, 2026-09-30:

- Python 3.12.13: the expanded launcher/recovery/staging/worker/Agent run passed
  177 of 179 tests in 782.42 seconds. Two existing Agent assertions compared
  ordinary path strings against the now-extended package paths; after switching
  those assertions to exact file identity, both passed in a 105.07-second rerun.
  The source/native-extension write denial and host file/network denial checks
  remain in place. These are combined results, not a claim of one green full run.
- All 40 recovery checks and 25 staging checks passed in the expanded run.
  The long-path copy/recovery regressions also passed again after their path
  lengths were made independent of the temporary directory's spelling.
- Shared execution, strategy and public-platform regression: 973 passed in
  15.24 seconds. Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX, YAML
  and whitespace checks passed. Resource, unraisable-exception and thread
  warnings were treated as failures.
- Python 3.13.12 five-wheel clean installation: 140/140 targeted checks passed
  in 519.06 seconds, covering wheel provenance and the complete launcher,
  recovery, staging and CodeAct modules. The installer exited with code zero
  and removed its temporary installation. This used repeated `--test-file`
  options, not the installer's full default acceptance suite.

The initial managed-framework test reproduced a `FileNotFoundError` when copying
a dependency to a path beyond `MAX_PATH`, despite its parent existing. Extended
paths fixed the minimal copy regression and real persistent-worker imports,
without shortening the recovery store or changing global Windows settings.

The project `.venv` was not replaced. Python 3.12 clean-wheel installation and
Linux containment were not rerun in this stage. The previously interrupted
`nooa-install-vkeac15m` directory was neither adopted nor cleaned by recovery.

## Implemented: Policy Mapping And Refusal

The [Windows policy contract](windows-sandbox-policy.md) maps all 14 current
`SandboxConfig` fields to the existing mechanisms and documents their semantic
gaps. Internal acceptance of the default configuration is only a no-policy
sentinel; it does not claim that LPAC implements Linux defaults.

Two regression-first fixes close unvalidated `model_copy(update=...)` admission
gaps: the public executor rechecks its fork-only start method, and CodeAct rejects
unknown execution backends instead of falling through to in-process execution.
Both checks precede execution effects. The 72 policy/platform tests are included
in Windows CI and targeted installed-wheel acceptance.

Verification on 2026-09-30 passed 1102 shared/configuration/strategy checks and
two native positive controls on Windows Python 3.12.13. Targeted installed-wheel
checks passed 73 tests on Python 3.12.13 and 103 on Python 3.13.12.
WSL2 Python 3.12.3 separately
passed 230 regular sandbox checks and all 69 fork-containment tests. See the
[verification record](windows-sandbox-policy.md#verification-policy-refusal)
for selection, skips and cleanup details. Public Windows sandbox selection
remains unavailable.

## Implemented: Explicit Private Workspace Permissions

`_AppContainerPython(workspace_access="read")` provisions a read-only private
workspace; `"read_write"` retains the previous writable behavior and remains
the default. The profile's protected ACL, installed before staging or launch,
enforces the mode. Invalid values fail before allocating a profile, tree or
recovery lease. Failed ACL installation aborts and cleans owned resources.

Both modes permit reading and listing. Read-only mode denies creation, append,
truncate, rename, deletion, nested writes and alternate streams. Only writable
mode adds the modify ACL and inherited low-integrity label. Neither grants
workspace ACL/owner changes, runtime/input writes, host paths or network access.
The read-only `workspace_access` property describes the creation-time choice;
there is no permission-changing setter. A mode change requires a new runtime.

Staged framework workers and real Agent calls use the same runtime ACLs, including
after worker replacement. Host-side tools remain independent explicit grants,
not constrained by the worker's workspace mode. Newly enrolled read-only
runtimes support the same recovery ledger and normal ownership cleanup.

Synthetic temporary/application-data environment paths remain beneath the
workspace, including Windows' package-directory rewriting. No extra writable
temporary directory is provisioned for read-only mode. Code requiring scratch
files must choose writable mode. Windows CPython `tempfile` may retry ACL-denied
opens as filename collisions; the new test bounds its own retry count instead
of weakening ACLs or modifying the staged standard library.

The self-contained `test_lpac_workspace.py` suite pairs both modes through real
stdlib launches, persistent-worker recovery and scripted-model Agent calls.
It is included in native Windows CI and installed-wheel acceptance. See the
[internal mapping contract](windows-sandbox-policy.md#internal-workspace-mapping).
This is not a public `SandboxConfig` adapter or a live host-directory grant;
public selection, capability probes, context blocks and doctor remain unchanged.

### Verification: Private Workspace Permissions

On 2026-09-30, Windows Python 3.12.13 passed all 159 workspace/launcher/policy/
platform tests, including the 43 new workspace checks. The shared execution,
strategy, configuration and Bash lifecycle run passed 1027 tests. A fresh
Python 3.13.12 five-wheel installation passed all 116 targeted checks and
removed its temporary environment. Scoped static checks passed; resource,
unraisable-exception and thread warnings were failures.

See the [verification record](windows-sandbox-policy.md#verification-private-workspace-permissions)
for the initial temporary-file test corrections, selection and cleanup details.
This stage did not rerun Python 3.12 clean-wheel installation or Linux containment.

## Implemented: Internal Live Host Directory Broker

`runtime/sandbox/_lpac_directories.py` provides `_DirectoryBroker` and
`_DirectoryGrant`. Named existing host directories support bounded live listing,
reading and replacement of existing regular files. Directory write permission
is opt-in and applies to its entire subtree. There is no creation, deletion,
rename, append API, or direct worker filesystem grant.

```python
from pathlib import Path
from nooa.runtime.sandbox._lpac_directories import _DirectoryBroker, _DirectoryGrant

async with _DirectoryBroker({"docs": _DirectoryGrant(Path(r"C:\project\docs"))}) as files:
    entries = await files.list("docs")
    content = await files.read("docs", "guide.txt")
```

Host roots and their readable ancestors are pinned for the broker's lifetime.
Descendant components are opened relative to pinned directory handles, never
by checking a path prefix and then trusting a later path-based open. Native
no-reparse traversal plus explicit leaf attributes refuse junctions and other
reparse points. Strict relative components, final-name checks and single-link
file admission reject traversal, stream paths, aliases and pre-existing hardlinks.
UNC/device paths, drive-root grants and missing roots are refused.

No host ACL, owner or integrity label is changed. The worker receives neither
directory handles nor implicit access to broker methods. Trusted Agent methods
must delegate and be explicitly granted. A real LPAC Agent can copy between
separate read-only input and writable output directories through the broker
while direct access to the same host paths remains denied.

Operations serialize bounded disk I/O and drain it on cancellation before
closing handles. Files exclude other writers and replacement during an operation;
host edits between calls remain visible. Listings have bounded native pages and
entry counts, not snapshot semantics. Creation/cleanup changes no host data
beyond explicitly requested writes. The caller must await calls and `aclose()`.

Windows sharing flags do not prevent trusted same-user host code from adding
hardlinks after the admission check. This limitation was reproduced during
testing; no protection against a malicious same-user host is claimed. The LPAC
worker cannot directly edit the host tree and the broker provides no link API.
Per-call byte/entry limits are not session quotas or hard disk-I/O timeouts.

The self-contained `test_lpac_directories.py` suite is included in native CI and
installed-wheel acceptance. Existing exact-file and recovery code reuse the
extended native open helper and retain their earlier contracts. See the
[directory policy contract](windows-sandbox-policy.md#internal-host-directory-broker).
Public `FileRule` translation, worker mounting, capability/doctor reporting and
Windows backend selection remain unavailable.

### Verification: Host Directory Broker

On 2026-09-30, Windows Python 3.12.13 passed all 116 directory/policy/platform
checks and, separately, all 99 existing broker/recovery tests. Shared execution,
strategy and configuration regression passed 1018 tests. A fresh Python 3.13.12
five-wheel installation passed all 216 targeted tests in 438.17 seconds and
removed its temporary environment. Scoped static checks passed, with resource,
unraisable-exception and thread warnings treated as failures.

The [verification record](windows-sandbox-policy.md#verification-host-directory-broker)
details the leaf-junction admission fix and the observed host-hardlink limitation.
Python 3.12 clean-wheel installation and Linux containment were not rerun.

## Implemented: Internal Managed Windows Session

`_WindowsSandboxPolicy` and `_WindowsSandboxSession` assemble the existing LPAC
runtime, dependency staging, named brokers and Agent strategy behind one owned
lifecycle. The implementation remains internal; the staged public declarations
reuse it behind a closed launch gate and do not enable public Windows selection.

```python
from pathlib import Path
from nooa import Agent, strategy
from nooa.runtime.sandbox._lpac_directories import _DirectoryGrant
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy
from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession

policy = _WindowsSandboxPolicy(
    workspace_access="read",
    directories={"docs": _DirectoryGrant(Path(r"C:\project\docs"))},
    memory_limit_bytes=1024**3,
    broker_timeout_s=15,
)

async def run(llm):
    async with _WindowsSandboxSession(policy) as session:
        backend = session.strategy()

        class Reader(Agent, llm=llm):
            @strategy(backend)
            async def summarize(self, filename: str) -> str:
                """Summarize the file using await self.read_directory('docs', filename)."""
                ...

        return await Reader().summarize("guide.txt")
```

This private policy uses explicit Windows units and defaults to a read-only
disposable workspace. It never treats Linux headroom as committed-memory limits,
broker operations as direct path grants, or HTTPS fetches as unrestricted
networking. Worker budgets reset on replacement and new calls.

The session supports sequential calls on one loop. It rejects concurrent/nested
calls, owns brokers and worker teardown, drains cancellation during provisioning
and cleanup, and retains failed cleanup resources for an explicit retry.
Dependency staging remains explicit; installed named requirements and exported
application source files are the supported inputs, not arbitrary source trees.
See the [managed policy contract](windows-sandbox-policy.md#internal-managed-policy-and-lifecycle).

Managed strategies now provide a static Windows policy context after provisioning.
It describes actual workspace and named broker grants, native resource units,
deadlines, recovery and async limitations without advertising Linux permissions
or exposing host paths, snapshot contents or endpoint URLs. Raw caller-owned LPAC
strategies still suppress policy context. This internal block does not enable
public backend selection or capability/doctor reporting.

### Verification: Managed Lifecycle

Completed on 2026-10-01: Windows Python 3.12.13 passed all 70 managed-session
tests and, separately, 161 existing policy/platform/broker/Agent checks. Shared
strategy, configuration and runtime regression passed 2138 tests with 18 skips
using an independent Windows bytecode cache. A fresh Python 3.13.12 five-wheel
installation passed all 143 targeted checks and removed its temporary environment.

See the [verification record](windows-sandbox-policy.md#verification-managed-windows-lifecycle)
for test-fixture corrections, cache handling and remaining acceptance gaps.
That stage did not enable public Windows backend selection, policy context or
capability/doctor reporting. Linux containment was not rerun in that stage.

### Verification: Managed Context And HTTPS

On 2026-10-01, a fresh Windows Python 3.13.12 five-wheel installation passed all
212 targeted checks, including the complete managed-session, file/HTTPS broker,
policy-refusal and platform suites. The installer exited successfully and removed
its temporary environment. Shared Windows regression passed 2305 tests with 19
skips. WSL Linux passed all 69 selected containment tests and, separately, 191
related checks with 20 Windows-only skips.

Native Windows Python 3.12.13 passed 145 fast checks and a separate real staged
application/recovery test. Its complete source-suite runs did not finish: the
180-second test limit was reached during filesystem cleanup and, on a serial
rerun, during dependency copying. The isolated recovery test passed in 145.52
seconds without changing that limit. Full native Python 3.12 source-suite
acceptance therefore remains unverified, not a successful aggregate of partial
runs. See the [verification record](windows-sandbox-policy.md#verification-managed-context-and-https)
for test-fixture corrections, retained caches and remaining limits.

### Verification: Bounded Dependency Copying

On 2026-10-01, native Python 3.12.13 profiling identified per-file opening and
deletion as the dominant staging/cleanup costs. Installed dependency copies now
use four threads with at most 32 pending futures. Validation remains serialized;
all copies finish before application staging, readiness or failed-stage cleanup.
Source checks, isolation grants, standard-library staging and cleanup rules are
unchanged, with no shared cache or hardlinks.

In same-parent diagnostic runs, framework staging fell from 25.34 to 11.13
seconds; setup plus staging and cleanup fell from 84.48 to 69.15 seconds. Both
runs produced 8,318 files and removed their owned runtime roots. The first slow
sample was significantly slower than the repeated serial control, so these
measurements are not a hard latency guarantee.

All 31 staging tests passed. The real staged-application/recovery case passed
separately in 93.34 seconds under the unchanged 180-second limit, compared with
the earlier 145.52-second run. A complete serial run of the seven staging,
managed-session, broker, policy, platform, launcher and workspace suites then
passed all 329 tests in 963.88 seconds, including fixture cleanup. The recovery
case took 85.58 seconds; the slowest test body took 124.41 seconds, below its
existing 180-second limit. This completes the previously interrupted targeted
source-suite gate, not full-project acceptance.

A subsequent Python 3.12.13 five-wheel clean installation passed all 181
selected provenance, staging, managed-session, policy and platform checks in
398.12 seconds. The installer exited successfully and removed its temporary
environment. This initial verification did not rerun Python 3.13, Linux
containment or the full default project suite; the follow-up below supplies
the targeted cross-platform results. See the
[bounded-copy verification record](windows-sandbox-policy.md#verification-bounded-dependency-copying).

### Verification: Cross-Platform Follow-Up

On 2026-10-01, the bounded-copy implementation passed all 69 selected Linux
containment tests in 30.06 seconds on WSL2 Python 3.12.3, with Landlock ABI 3,
seccomp and rlimit available. The separate shared runtime, strategy and
configuration selection passed 2458 tests with 283 skips in 43.75 seconds.
Windows Python 3.12.13 shared regression passed 2138 tests with 18 skips in
33.48 seconds. The isolated Linux test environment and cache were removed.
Windows Python 3.13.12 then completed the same seven native suites: all 329 tests
passed in 956.22 seconds, including cleanup. The slowest test body was 109.14
seconds, below its unchanged 180-second limit. A subsequent Python 3.13.12
five-wheel installation passed all 181 selected checks in 378.61 seconds;
the installer exited successfully and its temporary root was confirmed absent.
The separate source-verification environment remains because its cleanup was
blocked by execution policy; no alternative deletion route was used.
See the [cross-platform verification record](windows-sandbox-policy.md#verification-cross-platform-bounded-copying)
for scope, artifacts and remaining gates. No production code, permission grant
or test deadline was changed in this follow-up. At this stage, full default
project acceptance and storage-contention stability testing had not been run.

### Verification: Bounded I/O Contention

On 2026-10-01, the new opt-in Windows stability suite passed six real scenarios
on Python 3.12.13 in 596.55 seconds and six on Python 3.13.12 in 532.69 seconds.
After test-only typing corrections, one complete loaded 3.12 lifecycle passed
again in 116.97 seconds. Five fast load-generator checks passed on each version.

The suite covers repeated staging, application dependencies, worker timeout/
replacement, callback cancellation, successful subsequent calls, cancellation
during provisioning and repeated cancellation of close. The two-thread load
uses at most 64 KiB of simultaneous payload, caps writes at 512 MiB per case
and refuses less than 2 GiB free space. It runs on the runtime's volume and
does not fill the disk or touch pre-existing data.

Loaded full lifecycles measured 98.22-118.42 seconds with the per-case limit
unchanged at 180 seconds. All 36 tracked workers exited, and all 13 owned
runtime/profile/recovery entries were cleaned. Pressure directories and threads
were also cleaned up. Runtime close still took about 41-49 seconds under load.
These finite samples are not a worst-case latency guarantee; full-project,
disk-full and sustained-saturation acceptance remain separate gates.

The [contention verification record](windows-sandbox-policy.md#verification-bounded-io-contention)
contains individual measurements, resource-check scope and artifact paths.
Only tests and documentation changed. Public Windows selection remains disabled.

## Implemented: Staged Public Interface

`nooa.runtime.sandbox.windows` exposes native policy/grant aliases and a session
wrapper over the existing managed owner. Configuration can be constructed and
validated; session entry always raises `SandboxUnavailable` before creating
native resources. There is no caller release switch or Linux-policy translation.

The API, managed-session, policy and platform source suites passed all 188
checks on Windows Python 3.12.13 and all 188 on 3.13.12. Linux passed 184 and
skipped four native cases. Real native tests use the public class with only its
launch gate replaced in a fixture, including an added OS socket-denial check
and existing broker, recovery, cancellation and cleanup checks. This test
substitution does not enable public launch.

A subsequent Python 3.13.12 clean five-wheel installation passed all 189
selected checks, including installed-module provenance, and removed its
temporary environment. CI includes the new API suite in both Windows source
matrix selections and the wheel runner. This is targeted acceptance, not the
complete installed-workflow gate.

See the [staged interface verification record](windows-sandbox-policy.md#verification-staged-public-windows-interface)
for scope, timings, installed-package checks and remaining release gates.

## Remaining: Public Rollout

The [Windows policy contract](windows-sandbox-policy.md) records all current
`SandboxConfig` field meanings, non-equivalent native mechanisms, refusal tests
and the separate internal Windows policy. It is not an enabled public backend
or a Linux-policy translator.

- The 2026-10-01 native Python 3.12 default suite passed 9509 tests with no
  failures, using an independent Windows bytecode cache. Complete installed-wheel
  acceptance subsequently passed all 586 checks on both Windows Python 3.12.13
  and 3.13.12, with zero failures, errors or skips; see the
  [installed-wheel record](windows-sandbox-policy.md#verification-complete-installed-wheel-acceptance).
  These native tests retain test-only launch-gate substitution, not enabled
  public launch. Resolve the remaining repository and public-path release
  checks before rollout. See the
  [full-project record](windows-sandbox-policy.md#verification-full-project-regression-follow-up).
  The evaluation worker follow-up passed the full pipeline on both 3.12 and
  3.13 (309 tests each) and resolved all 14 scoped worker typing diagnostics.
  The latest coding-export follow-up clears ACP source/test typing, but the
  whole-project Windows-target report still has 198 errors and two warnings.
  See the [coding export record](windows-sandbox-policy.md#verification-typed-coding-exports-and-acp)
  for scoped regressions and the separate Linux-target baseline, and the
  [worker verification record](windows-sandbox-policy.md#verification-evaluation-worker-typing-and-error-cleanup).
- Broaden storage-contention coverage beyond the accepted two-thread bounded
  load. Per-file cleanup remains costly; disk-full, sustained saturation, other
  storage devices and operational disk budgets need separate validation.
- Cover broader application dependency and Agent workflows beyond explicitly
  installed distributions, exported source modules, data types and methods.
- Complete release acceptance before enabling the staged
  [public Windows interface](windows-sandbox-policy.md#staged-public-windows-interface).
  Its aliases and managed-session wrapper preserve explicit native grants,
  platform-specific units and fail-closed rules; they do not translate Linux
  settings. Generation configuration is validated before provisioning, including
  unknown and invalid copied fields. The launch gate remains closed and no
  CodeAct backend, capability probe or doctor support is registered.
- Extend the directory broker beyond bounded listing/read/existing-file replacement
  only with separately enforceable operations, richer HTTP policy and session-wide
  budgets. Direct socket denial does not restrict other privileged callbacks' effects.
- Implement explicit live host read/read-write grants and reject missing or
  unenforceable required rules. Direct worker inputs remain snapshots; live
  directory operations through the parent broker do not grant direct worker access.
- Validate any further async I/O requirements without enabling network access
  just to make event-loop startup work.
- Integrate the opt-in recovery lifecycle into a future public backend, including
  operational policy for uncommitted registrations, disk usage and scheduling.
  Do not infer ownership of legacy or incompletely registered resources.
- Complete release-level public-path acceptance before exposing capability probes,
  `SandboxedExecutor`, public agent-facing context or doctor support. Targeted
  native tests use the staged session with a test-only gate substitution; they
  do not show that installed public launch is enabled. The managed context must
  remain tied to its provisioned Windows policy.

Public configuration remains fail-closed. Documentation and diagnostics must
describe enforced restrictions, not requested-but-missing ones. Linux guard
semantics were not changed by the policy-refusal fixes; the separate Linux
containment regression above passed.
