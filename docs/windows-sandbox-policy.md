# Windows Sandbox Policy Contract

This records the public configuration gap, refusal contract and separate staged
Windows policy, not a backend registration or Linux-policy translator. The public `SandboxedExecutor`
still requires `fork`. Native Windows therefore rejects every public sandbox
request, including `require=False` and configurations with every guard disabled.
An explicit `execution_backend="inprocess"` remains host execution, not isolation.

The internal `_LpacCodeActStrategy` accepts only an unchanged `SandboxConfig()`
as a sentinel meaning "no public policy supplied." **Accepting that sentinel
does not mean LPAC implements the public defaults.** In particular, the internal
runtime defaults to a disposable writable workspace, while public `workspace=None`
does not grant one. A caller can now provision an internal read-only workspace
explicitly; that is still not a translation of the public policy. The low-level
internal strategy disables the public sandbox context block so those unimplemented
semantics are not advertised to the agent. The managed session instead supplies
a separate Windows block derived from its provisioned policy; it never renders
the Linux configuration.

See [Windows implementation status](windows-sandbox.md) for the native mechanisms
and their verification history.

## Staged Public Windows Interface

`nooa.runtime.sandbox.windows` exports `WindowsSandboxPolicy`,
`WindowsSandboxSession`, `FileGrant`, `DirectoryGrant` and `HttpsEndpoint`.
The policy and grants are aliases of the existing validated native types; the
session wraps the existing managed lifecycle. They are deliberately separate
from `SandboxConfig`, not a translation of Linux permissions or resource units.

Policy construction is available for configuration validation on any platform:

```python
from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

policy = WindowsSandboxPolicy(
    workspace_access="read",
    inputs={"seed.txt": b"snapshot"},
    memory_limit_bytes=1024**3,
)
session = WindowsSandboxSession(policy)
```

**Public launch remains disabled.** Entering this session raises
`SandboxUnavailable` before runtime, broker, profile or recovery provisioning,
even on Windows with a valid policy. `session.strategy()` requires successful
entry and is therefore unavailable through the installed public entry. There
is no constructor flag, environment switch or unisolated fallback.

Importing the module does not register a CodeAct backend, add a start method,
change `SandboxedExecutor`, or advertise Windows support through capability
probes or doctor. The names are staged declarations, not a supported execution
path or a completed release gate.

Native acceptance tests replace only the launch gate in a pytest fixture, then
exercise this session class with real LPAC provisioning, grants, workers,
brokers and cleanup. Platform admission and containment remain active. That
test-only substitution is not a caller opt-in and does not establish that the
installed public entry can launch.

## Field Mapping

Every non-default public field is currently refused by the internal strategy.
"Candidate" below means a possible future mapping that still needs an explicit
adapter and acceptance tests; it does not mean that mapping is available today.
Unknown fields are refused by `CodeActConfig`, `SandboxConfig` and `FileRule`,
including Windows-specific units or access flags passed to the Linux schema.

| Public field (default) | Current public meaning | Internal Windows mechanism and required decision |
| --- | --- | --- |
| `filesystem=True` | Landlock default-deny filesystem confinement. | LPAC token and private ACLs, with OS-granted resources and `registryRead`, are not Landlock path rules. `False` does not disable LPAC. Define the Windows filesystem contract explicitly. |
| `workspace=None` | Optional live host directory with read/write access; no workspace means no writable directory grant. | LPAC owns a disposable workspace with an explicit internal `read` or `read_write` grant, defaulting to `read_write`. Neither mode is a live host directory. No public adapter selects a mode from this field. |
| `allow=()` | Direct file/subtree access under named host paths, read or read/write; explicit paths are required. | Staged inputs are snapshots. Exact-file and directory brokers expose bounded parent-side operations; the directory broker sees live host entries but does not grant direct worker filesystem access. Neither implements `FileRule` semantics. |
| `system_paths=True` | Automatically allow read access to interpreter, installed packages and Linux system paths. | LPAC stages an explicit dependency closure and retains required OS access. It neither exposes the host installation nor supports disabling all runtime/system access. |
| `network=False` | Deny worker internet sockets (`AF_INET`/`AF_INET6`); `True` permits them. | Direct socket denial is a candidate for `False`. Fixed-HTTPS broker requests are parent operations and cannot implement `True`. No network capability is added automatically. |
| `max_memory_mb=0` | Extra address-space headroom in MiB above the worker baseline (`RLIMIT_AS`). Zero disables the cap. | Job limits use absolute committed bytes, include startup and apply to process and job. Multiplying MiB by 1024 squared is not a semantic translation. Use an explicit platform-specific contract before exposing it. |
| `max_cpu_seconds=0` | `RLIMIT_CPU` process CPU cap; zero disables it. | Job budgets count lifetime user-mode CPU, exclude kernel time and parent tools, and reset on replacement. Equal numeric seconds are not equivalent limits. |
| `rss_poll_s=0.25` | Documented as an RSS-watchdog interval; the current executor uses it for parent IPC polling, without reading RSS in that loop. | Native committed-memory enforcement is not an RSS watchdog. LPAC inherits a fixed internal polling default but does not map this public field. |
| `timeout_grace_s=2.0` | Extra grace beyond `CodeActConfig.cell_timeout` before parent termination. | Internal LPAC uses its cell deadline without this public grace. Any future adapter must define and test whether/how the grace applies. |
| `broker_timeout_s=300.0` | Separate parent-tool deadline; zero means unbounded. | The internal executor and strategy accept an explicit broker deadline, defaulting to 30 seconds. The managed Windows policy passes its own deadline, including zero. This public field remains unmapped. |
| `start_method="fork"` | Public multiprocessing start method, currently fork only. | Neither the unrestricted spawn experiment nor native LPAC launch is a public start method. Non-fork values are refused even if introduced by unvalidated `model_copy(update=...)`. |
| `recovery="restart_empty"` | Restart a dead worker with empty globals, or refuse subsequent cells with `"disabled"`. | Internal worker/Agent recovery has corresponding modes and is a candidate. It does not roll back workspace files, tool effects or aggregate resource usage. It is separate from orphan-resource cleanup. |
| `require=True` | Reject requested guards that cannot be enforced; existing supported fork paths can explicitly allow degraded guards with `False`. | `False` never authorizes a different public process backend or a Windows host-execution fallback. Both values fail when fork is unavailable. LPAC does not drop containment on request. |
| `context_block=True` | Advertise sandbox constraints to the agent. | Public rendering describes Linux policy and host-object access that LPAC does not provide. Low-level internal LPAC suppresses it. Managed sessions supply a separate Windows block from their provisioned policy; this public field remains unmapped. |

`CodeActConfig.cell_timeout` is outside `SandboxConfig`. The internal strategy
already passes it to LPAC; `None` disables the cell deadline and a finite positive
value sets it. Startup, IPC frames and parent tools have separate deadlines.
This existing internal argument does not make the other public fields supported.
Python restrictions also remain a separate language guard, not OS containment.

## Internal Managed Policy And Lifecycle

`_WindowsSandboxPolicy` is a separate frozen, validated internal policy.
`_WindowsSandboxSession` provisions and owns the corresponding runtime and
brokers as an async context manager. The staged public module above reuses these
types and lifecycle behind a closed launch gate; neither implementation is
selected by `CodeActConfig(execution_backend="sandbox")`.

| Policy | Enforced managed meaning |
| --- | --- |
| `workspace_access="read"` | Private read-only workspace by default. Explicit `"read_write"` enables disposable writes. This intentionally differs from the low-level launcher's compatibility default. |
| `inputs` | Named immutable byte snapshots, not live paths. |
| `files`, `directories` | Named existing-file and bounded live-directory broker grants, with explicit boolean write permission. Native path admission occurs during provisioning. |
| `https` | Exact HTTPS URL/public IP grants. Verified TLS, GET only, no redirects or direct worker network capability. |
| `tools`, `tool_policies` | Exact public Agent method names and optional synchronous argument predicates. These trusted callbacks retain their effects. |
| `memory_limit_bytes=0`, `cpu_time_limit_s=0` | Absolute committed bytes and worker/job lifetime user-mode CPU seconds; zero disables the limit. Startup counts, parent callbacks do not, and a new worker receives a new budget. |
| `cell_timeout_s=10`, `startup_timeout_s=60`, `broker_timeout_s=30`, `frame_timeout_s=5` | Separate deadlines. Cell `None` and broker zero disable their respective deadlines; all other values must be finite and positive. |
| `https_timeout_s=10` | Separate positive finite whole-request deadline. |
| `max_file_bytes=1048576`, `max_directory_entries=512`, `max_response_bytes=1048576` | Per-operation bounds; byte limits cannot exceed 4 MiB and listings cannot exceed 4096 entries. Not cumulative quotas. |
| `recovery="restart_empty"` | Worker replacement with empty globals; `"disabled"` refuses replacement within a call. Neither rolls back files or callback effects. |
| `recovery_directory=None` | Optional ownership enrollment for newly created resources. No implicit recovery scan, legacy adoption, scheduler or deletion of uncommitted registrations. |

