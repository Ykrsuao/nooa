# Public Windows session acceptance (2026-10-02)

Later evidence: [public-entry stability and complete installed acceptance](windows-public-stability-20261002.md)
passes the six native stress scenarios, repeats both loaded paths after a test
audit fix, and passes all 615 installed tests. The targeted selection and
pending stability follow-up described below retain this earlier run's scope.

## Change and scope

`nooa.runtime.sandbox.windows.WindowsSandboxSession` now enters the existing
managed native lifecycle directly. The unconditional release gate and the
test-only admission fixture have been removed. The public API retains explicit
`WindowsSandboxPolicy` grants, native units, configuration revalidation, platform
admission, sequential calls, cancellation draining and retryable cleanup.
No grant, timeout, dependency version or Linux policy meaning was changed.

Callers enter the session, obtain `session.strategy()`, and supply that strategy
to Agent generation methods. `CodeActConfig(execution_backend="sandbox")`
continues to use the fork executor; there is no automatic Windows translation
or in-process fallback. Policy construction alone creates no native resources.

`WindowsSandboxCapabilities` and `probe_windows_sandbox()` report platform and
AppContainer/Job Object binding availability without creating profiles, jobs,
workers or sandbox files. `containment_verified` is always `False`. Doctor
reports this prerequisite check separately from fork availability, and clearly
identifies `--smoke` as a shell test, not a sandbox containment test.

The session lifecycle suite uses public policy/grant/session names, including
its fast failure/cancellation tests. Four real session cases and three real
managed HTTPS cases now enter the public class without replacing its admission
or provisioning. The ordinary callback-cancellation case additionally verifies
a successful new call on the same session after cancellation.

The new capability suite is included in installed acceptance and Windows CI.
Installed provenance now also checks its module, and the installed doctor
workflow checks the new prerequisite report and its explicit verification limit.

## Source and static verification

All local checks use native Windows Python 3.12.13 and uv.

| Selection | Result |
| --- | --- |
| Public API, fork/platform refusal and installation scheduling | 53 passed, 8.41 s |
| Fast session and broker selection | 137 passed, 8 native cases deselected, 5.24 s |
| Capability and doctor unit tests | 49 passed |
| Configured repository Pyright | 320 files, 0 errors, 0 warnings |
| Repository Ruff lint, formatting and explicit text encoding | Passed; 1,103 formatted Python files |
| SPDX headers | Passed; 1,106 Python files |

An initial new API test attempted normal assignment to the already-frozen
`CodeActConfig` and failed before reaching its intended revalidation check.
It now explicitly simulates bypassing that freeze with `object.__setattr__`;
the unchanged entry validation rejects the modified config before provisioning.
The initial result (48 passed, 1 failed) was not an implementation failure or
an acceptance pass. No production validation was weakened.

The SPDX check found one missing header in the earlier diagnostic-only
`logs/profile_windows_cleanup.py`. Its header was added; diagnostic behavior
and packaged runtime behavior were unchanged.

## Installed verification

The isolated installed selection passed **262 tests**, with zero failures,
errors or skips, in **679.50 seconds (11 minutes 19 seconds)**. The runner exited
with code 0. Its temporary installation at
`C:/Users/QinGu/AppData/Local/Temp/nooa-install-1pma17fv` was confirmed absent.

The runner rebuilds all five wheels, installs outside the checkout, copies a
self-contained suite and executes the installed interpreter with `-I`.
Resource, unraisable-exception and unhandled-thread warnings remain failures.
The complete copied suite collects 615 cases; this request selects 262 and
deliberately deselects 353. This is a targeted public-entry run, not a new full
native matrix pass.

All seven real public-entry cases passed without gate substitution:

| Installed public-entry scenario | Time (JUnit) |
| --- | ---: |
| Named broker grants, host-file/workspace/socket denials, fresh workers and cleanup | 73.554 s |
| Agent callback cancellation, broker deadline and subsequent successful call | 89.479 s |
| Policy and deadline mutation refusal | 45.510 s |
| Staged PyYAML/application, worker recovery and writable workspace preservation | 77.034 s |
| Managed HTTPS permitted request and enforced denials | 83.808 s |
| Managed HTTPS broker deadline, cancellation and new calls | 89.020 s |
| Managed HTTPS request deadline, cancellation and new calls | 79.834 s |

The unchanged native-case timeout is 180 seconds. The installed doctor shell
workflow also passed (15.590 seconds), including its new prerequisite report,
offline operation and absence of user configuration writes. All five packages
were rebuilt as `0.0.11.dev356`; module provenance resolves under the isolated
installation. The runner prints its standard `Clean-install acceptance passed.`
message, but the explicit pytest selection above still applies.

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTHONUNBUFFERED = '1'
$env:PYTEST_ADDOPTS = '-k "test_windows_api or test_windows_session or test_windows_capabilities or test_windows_cleanup or test_lpac_recovery or test_lpac_policy or test_platform_support or test_managed_https or imports_are_from_installed_wheels or doctor_smoke_is_offline" --durations=12 --junitxml=E:/rivon/labs-OO-Agents/logs/windows-public-installed-20261002.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12
```

Reports: `logs/windows-public-installed-20261002.log` and `.xml`,
`logs/windows-public-api-20261002.xml`, and
`logs/windows-public-pyright-20261002.json`.

## Limits and follow-up

Worker `restart_empty` recovery preserves workspace files while replacing the
worker namespace. `recovery_directory` enrolls owned resources for the existing
internal orphan-recovery machinery; it does not expose a public automatic
crash-recovery service or adopt unidentified resources. Failed close retains
owned resources for an explicit `aclose()` retry.

The earlier [cleanup optimization](windows-cleanup-20261002.md) and
[complete installed matrix](windows-native-full-20261002.md) remain historical
evidence for their respective code states. This change adds the actual public
entry and its targeted acceptance. It does not establish sustained storage
stability, disk-full behavior, arbitrary dependency compatibility, or general
network/descriptor I/O support. Explicitly granted host callbacks still run
with host authority. Native stress and interruption audits beyond this selection
remain operational follow-up work.

Final `git diff --check` passes. No extra Python/platform matrix, live model
calls, commit or push was performed.
