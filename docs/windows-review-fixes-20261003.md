# Windows review fixes (2026-10-03)

This change addresses the three P2 findings from the review of
`2ff4485e..cdb2262b`, plus the existing Windows CRLF replacement defect.
The P3 suggestion to share broker file-transfer loops remains a maintenance
suggestion; these fixes do not change broker authorization or transfer logic.

## Changes and regression evidence

### Bash startup process ownership

Windows Bash is created suspended, assigned to its process Job, and only then
resumed. `BASH_ENV` cannot run before Job ownership is established. Because
`asyncio` closes the original primary-thread handle, the launcher locates the
suspended thread using the documented Toolhelp APIs and checks the resume result.
Startup failures use the existing cancellation-protected session cleanup.

The regression launches a real Python child from `BASH_ENV` and injects a
0.5-second scheduling delay before Job assignment. Before the fix the child was
absent from the Job; after the fix it belongs to the Job and dies on session
close or a control-connection failure. Additional cases cover failed Job
creation, assignment, resume, and repeated cancellation during cleanup.

### Profile initialization rollback

The runtime retains a close-safe Profile before native allocation and SID
initialization. The runtime owns rollback in its existing cleanup order. If
deleting the native profile fails, the object and SID remain reachable for a
later `close()` attempt. Standalone Profile construction still rolls back its
own failures.

Six failing regressions cover SID conversion, user SID lookup, and registry
capability lookup, each with temporary and recovery-enrolled roots. All now
verify a second delete attempt succeeds and releases the SID, root, and lease.
Two further tests cover standalone rollback and retention failure before
allocation. These fault-injection tests mock native APIs rather than leaking
real profiles.

### Managed session admission

Generation strategies have a synchronous `call_scope()` that is entered before
the Agent generation lock. Ordinary strategies use a no-op scope and retain
their serialized behavior. The managed Windows strategy reserves its session
until setup, execution, and teardown complete, including cancellation while
waiting for the Agent lock.

Reflexion delegates admission to its base strategy and retains it across
reflection and sequential retries. Nested strategy execution may borrow an
idle reservation owned by the same asyncio task. Nested Agent calls and calls
made while the managed strategy is executing still fail immediately.

The regressions retain real Agent dispatch, CodeAct, and FakeLLM execution,
replacing native allocation only. They cover same-Agent and shared-session
overlap, cancellation, setup failure, nested/direct calls, ordinary Agent
serialization, and Reflexion's reflection/retry interval. The original probe's
second call now raises `SandboxUnavailable` while the first call is still busy.

### File replacement line endings

Path and Match replacement inputs use the same universal-newline representation
as file reads before the result is written with the original file's LF or CRLF
style. CRLF input no longer becomes `\r\r\n`; CRLF search text matches the
normalized file content. `write_file()` continues to write content verbatim.

Fourteen additional cases cover both replacement forms, LF/CRLF destination
files, LF/CRLF/CR input, and CRLF search text. Before the fix, ten failed; all now
pass.

## Verification

All runs use native Windows Python 3.12.13 and `uv run --no-sync`. No live model
service is used. ResourceWarning, PytestUnraisableExceptionWarning, and
PytestUnhandledThreadExceptionWarning are errors in the integrated test runs.

| Check | Result |
| --- | --- |
| Shell, embedded tools, CLI editing observers | 417 passed |
| Runtime, strategies, Agent generation lock | 2,125 passed, 18 skipped |
| Native Windows session, API, cleanup, AppContainer, recovery, Job, LPAC CodeAct | 271 passed |
| Configured Pyright | 0 errors, 0 warnings |
| Repository Ruff lint, format, explicit encoding | Passed |
| Repository SPDX headers | Passed, 1,101 source files |
| `git diff --check` | Passed |

The three integrated suites total 2,813 passed and 18 skipped, with no failures.
Their durations were 195.16 seconds (Shell), 49.39 seconds (runtime), and 564.26
seconds (Windows sandbox). The skips are existing platform/signal limitations
and warning-capture exclusions; no new regression test is skipped on Windows.

The first runtime run found four failures caused by reused pytest bytecode
containing `/mnt/e/...` source filenames. Those filenames prevented source
inspection on native Windows and made constant-return test methods look like
generation stubs. All four passed using an independent bytecode cache without
application changes. The three affected generated cache files were then removed
and the same four tests passed through the ordinary invocation as well.

Integrated commands (append the three warning filters described above):

```powershell
uv run --no-sync pytest -q tests/tools src/nooa/tools/tests packages/nooa-cli/tests/test_coding_activity.py --junitxml=logs/windows-fixes-shell-20261003.xml

uv run --no-sync python -X pycache_prefix=logs/windows-fixes-pycache -m pytest -q tests/core_runtime tests/strategies tests/runtime --ignore=tests/runtime/sandbox src/nooa/runtime/tests/test_gl212_lock_loop.py --junitxml=logs/windows-fixes-runtime-clean-cache-20261003.xml

uv run --no-sync pytest -q tests/runtime/sandbox/test_windows_session.py tests/runtime/sandbox/test_windows_api.py tests/runtime/sandbox/test_windows_cleanup.py tests/runtime/sandbox/test_appcontainer.py tests/runtime/sandbox/test_lpac_recovery.py tests/runtime/sandbox/test_windows_job.py tests/runtime/sandbox/test_lpac_codeact.py --junitxml=logs/windows-fixes-sandbox-20261003.xml
```

JUnit reports remain in the ignored local `logs/` directory. This verification
does not rerun the complete wheel-install acceptance or a multi-platform or
multi-version matrix.