Input mappings and tool collections are copied and made read-only. Unknown
fields, invalid units/deadlines, aliased snapshot names, invalid endpoint policy,
ungranted tool predicates and collisions with managed broker names are refused.
Host filesystem checks still happen at native handle admission, not solely at
policy construction. Staged application modules/installed requirements are
explicit session arguments, separate from data and broker grants.

`session.strategy()` creates an internal CodeAct strategy only after successful
provisioning. It exposes async `self.read_file`, `self.write_file`,
`self.list_directory`, `self.read_directory`, `self.write_directory` and
`self.fetch_https` only for present grant categories. Write methods are absent
when there are no writable grants; individual named grants are still checked
on every call. These asynchronous methods require `await`. Granted Agent tools
cannot shadow these reserved broker method names.

Generation options such as retries still use `CodeActConfig`. Public sandbox
settings/backend selection and non-default CodeAct cell timeouts are refused:
the managed cell timeout comes exclusively from the Windows policy. The
session accepts generation `config` at construction and validates it again
before provisioning. `session.strategy()` uses that configuration by default;
an explicit `config` there replaces the generation options, without changing
the Windows permissions or deadlines. The
strategy rechecks its backend, public policy sentinel and cell deadline on
every call. Linux-style context rendering remains disabled. A separate static
Windows context describes the managed policy only while the owner is ready:
workspace mode, snapshot names, named broker permissions, exact tool grants,
native budget units, deadlines, recovery and async-I/O limitations. Read-only
grants never advertise write methods. The block distinguishes live parent-side
operations from direct worker access and per-operation/per-worker limits from
session-wide quotas. It does not claim isolation from every OS-granted resource.

Only logical resource names are rendered, with JSON escaping; host grant paths,
input contents, predicate implementations, endpoint URLs/IPs and recovery-ledger
paths are omitted. This does not redact information the application independently
puts in arguments, tool results or errors. The raw internal strategy still has
no policy context because its caller, not a managed policy, owns provisioning.

The session is one-shot and bound to one event loop. Sequential calls have
separate workers/globals but share the runtime identity and workspace files.
Concurrent or nested calls fail explicitly instead of sharing workers or waiting
on a reentrant lock. Callers must await every Agent call before context exit.
Closing during a call refuses cleanup and stops admission of new calls; await
the existing call and retry `aclose()`.

Provisioning runs in a thread. Cancellation drains that thread, retains any
partially allocated runtime, then cleans up. Teardown retires executors and
parent callbacks before closing brokers and finally deleting the owned
runtime/profile/ledger entry. Repeated cancellation is delayed until cleanup
finishes. Cleanup errors are visible, block new calls and retain ownership for
retry; unlike the general CodeAct teardown path, they are not silently ignored.
Filesystem work is drained, not forcibly interrupted by a wall-clock deadline.
The managed entry does not extend the existing trust boundary to hostile
same-user host code or arbitrary application import/validator behavior.

### Preflight Contract for Future Public Entry

The integration point is the existing owner, not a new policy translator:
`_WindowsSandboxSession(policy, config=codeact_config, ...)`. The staged
`WindowsSandboxSession` inherits this contract and additionally refuses launch.
Both the policy and generation configuration must be supplied before context
entry when refusal must precede runtime, broker, profile or recovery enrollment.
Only the explicit internal Windows policy defines disposable workspace access,
named grants, native resource units, deadlines and recovery.

| Input | Admission rule |
| --- | --- |
| Windows policy | Existing validated `_WindowsSandboxPolicy`; no conversion from `SandboxConfig` or direct live-host `FileRule` grants. |
| Generation options | Valid `CodeActConfig`; default public backend/policy and default CodeAct cell timeout are required as the no-public-policy sentinel. |
| Copied configuration | Strictly revalidate fields and nested file grants; refuse unknown keys and unvalidated coercions before provisioning. |
| Strategy overrides | Check again before creating the strategy; generation overrides cannot replace the owner's permissions or deadlines. |
| Native resources | Host path admission and dependency staging remain provisioning-time checks, with existing cleanup ownership on failure. |

The legacy `session.strategy(config=...)` form remains available after entry,
but cannot retroactively prevent resources already provisioned. Supply complete
generation configuration at session construction for creation-time preflight.
Neither this integration point, the staged interface nor schema validation
enables public Windows selection, advertises native availability or supplies
direct host-path grants.

## Internal Workspace Mapping

`_AppContainerPython(workspace_access="read")` installs a protected read/execute
ACL on the private workspace. `"read_write"` installs the existing modify ACL
and inherited low-integrity label. Only those two exact string values are
accepted, before profile, temporary-tree or recovery-ledger provisioning.
`workspace_access` is creation-time metadata with no setter or live mode switch.
The default remains `"read_write"` for existing internal callers.

Both modes support reads and directory traversal. The read-only mode denies
worker creation, append, truncate, rename, deletion, alternate-stream writes
and subdirectory mutation. Runtime binaries, staged packages and input snapshots
remain read-only in both modes. No shared installation or live host ACL changes.
Worker replacement and new Agent calls retain the caller-owned runtime's grant.
Parent tools remain separately granted trusted capabilities; they can still
write files if authorized, including in a worker-read-only workspace.

Windows may rewrite synthetic `TEMP`, `TMP` and `LOCALAPPDATA` into package
subdirectories beneath the workspace. This does not grant permission to create
them. Applications requiring writable scratch space must choose `"read_write"`;
there is no automatic writable fallback provisioned by NOOA. On Windows,
CPython `tempfile` may retry permission-denied creation as filename collisions,
so failure can be delayed until the existing cell deadline or retry exhaustion.
The acceptance probe bounds only its own retries, not the runtime's stdlib.

This mapping controls the owned workspace, not all OS-granted resources.
`registryRead`, explicit parent brokers and other native restrictions retain
their existing meanings. Public `workspace`, `allow`, `system_paths`, context
rendering and backend selection are unchanged and remain gated.

## Internal Host Directory Broker

`_DirectoryBroker` accepts named `_DirectoryGrant(path, writable=False)` values.
This is an internal parent-side capability, not a direct LPAC grant or a public
`FileRule` translation. Trusted Agent methods may delegate to `list`, `read` and
`write`, then explicitly grant those methods to `_LpacCodeActStrategy`. Creating
a broker alone exposes nothing to the worker.

| Operation | Enforced contract |
| --- | --- |
| `list(name, path="")` | One directory's bounded live entries, each with a name and `file`, `directory` or `reparse` kind. Reparse entries are metadata only, never traversed. |
| `read(name, path)` | Bounded bytes from an existing regular file beneath the named root. Multiple hardlinks observed at open are refused, including for reads. |
| `write(name, path, data)` | Replace an existing regular file only when that directory grant is writable. No creation, deletion, rename, append API, or atomic/durable transaction. |

The permission applies to the whole named subtree, not a per-file allowlist.
Use different roots for read-only inputs and writable outputs. The same path
authorized through a second writable grant is writable through that grant.
Host changes between calls are visible; listings are not atomic snapshots.

Roots must exist on fixed local drives, not be drive roots, and have readable
ancestors. UNC, device, alternate-stream, reserved-name and reparse paths are
refused. Worker paths must be strings with slash-separated plain components:
no absolute paths, backslashes, empty components, `.` or `..`; at most 32
components and 8192 characters. An empty path selects only the root for listing.
Opened objects' final paths are checked to reject aliases.

The broker pins root and ancestor directories for its lifetime, excluding
rename/deletion. Operations pin each descendant directory and open the next
component relative to its handle, with native no-reparse traversal and explicit
leaf reparse-attribute checks. File handles are non-inherited, exclude concurrent
writers and replacement during I/O, and are closed after each operation. No
worker receives these handles. No host DACL, owner or integrity label is changed.

Same-user host code remains trusted. In particular, Windows sharing flags do
**not** stop such code from adding a hardlink after the file's link count was
checked. The broker does not claim protection against that host action. It
rejects links already present at admission; the LPAC worker cannot directly
alter the host directory, and the broker offers no link-creation operation.

The default per-call read/write limit is 1 MiB, with a 4 MiB maximum. Listing
defaults to 512 entries, with a 4096-entry maximum and fixed-size native pages.
Exceeding a limit raises, not a silent partial result. These are not cumulative
session quotas. Disk I/O is serialized in a thread and drained on cancellation;
the owner must await pending operations and `aclose()`. There is no claim that
the cell deadline forcibly interrupts an in-flight filesystem operation.

