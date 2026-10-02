# Current Windows native session acceptance (2026-10-02)

This is the earlier managed-session selection. See
[public-entry acceptance](windows-public-entry-20261002.md) for the subsequent
enabled public session and its installed verification.

## Scope And Result

Rebuild the current worktree's five wheels and verify the Windows policy,
staged public API and real managed LPAC session from an isolated installation.
Windows Python 3.12.13 only. No production code, grants, test assertions,
timeouts or public launch gate were changed.

**189 passed, zero failures, errors or skips**, 463.77 seconds of pytest time.
The smoke runner exited with code 0 and removed its temporary installation at
`C:/Users/QinGu/AppData/Local/Temp/nooa-install-x49z3jfa`.

The selection comprises installed-package provenance plus
`test_windows_api.py`, `test_windows_session.py`, `test_lpac_policy.py` and
`test_platform_support.py`. All five wheels were rebuilt as `0.0.11.dev356`.
The interpreter used `-I`, a copied self-contained suite and an environment
outside the checkout. No source-tree pythonpath or root conftest was supplied.

## Real LPAC Coverage

| Managed native test | Passed | Time |
| --- | --- | ---: |
| Broker grants, access denials, fresh workers and cleanup | Yes | 118.285 s |
| Cancellation and broker deadline | Yes | 105.889 s |
| Policy and deadline mutation refusal | Yes | 80.963 s |
| Staged application, writable workspace and recovery | Yes | 112.785 s |

The first scenario reaches native socket denial (WSAEACCES 10013), rather than
mistaking the optional Python import guard for network containment. It also
checks denied direct host-file/workspace access, ungranted callbacks, argument
predicates, allowed named file/directory operations and clean fresh workers.
The application scenario stages PyYAML and application data types, times out a
worker, verifies fresh globals and retained workspace files, and checks final
removal of owned resources. Original 180-second managed-test deadlines remain.

These four tests replace only `_require_public_launch` in a pytest fixture.
Native provisioning, LPAC processes, brokers, recovery and cleanup are real.
Separate public API tests verify that the unmodified installed entry rejects
launch before provisioning. Both facts matter: passing real native tests is
not evidence of an enabled public entry.

## Reproduction

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTEST_ADDOPTS = '--junitxml=E:/rivon/labs-OO-Agents/logs/windows-native-current-20261002-installed.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12 --test-file test_windows_api.py --test-file test_windows_session.py --test-file test_lpac_policy.py --test-file test_platform_support.py
```

Build/install/test transcript and JUnit report:
`logs/windows-native-current-20261002-installed.log` and `.xml`.
The development environment and earlier reports remain intact.

## Release Evidence And Open Work

| Area | Current evidence or gap |
| --- | --- |
| Configured repository typing | [Zero errors/warnings across 318 files](typing-final-20261002.md); old 198-error figures are historical. |
| Combined source behavior | [Default source and embedded/memory acceptance](typing-acceptance-20261002.md), with documented skips and three ACP xfails. |
| Offline installed workflows | [All twelve passed on fresh wheels](typing-wheel-20261002.md). |
| Current native managed-session path | This 189-case installed selection passes, including four real LPAC cases. |
| Entire native matrix on current source | [The subsequent complete run](windows-native-full-20261002.md) passes all 574 native cases plus 12 installed workflows; its initial timeout and diagnostic follow-up are retained. |
| Supported public launch | Still closed. A release candidate must exercise its actual installed public entry without test-only gate substitution before support is advertised. |
| Broader operational assurance | Disk-full, sustained storage pressure, device variation, expanded application dependencies and recovery/disk-budget policy remain separate work. |

The complete installed native matrix was the next verification step and is
now recorded in the [full acceptance follow-up](windows-native-full-20261002.md).
Public launch/backend/doctor integration remains a separate implementation
and acceptance step, not an automatic consequence of passing that matrix.
Existing broker limitations and explicit grants must not be silently widened
to approximate Linux SandboxConfig.

No extra Python/platform matrix, live model calls, commit or push was performed.
Public Windows sandbox launch remains disabled. Final `git diff --check` passes.