Public backend selection, context blocks, doctor and `SandboxConfig.allow`
remain unchanged. Brokering live directory operations is not mounting that
directory into the worker or implementing arbitrary direct filesystem access.

## Refusal Before Effects

The public execution path must reject an unsupported start method before
selecting a multiprocessing context or creating the requested workspace.
Pydantic schema validation rejects `spawn`, `forkserver` and `lpac`, but
`model_copy(update=...)` skips validation. The executor therefore checks its
fork-only invariant again. `require=False` cannot bypass that check.
The public CodeAct entry also rechecks `execution_backend`: unvalidated values
such as `"spawn"` or `"lpac"` must raise `SandboxUnavailable`, not enter the
ordinary in-process branch. Valid `"inprocess"` remains explicitly unisolated;
valid `"sandbox"` still requires a supported fork-based executor.

Public CodeAct calls strictly revalidate copied configurations before execution
setup, for both backend values. The direct executor validates its sandbox policy
before resolving paths or inspecting the agent's wire types. Nested sandbox and
file-rule instances are revalidated too. Unknown keys inserted by `model_copy()`
must not disappear through schema serialization. Invalid requests raise
`SandboxUnavailable` without including configuration values in that exception.
The internal LPAC constructor applies the same schema check before accepting its
default-policy sentinel. `require=False` does not bypass configuration validity.

For a real Agent using the public sandbox without fork, rejection must precede
prefill, pre-ellipsis cells, parent tool calls and model requests. No source tree,
profile, job or fallback worker is provisioned for this unavailable backend.
This does not isolate arbitrary trusted Python performed while constructing the
Agent or configuration; those remain host code.

The internal LPAC strategy rejects non-default public policy before consulting
its caller-owned runtime, even when native memory/CPU limits were also supplied.
It rechecks backend/policy changes before executing a call. Supplying
`context_block=False` at construction is still a non-default public policy,
distinct from the strategy's own private suppression after accepting the sentinel.

These are configuration invariants, not protection against malicious same-user
host code modifying the framework.

## Acceptance Gates

Before exposing a Windows backend:

1. Define disposable-workspace and live-host-grant semantics separately.
   Reject missing or unenforceable required grants without changing unrelated ACLs.
2. Introduce explicit resource units and lifecycle accounting; do not relabel
   Linux headroom or CPU limits as Windows Job limits.
3. Map supported deadlines and recovery modes explicitly. Retain refusal for
   every unmapped field, including non-default combinations and reconfiguration.
4. Derive agent context and capability/doctor reporting from the enforced policy.
   Merely detecting an AppContainer API is not proof of public policy support.
5. Own staging, executors and profile cleanup through the complete public call
   lifecycle. Keep uncommitted/legacy resources outside automatic adoption.
6. Exercise paired allowed/denied file and network operations, process ownership,
   cancellation and recovery through real Agent calls, installed Windows Python
   versions, and the existing Linux containment suite.

## Executable Contract

- `test_lpac_policy.py` covers all 14 current public fields, construction with
  and without native budgets, reconfiguration, schema refusal and suppressed
  public context. A schema-field coverage assertion requires this contract to
  be revisited whenever a field is added or removed.
- `test_platform_support.py` covers no-fork refusal, unvalidated non-fork
  selection, unknown public CodeAct backends, no workspace/context allocation
  on refusal, actual Agent calls without model/tool/cell effects, and a simulated
  supported-fork control.
- Both suites are platform-independent policy tests: they simulate start-method
  availability, do not launch native workers and are not marked fork-only.
  They run in regular unit tests, Windows CI and installed-wheel acceptance.
  They complement, rather than replace, the native containment suites.
- `test_windows_session.py` also checks the managed context's default and mixed
  grants, explicit units/deadlines, escaping, sensitive-value omission, static
  placement, ready-state admission and delivery in real Agent model messages.
  A field-coverage assertion requires every new Windows policy field to be
  considered for disclosure.
- `test_lpac_brokers.py` exercises managed Agent HTTPS calls with a local verified
  TLS server, test-only public-IP routing and a test CA. It checks native socket
  denial, ungranted names/arguments, redirects, response bounds, certificate
  hostname verification, both deadline sources, cancellation and subsequent
  calls. Only the test's Python socket-import restriction is removed to reach
  the native denial; no production network grant or TLS rule is relaxed.

```powershell
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_policy.py tests/runtime/sandbox/test_platform_support.py
uv run --no-sync python scripts/smoke_install.py --python 3.13 --test-file test_lpac_policy.py --test-file test_platform_support.py
```

The second command is targeted acceptance plus mandatory installed-wheel
provenance, not the entire default acceptance suite.

## Verification: Policy Refusal

Verified on 2026-09-30:

- Regression tests first reproduced two admission gaps: an unvalidated copied
  non-fork start method could select a worker with degraded guards, and an
  unknown copied CodeAct backend could enter in-process execution. Both now
  raise `SandboxUnavailable` before worker/workspace creation or Agent effects.
- Native Windows Python 3.12.13: all 72 policy/platform checks passed. The broader
  shared execution, strategy and configuration regression passed 1102 tests.
  Separate real spawn and LPAC Agent positive controls passed both tests,
  preserving persistent cells, parent brokering and typed completion.
- Native Windows Python 3.12.13: a fresh five-wheel targeted installation
  passed all 73 checks in 45.72 seconds, covering both complete policy/platform
  suites and wheel provenance. This rerun supplies the result because the
  earlier installation process's final output was unavailable; that earlier
  process is not counted as a successful run. The rerun installer exited with
  code zero and removed its temporary installation; the earlier installation
  directory was also confirmed absent.
- Native Windows Python 3.13.12: five-wheel targeted installation passed all
  103 checks, covering wheel provenance and the complete policy, platform and
  LPAC CodeAct suites. The installer exited with code zero and removed its
  temporary installation. This is not the full default acceptance suite.
- Ubuntu on WSL2, Python 3.12.3: capability probing reported Landlock ABI 3,
  seccomp and rlimit. The regular sandbox-directory selection passed 230 tests
  with 182 Windows-only skips; the separately selected `-m sandbox` containment
  run passed all 69 fork tests. Its two collection skips were Windows-only
  recovery and Job Object modules, not skipped fork-containment tests.
- Scoped Ruff lint, formatting, explicit text encoding, Pyright, SPDX and YAML
  checks passed. Resource, unraisable-exception and thread warnings were treated
  as failures in the test runs.

The Linux runs used a separate temporary environment and bytecode cache, both
removed afterward. The project's `.venv` remains native Windows Python 3.12.13.
The legacy interrupted `nooa-install-vkeac15m` installation was not adopted or
cleaned. No public Windows backend, permission translator or new native grant
was enabled.

## Verification: Private Workspace Permissions

Verified on 2026-09-30:

- Native Windows Python 3.12.13: all 159 tests passed in 295.04 seconds across
  the complete workspace, stdlib launcher, policy and platform suites. The
  workspace suite contributes 43 checks, including real worker replacement and
  Agent calls in both modes, temporary-directory handling and recovery cleanup.
- Shared execution, strategy, configuration and Bash lifecycle regression:
  all 1027 tests passed in 29.70 seconds.
- Native Windows Python 3.13.12: a fresh five-wheel targeted installation passed
  all 116 tests in 332.19 seconds, covering wheel provenance and the complete
  workspace, policy and platform suites. The installer exited with code zero
  and its temporary directory was confirmed absent.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX, YAML and whitespace
  checks passed. Resource, unraisable-exception and thread warnings were failures.

Initial probes exposed two test assumptions: Windows expands the supplied
temporary-directory environment paths below the workspace, and CPython may
retry ACL-denied temporary-file creation rather than immediately propagating
`PermissionError`. The final tests account for path expansion and bound their
own retries; no ACL or production stdlib behavior was relaxed.

Both the unsuccessful initial targeted installation and the final successful
installation cleaned their temporary environments. The project's `.venv`
remains native Windows Python 3.12.13. Legacy resources were not adopted or
removed. Python 3.12 clean-wheel installation, the full default acceptance suite
and Linux containment were not rerun in this stage. The earlier Linux results
above are not a new run. Public Windows backend selection remains unavailable.

## Verification: Host Directory Broker

Verified on 2026-09-30:

- Native Windows Python 3.12.13: all 116 directory/policy/platform tests passed
  in 116.04 seconds, including all 44 new directory checks. The real Agent
  test uses separate read-only source and writable output roots while direct
  host-file access remains denied.
- Existing exact-file/HTTPS broker and resource-recovery suites: all 99 tests
  passed in 217.96 seconds after extending the shared native file-open helper.
  These and the 116 tests above are separate successful runs, not one 215-test run.
- Shared execution, strategy and configuration regression: all 1018 tests passed
  in 17.64 seconds.
- Native Windows Python 3.13.12: a fresh five-wheel targeted installation passed
  all 216 tests in 438.17 seconds, including wheel provenance and the complete
  directory, exact-file/HTTPS, recovery, policy and platform suites. The installer
  exited with code zero and its temporary directory was confirmed absent.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX and YAML checks passed.
  Resource, unraisable-exception and thread warnings were treated as failures.

The initial native tests exposed that `FILE_OPEN_REPARSE_POINT` can open a leaf
junction object without following it. Directory admission now checks the handle's
attributes as well as refusing traversal and verifying its final path. Subsequent
root-junction and descendant-swap checks passed. A separate probe showed that
sharing flags do not block same-user host hardlink creation; this is recorded as
a trust-boundary limitation, not claimed as a fixed or enforced protection.

The project's `.venv` remains native Windows Python 3.12.13; legacy resources
were not adopted or removed. Python 3.12 clean-wheel installation, full-project
tests and Linux containment were not rerun in this stage. Public Windows sandbox
selection remains unavailable; these results validate the internal broker only.

## Verification: Managed Windows Lifecycle

Verified across 2026-09-30 and 2026-10-01:

- Native Windows Python 3.12.13: the final complete managed-session suite passed
  all 70 tests in 426.31 seconds. Four real Agent tests cover brokered file and
  directory access, direct-access denial, callback timeout/cancellation, fresh
  calls, policy refusal, typed dependency staging, worker replacement with
  retained workspace files and enrolled-resource cleanup.
- Existing policy, platform, exact-file/HTTPS broker and LPAC CodeAct regression:
  all 161 tests passed in 344.05 seconds.
- Shared strategy, configuration and runtime regression: 2138 passed and 18
  skipped in 34.03 seconds. This run used an independent Windows bytecode cache.
- Native Windows Python 3.13.12: a fresh five-wheel installation passed all 143
  targeted checks, including wheel provenance and the complete managed-session,
  public-policy refusal and platform suites. The installer exited with code zero
  and its temporary environment was confirmed absent afterward. The two earlier
  unsuccessful installations also removed their temporary environments.
- Scoped Ruff lint/format/explicit-encoding, Pyright, SPDX and YAML checks passed.
  Resource, unraisable-exception and thread warnings were treated as failures.

The new tests initially needed corrections to await asynchronous broker methods,
avoid forbidden `globals()` introspection, and declare application data types at
module level as required by Agent annotation resolution. No containment rule was
relaxed. The broader shared run initially had four failures from pytest bytecode
retaining WSL source paths; all four passed with a fresh Windows cache, followed
by the complete successful shared run above. The verification cache under
`logs/managed-windows-cache` remains because the execution environment refused
its cleanup command; no alternative deletion route was used.

Managed HTTPS construction and dispatch are checked at the ownership boundary;
the separate broker suite supplies the real verified-TLS and network-denial
controls. This is not a claim of a new complete managed HTTPS Agent acceptance
path. At that stage, public policy/API selection, enforced-policy context and
capability/doctor reporting remained gated. Linux containment, Python 3.12 clean-wheel
installation and full-project acceptance were not rerun in that stage. Legacy resources were
not adopted or removed.

## Verification: Managed Context And HTTPS

Verified on 2026-10-01:

- Native Windows Python 3.12.13: all 145 fast managed-policy/context/lifecycle and
  public-policy/platform checks passed in 4.81 seconds, excluding the four real
  managed Agent tests. The previously timed-out staged-application/recovery
  test passed separately in 145.52 seconds with its existing 180-second limit.
  These are completed targeted runs, not a successful complete native suite.
- Native Windows Python 3.13.12: a fresh five-wheel targeted installation passed
  all 212 checks in 919.46 seconds, including wheel provenance and the complete
  managed-session, file/HTTPS broker, policy-refusal and platform suites. The
  installer exited with code zero and its temporary environment was confirmed
  absent afterward.
- Shared strategy, configuration and runtime regression: 2305 passed and 19
  skipped in 33.09 seconds, using an independent Windows bytecode cache.
- Ubuntu on WSL2, Python 3.12.3: the four policy/platform/session/broker suites
  passed 191 tests with 20 Windows-only skips in 3.78 seconds. The separate
  `-m sandbox` containment run passed all 69 selected tests in 29.17 seconds;
  its two collection skips were Windows-only modules.
- Scoped Ruff lint/format/explicit-encoding, Pyright and SPDX checks passed.
  Resource, unraisable-exception and thread warnings were treated as failures.

The initial native HTTPS Agent test encountered the Python `__import__` guard
before reaching socket creation. Its probe now uses a normal import with only
the test configuration's socket import block removed, so the expected denial
comes from Windows. Production Python restrictions, LPAC grants and TLS
verification were not relaxed.

Two complete Windows Python 3.12 source-suite attempts did not finish. The first,
concurrent with installed-wheel acceptance, hit the existing 180-second test
timeout while `_AppContainerPython.close()` was deleting staged files. A serial
rerun passed that case and all file/HTTPS broker tests, but its final staged
application test timed out in `_stage_packages()` while copying dependencies,
before worker startup. The stacks do not establish a worker/broker deadlock, and
the serial failure means concurrency alone does not explain the issue. The system
drive still had about 40 GB free. Neither interrupted attempt is counted as a
successful complete suite. No test timeout, production staging rule or containment
grant was relaxed, and no interrupted staging-tree ownership was inferred for
manual cleanup. The isolated staged-application pass still leaves little margin
under the test deadline; the native Python 3.12 full-suite timing issue remains
unresolved and must not be treated as accepted for public rollout.

The Windows verification cache at `logs/windows-context-cache` remains because
the execution environment refused its cleanup command; no alternative deletion
route was used. WSL used a separate `/tmp/nooa-windows-context-cache-20261001`
bytecode cache and an isolated uv environment, not the native project `.venv`.

Public Windows policy/API selection and capability/doctor reporting remain
gated. Python 3.12 clean-wheel installation and full-project/default acceptance
were not rerun in this stage. Legacy resources were not adopted or removed.

## Verification: Bounded Dependency Copying

On 2026-10-01, profiling the Python 3.12.13 source environment localized the
slowdown to per-file opening and deletion, not worker startup or a broker
deadlock. The initial sample spent 93.39 seconds staging dependencies and 57.65
seconds cleaning up. A subsequent serial control was substantially faster,
so the initial sample alone is not a reliable estimate of the optimization's
benefit.

The production change uses four copy threads and at most 32 pending copy
futures per staging call. Dependency closure, manifest, source-path, reparse,
name and overlap checks remain serialized on the caller. Executor shutdown
waits for every submitted copy on success and failure before control returns
to application admission, readiness or owned-runtime cleanup. The editable
NOOA fallback, standard-library copying, permission grants and cleanup logic
remain unchanged. There are no shared caches, hardlinks or background deletions.

The diagnostic harness is retained under `logs/profile_windows_staging.py`;
its explicit recovery ledgers, JSON and profiler outputs stay under
`logs/windows-staging-profile`. All following measurements used the same
E: parent directory and produced 8,318 runtime files:

| Run | Runtime setup | Framework staging | Cleanup | Total |
| --- | ---: | ---: | ---: | ---: |
| Current serial control | 1.39 s | 25.34 s | 57.75 s | 84.48 s |
| Diagnostic four-thread experiment | 0.90 s | 11.23 s | 58.60 s | 70.73 s |
| Production bounded copying | 1.38 s | 11.13 s | 56.64 s | 69.15 s |

Each run confirmed that its owned runtime root was removed. The initial
diagnostic needed long-path-safe file counting, and its first concurrent
attempt needed a local argument-name correction; neither failed diagnostic is
counted as a completed comparison. The diagnostic's 90-second performance
budget is checked after cleanup and is not a production or pytest timeout.

The staging suite passed all 31 tests, including six new concurrency checks.
They cover the four-worker and bounded-queue contract, serialized validation,
complete file contents, application/shim/readiness ordering, copy failures
while draining or filling the queue, and waiting for pending copies after
source or overlap rejection. The parallel-copy and copy-failure regressions were first
observed failing against the serial implementation.

The previously slow real staged-application/recovery test passed separately in
93.34 seconds (92.63 seconds in the test body), using the normal C: pytest
temporary location and its unchanged 180-second timeout.

A complete serial native source run then passed all 329 tests in 963.88 seconds:
the entire staging, managed-session, file/HTTPS broker, policy-refusal, platform,
AppContainer launcher and workspace suites. The staged-application/recovery
case took 85.58 seconds in this run; the slowest test body took 124.41 seconds,
below its existing 180-second limit. The process exited successfully after
fixture cleanup. Results are retained in `logs/windows-staging-source.xml`.
Resource, unraisable-exception and unhandled-thread warnings were errors.
Scoped Ruff lint/format/explicit-encoding, Pyright and SPDX checks also passed.

A fresh Python 3.12.13 five-wheel installation then passed all 181 selected
checks in 398.12 seconds: wheel provenance and the complete staging,
managed-session, policy-refusal and platform suites. It ran outside the
checkout in a path containing Chinese characters and spaces. The installer
exited with code zero and its temporary root was confirmed absent afterward.
Source and installed-wheel native acceptance were run sequentially.

This completes the previously interrupted managed-context/HTTPS source-suite
run and adds staging, launcher and workspace regression; it is not full-project
acceptance. Per-file deletion still dominates total provisioning/cleanup cost,
and filesystem variability remains an operational limitation. Public Windows
selection stays disabled.

The initial bounded-copy verification did not rerun Python 3.13, Linux
containment or the full default project suite; the follow-up below supplies
the targeted cross-platform results. Diagnostic artifacts and the independent
`logs/windows-staging-cache` remain under `logs`; legacy or unidentified
interrupted resources were not adopted or removed.

## Verification: Cross-Platform Bounded Copying

Follow-up verification on 2026-10-01 uses the bounded-copy implementation above,
without changing production code, permission grants or test deadlines.

- Ubuntu on WSL2, Python 3.12.3: capability probing reports Landlock ABI 3,
  seccomp and rlimit. The separate `tests/runtime/sandbox -m sandbox` run passed
  all 69 selected containment tests in 30.06 seconds. Its two collection skips
  were Windows-only modules, not skipped containment tests.
- The Linux `tests/runtime tests/strategies tests/config` selection passed
  2458 tests with 283 skips and 69 deselections in 43.75 seconds. The deselected
  containment tests are covered by the preceding run.
- Native Windows Python 3.12.13: shared runtime, strategy and configuration
  regression passed 2138 tests with 18 skips in 33.48 seconds. This run excluded
  `tests/runtime/sandbox`, which has separate native and containment selections.
- Native Windows Python 3.13.12: the complete seven-suite staging, managed-session,
  file/HTTPS broker, policy-refusal, platform, AppContainer launcher and workspace
  selection passed all 329 tests in 956.22 seconds, including fixture cleanup.
  The slowest test body took 109.14 seconds; the staged-application/recovery case
  took 84.47 seconds. The existing 180-second limits were unchanged.
- Native Windows Python 3.13.12: a fresh five-wheel installation passed all 181
  selected provenance, staging, managed-session, policy-refusal and platform
  tests in 378.61 seconds. The slowest test body took 101.89 seconds, and the
  staged-application/recovery case took 84.03 seconds. The installer exited
  successfully and its temporary root was confirmed absent. This is targeted
  installed-package acceptance, not the full default smoke-install suite.

All completed runs treated resource, unraisable-exception and unhandled-thread
warnings as errors. Existing deprecation warnings remain visible, including the
Linux multi-threaded `fork()` warning. Results are retained in
`logs/copy-verification-linux-containment.xml` and
`logs/copy-verification-linux-shared.xml`, plus
`logs/copy-verification-windows-shared.xml` and
`logs/copy-verification-py313-source.xml`. Installed-wheel results and the build/
install transcript are retained in `logs/copy-verification-py313-wheel.xml`
and `logs/copy-verification-py313-wheel.log`.

Linux used an independent uv environment and bytecode cache under
`/home/qinguang/.cache/nooa-verify-20261001-linux-*`; both were removed and
confirmed absent after testing. The initial `/tmp` attempt failed to import
pytest before test collection and is not counted as a test run; its environment
and cache were also removed. The native project `.venv` remains Windows Python
3.12.13. Windows shared tests used `logs/copy-verification-windows-shared-cache`
to avoid reusing pytest bytecode with Linux source paths.

The Python 3.13 source run used a separate uv environment with the frozen
workspace lockfile and `logs/copy-verification-py313-cache`. All seven native
suites ran in one pytest process. Clean-wheel acceptance started only after it
exited successfully. Some brief Linux/shared checks overlapped the source run;
these timings are acceptance observations, not an isolated benchmark or a
storage-contention stability test.

The clean-wheel run used a repository-external path containing Chinese
characters and spaces. `PYTEST_ADDOPTS` added only the XML output path and
duration reporting; isolation flags, warning filters and deadlines were not
changed. Its owned `nooa-install-w9isgyu5` root was removed by the installer.
The separate source-verification environment remains at
`C:\Users\QinGu\AppData\Local\Temp\nooa-verify-20261001-py313-copy` because the
execution environment refused the checked PowerShell cleanup command. No
alternative deletion route was attempted. Windows verification caches remain
under `logs`; no legacy or unidentified resources were adopted or removed.

At this stage, full default project acceptance and storage-contention stability
testing had not been run. The bounded follow-up below supplies the first
contention results. Public Windows policy/API selection and capability/doctor
reporting remain disabled.

## Verification: Bounded I/O Contention

The 2026-10-01 follow-up adds `test_windows_stability.py`, an opt-in native
`stress` suite. Production staging, cleanup, permission grants and public
backend selection are unchanged. See the [test instructions](../tests/README.md)
for the command and machine-readable JUnit `stability` properties.

Each supported Python version gets an idle control, three complete lifecycle
repetitions with I/O load, and idle/loaded provisioning-cancellation cases.
Complete lifecycles stage PyYAML and an explicit application module, enforce
direct host-file denial, write through a named broker, replace a timed-out
worker while retaining workspace files, cancel a parent callback, and make a
successful subsequent call. Two cancellation requests at the runtime-close
entry must wait for actual cleanup. Provisioning cancellation is injected
after at least 32 real dependency-copy futures have drained. The original
operations still run; no copy, worker launch or native cleanup is faked.

The load creates, fsyncs, reads and unlinks 32 KiB files on the runtime volume
using two threads with a 20 ms pause between cycles. It permits at most 64 KiB
of simultaneous payload, caps cumulative writes at 512 MiB per case, refuses
less than 2 GiB of free space, and only removes its newly created directory.
Each case has a 180-second timeout including fixtures. Generator failures,
resource warnings, unraisable exceptions and unhandled-thread warnings fail
the run; load threads must join before reporting completion.

The resource checks pin worker process objects before retirement and verify
kernel-signaled exit, closed Job Objects and pipe endpoints, stopped stderr
threads, deleted profiles and removal of the runtime plus its recovery entry.
The caller-owned empty recovery directory is not deleted by the runtime.
Process-wide handle counts are recorded but are not used as a zero-leak claim:
first-use initialization and shared pools change them across tests. These
results cover the explicitly tracked native resources, not every host cache.

Initial Python 3.12.13 acceptance passed all six scenarios in 596.55 seconds:

| Scenario | Lifecycle elapsed | Payload written | Cleanup verified |
| --- | ---: | ---: | --- |
| Idle full lifecycle | 129.92 s | 0 MiB | Yes |
| Loaded lifecycle 1 | 118.42 s | 209.09 MiB | Yes |
| Loaded lifecycle 2 | 109.38 s | 201.28 MiB | Yes |
| Loaded lifecycle 3 | 105.57 s | 196.09 MiB | Yes |
| Idle provisioning cancellation | 64.62 s | 0 MiB | Yes |
| Loaded provisioning cancellation | 65.98 s | 123.16 MiB | Yes |

All loaded cases performed real I/O throughout the long provisioning/cleanup
phases. Five fast generator checks also passed, including existing-directory
refusal, low-space refusal, worker-error propagation and the write-budget cap.
The initial source run is retained in `logs/windows-stability-py312.xml`.
After test-only response construction/type-narrowing corrections, a complete
loaded lifecycle rerun passed in 116.97 seconds (115.79 seconds for the measured
lifecycle), with 209.28 MiB written and every tracked resource released.
That result is in `logs/windows-stability-py312-final.xml`. All five fast
generator checks passed on both Python 3.12 and 3.13. Scoped Ruff lint/format,
explicit-encoding, Pyright and SPDX checks passed.

Python 3.13.12 then passed all six scenarios with the final test code in
532.69 seconds, with results retained in `logs/windows-stability-py313.xml`:

| Scenario | Lifecycle elapsed | Payload written | Cleanup verified |
| --- | ---: | ---: | --- |
| Idle full lifecycle | 107.72 s | 0 MiB | Yes |
| Loaded lifecycle 1 | 98.80 s | 185.34 MiB | Yes |
| Loaded lifecycle 2 | 98.78 s | 184.72 MiB | Yes |
| Loaded lifecycle 3 | 98.22 s | 183.72 MiB | Yes |
| Idle provisioning cancellation | 62.72 s | 0 MiB | Yes |
| Loaded provisioning cancellation | 64.02 s | 125.44 MiB | Yes |

The three XML reports contain no failures or errors and every case records
successful owned-resource cleanup. Across both versions and the final 3.12
rerun, 36 worker exits and 13 runtime/profile/recovery-entry cleanups were
checked. All test-owned pressure directories were removed and their I/O threads
joined. Loaded full lifecycles measured 98.22-118.42 seconds under the unchanged
180-second per-case test limit; loaded runtime-close phases took 41.26-49.08
seconds and remain a substantial part of total elapsed time.

The versions ran sequentially. Python 3.13 reused the prior independent source
verification environment, which remains at the previously documented path; its
blocked deletion was not retried. The project `.venv` was not replaced. Reports
and independent `logs/stability-py312-cache` / `logs/stability-py313-cache`
directories remain for diagnosis. No legacy resource ownership was inferred.

These are finite, modest contention samples on one Windows host. Faster warm
runs than the idle control do not show that contention has no cost. Disk-full,
power-loss, other storage devices, sustained saturation and full-project
acceptance remain outside this gate. Public Windows rollout remains disabled.

## Verification: Full-Project Regression Follow-Up

On 2026-10-01, native Windows Python 3.12.13 verification was expanded beyond
the bounded-I/O gate. The first completed default run reported 9448 passed,
61 failed, 37 skipped, three xfailed and 331 deselected in 2657.45 seconds.
Its report is `logs/full-regression-20261001-py312.xml`.
An earlier interrupted invocation did not produce a report and is not counted.
The first completed invocation's output is
`logs/full-regression-20261001-py312.log`.

The failures shared a source-introspection symptom: Windows tracebacks pointed
at `/mnt/e/...` paths from WSL assertion-rewritten bytecode. Two isolated
ellipsis/traceback tests failed with the shared cache and passed when only
`-X pycache_prefix=logs/full-regression-py312-cache` changed. All affected files
then passed (730 tests in 25.94 seconds), including all 61 original failures;
see `logs/full-regression-20261001-source-retest.xml`. No production
source-introspection logic was changed and no shared cache was deleted.
The test README now documents platform/version-specific cache prefixes.

A full default rerun with that independent Windows cache completed with 9509
passed, 37 skipped, three xfailed and 331 deselected in 3025.65 seconds.
Its log and XML report use the
`logs/full-regression-20261001-py312-isolated` prefix. The XML records 9549
cases, zero failures and zero errors; its 40 skipped entries include the three
expected failures. This completed run, not the earlier targeted rerun, supplies
the default-suite acceptance evidence. It completed before the evaluation
memory-monitor follow-up below.

Completed supplemental checks:

- Memory, embedded runtime and embedded tools: 459 passed, 14 skipped and one
  deselected in 20.90 seconds. Report:
  `logs/full-regression-20261001-extra-supported.xml`.
- Evaluation pipeline, explicitly excluding `test_memory_monitor.py`: 271
  passed and 45 skipped in 94.48 seconds. Report:
  `logs/full-regression-20261001-eval-final.xml`.
  Four initial failures came from stale test construction: the evaluator now
  resolves output paths, and `ExecutionTurn` has no `status` constructor field.
  Only those test expectations/arguments were updated. Both affected files
  also passed a separate 54-test rerun.
- Repository Ruff lint, formatting, explicit text encoding and SPDX checks
  passed. Formatting normalized two lines in `_bash_session.py` and the
  existing staging diagnostic script; no runtime behavior was changed.
- Scoped Pyright passed for Windows session, policy, context, Bash session and
  the bounded-I/O stability test.

The follow-up identified two additional repository checks:

- Collecting the evaluation pipeline without exclusions initially failed because
  `_memory_monitor.py` imported the Unix-only `resource` module on Windows.
  The initial collection failure is retained in
  `logs/full-regression-20261001-extra.xml`; the exclusion in the supplemental
  run was an explicit limitation, not a fix or a Windows memory-limit guarantee.
  The separate follow-up below addresses this collection failure.
- Whole-project Pyright reports 323 errors and two warnings across 313 files,
  including event-model typing and Unix-only APIs. The machine-readable report
  is `logs/full-regression-20261001-pyright.json`. No historical comparison was
  performed, so these diagnostics are not classified as newly introduced or
  pre-existing.

Clean-wheel acceptance and Linux/macOS execution have not been rerun in this
follow-up. Public Windows rollout remains disabled.

## Verification: Evaluation Worker Memory Monitoring

The Windows import failure was reproduced independently and retained in
`logs/memory-monitor-20261001-before.xml`. The evaluation monitor now reads
current working-set bytes through `K32GetProcessMemoryInfo`, reports native
API failures, and does not import the Unix-only `resource` module on Windows.
Windows `set_hard_limit()` explicitly returns `False`, and clearing that
unavailable OS limit is a no-op. Linux RSS and resource-limit behavior are
unchanged; Unix RSS fallback units are also covered by simulated-platform tests.

This is resident-memory polling, not a Job Object or committed-memory cap.
It does not bound child processes or allocations between samples. See the
[evaluation instructions](../util/eval_pipeline/README.md#worker-memory-limits)
for the distinction from public sandbox enforcement.

The new real worker test exposed a second failure after task completion:
`tracemalloc.clear_traces()` stalled before the JSON result was delivered.
The original 30-second test timeout and an independent 120-second diagnostic
both reproduced the stall. Repeated thread snapshots remained at that cleanup
call, with the executor thread idle. Changing only that clear operation to a
no-op returned a valid result in 6.66 seconds. Since memory-capped workers
already exit after one task, the redundant reset was removed; production
tracking depth, monitor thresholds and the 30-second worker test limit were
not relaxed. The diagnostic harness and original stacks remain under
`logs/profile_memory_worker.py` and `logs/memory-worker-profile-default.log`.

The soft-limit test now supplies a deterministic RSS between its soft and
hard thresholds, preventing it from terminating pytest itself. The hard-kill
test runs in an isolated child and verifies exit 137, result identity,
`MemoryError`, and the diagnostic file. Native tests also check current
working set rather than peak working set, Windows API error propagation,
and a real 32 MiB resident allocation.

Final source verification on 2026-10-01 used the completed implementation and
the final resident-allocation test:

| Runtime | Scope | Passed | Skipped | Elapsed |
| --- | --- | ---: | ---: | ---: |
| Python 3.12.13 | Entire evaluation pipeline | 291 | 47 | 125.06 s |
| Python 3.13.12 | Memory monitor and subprocess worker files | 32 | 2 | 126.65 s |

The reports are `logs/eval-memory-final-20261001-py312.xml` and
`logs/eval-memory-final-20261001-py313.xml`; both record zero failures and zero
errors. The full pipeline no longer excludes the memory-monitor file.
Its 47 skips comprise 45 opt-in model tests and two Unix/Linux-only resource
limit tests; the 3.13 run has only those two platform skips. Both runtimes
passed native working-set allocation, diagnostic hard-kill and real
memory-capped worker execution. The final allocation test loads the standalone
memory module with `runpy`, avoiding unrelated package initialization within
its unchanged 15-second deadline; real worker tests still initialize the full
package with the unchanged 30-second deadline.

Repository Ruff lint, formatting (1069 files) and explicit text encoding checks
passed, as did SPDX checks for the four relevant Python files. At that point,
Pyright reported zero errors for `_memory_monitor.py`. Extending that check to
`subprocess_worker.py` reported 14 errors concerning possibly unbound results,
an optional span reason and the generic monitor annotation; the report is
`logs/eval-memory-final-20261001-pyright.json`. This scoped check is separate
from the earlier whole-project report, whose configured scope excludes `util`;
neither report is represented as a clean type-check pass.

Python 3.13 reused the independent source environment documented above without
replacing the project `.venv`. These final checks do not repeat the full default
suite, clean-wheel acceptance or execution on Linux/macOS. Public Windows
rollout remains disabled.

## Verification: Evaluation Worker Typing and Error Cleanup

The scoped worker check above was reproduced on 2026-10-01 with all 14
diagnostics. The worker now explicitly initializes its optional execution result,
checks that a successful execution produced a result before scoring, and
narrows the span-close reason independently of exception truthiness. A
`TYPE_CHECKING` import gives the memory annotation helper its concrete monitor
type without loading the memory module when monitoring is disabled.

The new regression tests first exposed two trace-cleanup failures: an empty
execution error string skipped `end_active_spans()`, and a raised exception
whose boolean value was false selected an unassigned result instead of the
exception message. Both now close active spans before shutting down tracing.
The focused tests went from 2 failed / 16 passed to 18 passed. They also cover
successful execution, ordinary raised errors, non-fatal tracing cleanup
failures, skipped scoring on raised errors, memory annotations with and without
diagnostics, preservation of existing errors, and lazy monitor imports in a
fresh process.

| Runtime | Scope | Passed | Skipped | Elapsed |
| --- | --- | ---: | ---: | ---: |
| Python 3.12.13 | Entire evaluation pipeline | 309 | 47 | 116.10 s |
| Python 3.13.12 | Entire evaluation pipeline | 309 | 47 | 163.95 s |

The reports are `logs/eval-worker-typing-20261001-py312.xml` and
`logs/eval-worker-typing-20261001-py313.xml`, with zero failures and zero errors.
Each run skipped the same 45 opt-in model tests and two Unix/Linux-only resource
limit tests. Existing real-worker, memory-monitor and timeout-span export tests
ran with their original deadlines. Each run reported four warnings concerning
the unregistered `slow` mark, test-class collection and deprecated websocket APIs.

Scoped Pyright now reports zero errors and zero warnings for both
`_memory_monitor.py` and `subprocess_worker.py`, including a separate check
targeting Python 3.13. The saved default-target report is
`logs/eval-worker-typing-20261001-pyright.json`. Repository Ruff, formatting
(1069 files), explicit text encoding checks, and SPDX checks for the two changed
Python files passed. The independent 3.13 environment was reused without
replacing the project `.venv`.

This resolves only the 14 scoped worker diagnostics, not the earlier
whole-project Pyright report. The full default project suite, clean-wheel
acceptance and Linux/macOS execution were not repeated in this follow-up.
Public Windows rollout remains disabled.

## Verification: Configuration Preflight

On 2026-10-01, the next integration step added creation-time generation options
to the existing managed Windows session. Its configuration is checked before
provisioning and again at entry; strategy overrides retain the same Windows
policy. Public backend registration, native grants and all deadlines remain
unchanged.

Regression tests first reproduced silent acceptance of unknown policy keys,
copied configurations reaching resolution or in-process setup, and the internal
LPAC constructor overlooking copied unknown fields. The three configuration
schemas now reject unknown keys. Public execution and internal entry checks
strictly revalidate copies and nested grants. The 25 new cases cover those
refusals, `require=False`, pre-provisioning configuration admission, generation
option binding and validated per-strategy overrides. The existing real Agent
broker/denial/fresh-worker test now supplies generation options before entry.

| Runtime | Scope | Passed | Skipped | Deselected | Elapsed |
| --- | --- | ---: | ---: | ---: | ---: |
| Windows 3.12.13 | Runtime, strategies, configuration and config-chain regression; excludes sandbox directory | 2170 | 19 | 0 | 39.30 s |
| Windows 3.12.13 | LPAC policy, public platform entry and managed session | 174 | 0 | 0 | 449.97 s |
| Windows 3.13.12 | Same policy, platform and managed-session files | 174 | 0 | 0 | 410.59 s |
| WSL2 Linux 3.12.3 | Explicit sandbox containment selection | 69 | 2 | 619 | 31.37 s |
| WSL2 Linux 3.12.3 | Runtime, strategies, configuration and config-chain default selection | 2516 | 288 | 75 | 48.35 s |

Reports use the `logs/windows-preflight-20261001-` prefix, with suffixes
`shared-py312.xml`, `native-py312.xml`, `native-py313.xml`,
`linux-containment.xml` and `linux-shared.xml`. All record zero failures and
zero errors. Linux collection skips include two Windows-only modules; its
default selection also skips native Windows cases and deselects explicit
containment/stress tests. The separate containment run covers all 69 selected
Linux tests. Existing deprecation warnings remain visible.

Both Windows runs include real Agent file and callback denials, successful
granted operations, fresh workers, cancellation, callback deadlines, application
recovery and owned-resource cleanup. The slowest case was 136.18 seconds on
3.12 and 127.12 seconds on 3.13, below the unchanged 180-second case deadline.
Some runs overlapped; these are acceptance observations, not storage-load
benchmarks or worst-case timing guarantees.

Pyright reports zero errors and zero warnings for the six changed production
modules in `logs/windows-preflight-20261001-pyright.json`. Repository Ruff,
formatting (1069 files), explicit encoding and changed-file SPDX checks passed.
Windows reused the independent version-specific caches and the existing 3.13
environment. Linux used the frozen lockfile in the separate environment
`/home/qinguang/.cache/nooa-preflight-20261001-linux` and bytecode cache
`/home/qinguang/.cache/nooa-preflight-20261001-linux-bytecode`; both are retained
for subsequent verification. The Windows project `.venv` was not replaced.

Unknown-key rejection is a deliberate compatibility change for all platforms;
see the [CodeAct configuration notes](concepts/strategies.md#codeact-inspect-act-and-iterate).
This follow-up does not repeat the whole-project default suite, whole-project
Pyright, installed-wheel acceptance, macOS execution or storage contention.
It supplies an internal preflight integration point, not a public Windows
policy interface or backend. The remaining public rollout gates still apply.

## Verification: Staged Public Windows Interface

On 2026-10-01, `nooa.runtime.sandbox.windows` added public names for the existing
Windows policy and grant types, plus a managed-session wrapper. Its launch gate
always raises before provisioning. No native permission, backend selector,
capability report, recovery rule or production deadline changed.

Fourteen new fast cases cover immutable copied policy inputs, native units,
both workspace modes with and without memory caps, pre-provisioning refusal,
absence of caller release flags, Linux-field rejection, generation preflight,
unchanged backend/start-method registration and native platform admission.
The gate test verifies that neither a recovery directory nor a missing host
grant root is created and that a strategy cannot be obtained before readiness.

The four existing real managed-session cases now enter through the staged
public class using a fixture that replaces only its launch gate. They retain
real LPAC provisioning and verify denied host-file/workspace access, ungranted
tools, argument predicates, successful named broker operations, fresh workers,
callback deadlines, cancellation, policy-mutation refusal, staged application
recovery and owned-resource cleanup. The first case additionally permits the
Python `socket` import only in its test configuration and verifies native
`WSAEACCES` (10013), rather than mistaking the language import guard for OS
network enforcement. Its existing retry and resource budgets are unchanged.

| Runtime | Scope | Passed | Skipped | Elapsed |
| --- | --- | ---: | ---: | ---: |
| Windows 3.12.13 | Public API, managed session, LPAC policy and platform support | 188 | 0 | 499.09 s |
| Windows 3.13.12 | Same four source suites | 188 | 0 | 495.02 s |
| WSL2 Linux 3.12.3 | Same four source suites | 184 | 4 | 4.79 s |
| Windows 3.13.12 | Clean five-wheel installation: same four suites plus provenance | 189 | 0 | 453.21 s |

The reports are `logs/windows-api-20261001-source-py312.xml`,
`logs/windows-api-20261001-source-py313.xml` and
`logs/windows-api-20261001-linux.xml`, with zero failures and zero errors.
Linux skips only the four Windows-native cases. The two Windows runs overlapped;
their slowest cases took 161.42 and 162.99 seconds, respectively, below the
unchanged 180-second limit. These are acceptance timings, not latency guarantees
or controlled storage-contention measurements.

The installed-wheel runner now includes the API suite and checks that the
Windows module, as well as all five packages, comes from the external installed
environment. Native acceptance still uses the test-only launch-gate fixture;
the installed default entry remains closed.

The targeted clean installation completed with zero failures, errors or skips;
its report and transcript are `logs/windows-api-20261001-wheel-py313.xml` and
`logs/windows-api-20261001-wheel-py313.log`. It ran outside the checkout in a
Chinese/space-containing environment with isolated Python, no inherited
`PYTHONPATH`/`PYTHONHOME`, and resource/thread/unraisable warnings treated as
failures. Its slowest native case took 129.67 seconds under the unchanged
180-second limit. The installer exited successfully and its owned temporary
root was independently confirmed absent. The project `.venv` was not replaced.

Windows CI now includes the API suite in both native source matrix selections
as well as the default wheel runner. Repository Ruff lint, formatting (1071
files), explicit text encoding, changed-file SPDX, workflow YAML and whitespace
checks passed. Scoped Pyright reports zero errors and zero warnings for
`windows.py`, the sandbox package initializer and `scripts/smoke_install.py`;
the report is `logs/windows-api-20261001-pyright.json`. The existing independent
source environments and platform/version-specific caches were reused.

This follow-up does not repeat the full default project suite, complete
installed-wheel acceptance, whole-project Pyright, macOS execution or storage
contention. The earlier repository-wide typing diagnostics remain unresolved.
The test-only admission establishes targeted behavior of the staged class,
not an enabled public Windows backend or completed release acceptance.

## Verification: Complete Installed-Wheel Acceptance

The first complete Python 3.12.13 invocation on 2026-10-01 collected 586 tests
but was interrupted by the runner's fixed 1800-second whole-suite budget during
the spawn IPC suite. The log is `logs/windows-full-wheel-20261001-py312.log`.
It contains no reported test failure before the outer `subprocess.TimeoutExpired`,
but produced no final XML report and is not a completed acceptance pass. Its
temporary installation was removed and independently confirmed absent.

The runner now allows 3600 seconds for the complete installed suite, while
targeted selections retain 1800 seconds. Individual pytest deadlines, native
startup/cell/broker limits, permission grants, test selection and warning
policies are unchanged. A fast scheduler regression first failed on the old
full-suite budget and passed after this change; it also checks installed
interpreter isolation, mandatory provenance, selection deduplication, failure
propagation and cleanup. No packaged production module changed in this follow-up.

The complete Python 3.12.13 rerun passed all 586 tests in 2513.44 seconds,
with zero failures, errors or skips. Its report and transcript are
`logs/windows-full-wheel-20261001-py312-final.xml` and
`logs/windows-full-wheel-20261001-py312-final.log`. The installer exited
successfully and its temporary root was independently confirmed absent.
The slowest case took 137.30 seconds, below its unchanged 180-second deadline.
This is the entire configured wheel acceptance suite, including offline
application workflows and every listed native suite, not a targeted selection.

The complete Python 3.13.12 run also passed all 586 tests in 2509.32 seconds,
with zero failures, errors or skips. Its report and transcript are
`logs/windows-full-wheel-20261001-py313-final.xml` and
`logs/windows-full-wheel-20261001-py313-final.log`. The slowest case took
137.92 seconds, below its unchanged 180-second deadline. The installer reported
success, its process tree exited, and its owned temporary installation
`nooa-install-ypiut3bm` was independently confirmed absent. Both supported
Windows versions have now completed the entire configured installed suite.
Native public-session tests still replace only the launch gate in a fixture;
these passes do not enable or validate an unrestricted public launch.

The final four scheduler cases passed on Windows Python 3.12.13 (1.17 seconds),
Windows 3.13.12 (1.25 seconds) and WSL2 Linux 3.12.3 (1.41 seconds). Reports are
`logs/windows-full-wheel-20261001-runner-py312-final.xml`,
`logs/windows-full-wheel-20261001-runner-py313-final.xml` and
`logs/windows-full-wheel-20261001-runner-linux.xml`. These mocked scheduler
checks do not substitute for the real installed-package suites.

Repository Ruff lint, formatting (1072 files), explicit encoding and changed-file
SPDX checks passed. Scoped Pyright reports zero errors and zero warnings for the
runner and its tests in `logs/windows-full-wheel-20261001-runner-pyright.json`.
The refreshed whole-project Windows-target baseline remains 323 errors and two
warnings across 314 analyzed files; comparing file, line, rule and message
against the earlier full-project report found no changed diagnostics. Its
report is `logs/windows-release-20261001-pyright-before.json`.

A separate `--pythonplatform Linux` static check on the Windows host reports
461 errors and two warnings in
`logs/windows-release-20261001-pyright-linux-target-before.json`, including
platform-specific native modules. This is a static target comparison, not a
Linux execution result or a clean type-check pass. Neither repository-wide
diagnostic set has been resolved by changing the acceptance scheduler.

## Verification: Memory and Benchmark Test Typing

While the complete installed suites ran, a separate test-only follow-up removed
56 diagnostics from the memory tests and 22 from the benchmark tests. No
packaged production module or installed acceptance case changed.

Memory tests now assert that persisted rows exist before reading their fields,
check concrete reflection event types, use structured access records for
forgetting scenarios, and type the vector-backend test matrix explicitly.
The dynamic memory-skill attachment is checked through `getattr` plus
`isinstance`. Legacy access-log conversion still has its dedicated test.

Benchmark tests use canonical response parts and status enums, the real Agent
runtime instead of an incomplete stand-in, explicit non-null prompt/tool
assertions, and fake-client signatures matching the real client. Cancellation
test doubles declare that they never return normally. The malformed-input
negative test retains every invalid value as explicitly untyped test data.
No diagnostic rule was disabled and no blanket suppression was added.

| Runtime | Suite | Passed | Skipped | Deselected | Elapsed |
| --- | --- | ---: | ---: | ---: | ---: |
| Windows 3.12.13 | Memory | 274 | 13 | 1 | 14.44 s |
| Windows 3.13.12 | Memory | 274 | 13 | 1 | 18.70 s |
| WSL2 Linux 3.12.3 | Memory | 271 | 16 | 1 | 18.11 s |
| Windows 3.12.13 | Benchmark | 126 | 0 | 0 | 18.97 s |
| Windows 3.13.12 | Benchmark | 126 | 0 | 0 | 65.49 s |
| WSL2 Linux 3.12.3 | Benchmark | 126 | 0 | 0 | 23.91 s |

Reports use `logs/windows-release-20261001-` followed by
`memory-tests-{py312,py313,linux}-verified.xml` or
`bench-tests-{py312,py313,linux}-verified.xml`. All record zero failures and
zero errors. Memory skips cover optional vector backends and, on Linux, three
Windows path-reference tests. Its existing live-model test remains deselected,
and its existing Starlette deprecation warning remains visible.

Scoped Pyright is clean for all 24 memory test files and all five benchmark
test files, in `logs/windows-release-20261001-memory-tests-pyright-final.json`
and `logs/windows-release-20261001-bench-tests-pyright-final.json`.
Whole-project Windows-target errors fell from 323 to 245, and Linux-target
errors from 461 to 383; both still have two warnings. Final reports are
`logs/windows-release-20261001-pyright-final.json` and
`logs/windows-release-20261001-pyright-linux-target-final.json`.
Repository-wide type acceptance therefore remains incomplete.

## Verification: Typed Coding Exports and ACP

The preceding ACP test-only cleanup removed nine diagnostics, leaving
236 Windows-target errors and 374 Linux-target errors, with two warnings on
each target. Those baselines are recorded in
`logs/windows-release-20261001-pyright-verified.json` and
`logs/windows-release-20261001-pyright-linux-target-verified.json`.

On 2026-10-01, a further scoped check reproduced 38 ACP source diagnostics.
The coding facade's lazy `__getattr__` exports erased concrete class types:
event handlers could not narrow `EventBase`, and constructed agent/command
objects remained potentially null to the checker. Eight explicit export-type
assertions first failed with `Any`, then passed after adding `TYPE_CHECKING`
imports for the existing exports. Runtime loading remains lazy and the exported
objects retain their defining-module identities.

This exposed one previously hidden MCP registration mismatch. `SkillRegistry`
already accepts pre-constructed non-`Skill` objects, but its annotation excluded
them. The parameter now uses `object`, matching that existing duck-typed
contract without a cast, new adapter, validation change or permission change.
No diagnostic rule was disabled. The complete ACP source/test scope, including
the new export regression file, is clean across 15 analyzed files:
`logs/windows-release-20261001-acp-exports-pyright-final.json`.

| Runtime | Source regression scope | Passed | Expected failures | Elapsed |
| --- | --- | ---: | ---: | ---: |
| Windows 3.12.13 | ACP, skill registry, skill objects, coding agent/activity/settings/slash commands, CLI smoke | 227 | 3 | 105.97 s |
| Windows 3.13.12 | Same scope | 227 | 3 | 127.04 s |
| WSL2 Linux 3.12.3 | Same scope | 227 | 3 | 121.86 s |

The reports are `logs/windows-release-20261001-coding-exports-` followed by
`py312-final.xml`, `py313-final.xml` or `linux-final.xml`.
All have zero failures and errors. The three existing strict expected failures
concern external skill-package ownership across ACP sessions; their markers
and behavior were not changed. Thirteen new runtime checks cover lazy loading,
the identity/cache behavior of all eleven exports, and unknown-name refusal.
The project environment was not replaced, and Linux used a separate bytecode
cache from the Windows runs.

Whole-project Pyright now reports 198 errors and two warnings for Windows and
336 errors and two warnings for the Linux static target, across 315 files.
Reports are `logs/windows-release-20261001-pyright-after-coding-exports.json`
and `logs/windows-release-20261001-pyright-linux-after-coding-exports.json`.
The Linux-target report is a static check on Windows, not Linux execution.
Ruff lint, formatting (1073 files), explicit text encodings and SPDX headers
(1076 source Python files) passed.

The full wheel results above used the build from before this export-typing
follow-up. This follow-up changes type visibility and annotations only, and is
verified by the scoped source regressions; it does not claim another complete
wheel rebuild or full-project test run. The Windows public launch gate,
capability probes and doctor support remain unchanged and closed. Remaining
repository-wide typing and public-path release acceptance are still required.
