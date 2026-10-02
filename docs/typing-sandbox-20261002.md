# Sandbox Guards and Wire Typing: 2026-10-02

## Scope

This follows the [memory/Viewer batch](typing-memory-20261002.md). Development
and runtime verification use Windows Python 3.12.13. Only `guards.py`, `wire.py`
and a new portable boundary test file are changed in this batch. Existing dirty
changes are preserved; dependencies, lockfile and Pyright settings are unchanged.

| Type check | Files | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Guards and wire baseline, Windows target | 2 | 22 | 0 |
| Guards, wire and new tests, Windows target | 3 | 0 | 0 |
| Same scope, Linux static target | 3 | 0 | 0 |
| Repository after memory/Viewer | 317 | 67 | 1 |
| Repository after this batch | 317 | 45 | 1 |

No new file/message diagnostic pairs were introduced. No casts, diagnostic
suppressions or exclusions were added. The existing CLI exclusion remains.
Linux is a static-analysis target here, not an executed Linux runtime matrix.

## Changes

- Add explicit `sys.platform` checks to Linux capability probes and installers.
  Outside Linux, probes return unavailable and installers raise before reaching
  fork or raw syscalls, regardless of the reported CPU architecture. Windows
  resource-limit installation raises a clear `OSError` before importing the
  POSIX resource module. Unsupported hosts do not silently skip requested guards.
- Keep the Linux implementations intact: rlimit headroom and existing hard-cap
  clamping, Landlock rights and descriptor lifecycle, and seccomp filter bytes
  and installation order are unchanged. The Linux static target checks code
  that is unreachable under the Windows static target.
- Enforce the codec's bytes return contract after `msgpack.packb`. Unexpected
  non-bytes results raise `TypeError`; serialization format and decode rules
  are unchanged.
- Use a typed NumPy import only after observing that NumPy is already loaded.
  Scalar and array dispatch remain in the same order, after declared dataclass
  handling. The codec does not eagerly require NumPy just for ordinary values.
  No pickle-based worker message decoding or new constructors are introduced.

Public Windows sandbox startup remains disabled. These edits neither enable a
backend nor modify enforcement policy, allowlists, resource defaults or the
worker protocol.

## Verification

Runtime commands use the separate Windows bytecode cache:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest tests/runtime --ignore=tests/runtime/sandbox -q --junitxml=logs/typing-sandbox-20261002-runtime.xml
uv run --no-sync python -m pytest tests/runtime/sandbox/test_wire.py tests/runtime/sandbox/test_platform_support.py tests/runtime/sandbox/test_error_serialization.py tests/runtime/sandbox/test_guard_wire_contracts.py tests/agents/test_forked_summarizer_wire.py -q --junitxml=logs/typing-sandbox-20261002-focused.xml
```

The existing wire/platform baseline passed all 81 tests in 8.29 seconds. The
new 15-case contract suite passed in 3.64 seconds. Its simulated Linux calls
verify dispatch and arguments without installing irreversible restrictions
in the test process; they do not establish Linux kernel enforcement.

The focused regression passed all 136 tests in 10.94 seconds: existing wire,
platform support, error serialization, new guard/wire contracts and forked
summarizer wire tests. A separate diagnostic rerun of 38 broker address/URL
validation tests also passed while the broader suite was running.

The contracts exercise non-Linux rejection, POSIX resource-limit clamping,
seccomp child exit status and install ordering, Landlock rule dispatch and
descriptor closure, bytes-only packer output, optional NumPy dispatch, and
rejection of dataclass classes while retaining instance/scalar round trips.

The initial expanded run included all of `tests/runtime/sandbox`, including
slow native Windows LPAC integration tests. It was explicitly terminated to
bound this typing batch after progressing through broker tests into LPAC
CodeAct tests; the stopped process exited with code 1. Its partial log is
retained, but it is not a passing regression result or completed native
acceptance. A narrower ordinary-runtime run excludes that directory, while
the focused command above covers the changed wire/platform surface.

Ordinary runtime regression completed with 1141 passed, 18 skipped and nine
pre-existing API-deprecation warnings in 22.13 seconds (exit code 0). Together
with the disjoint 136-case focused run, 1277 regression cases passed. The
15 new contract cases are included in the focused count, not counted twice.

Repository Ruff lint, formatting (1087 files), explicit-encoding checks and
license headers (1090 source Python files) pass. Final `git diff --check` passes.

Reports use the prefix `logs/typing-sandbox-20261002-`:

- `before.json`, `scoped.json`, `linux.json`, `project.json`: type checks.
- `baseline.xml`: existing wire and platform tests.
- `contracts.xml`: new portable boundary tests.
- `focused.xml`: 136 directly related regression tests.
- `regression.log`: intentionally stopped, incomplete expanded run.
- `runtime.xml` / `.log`: ordinary runtime regression excluding the separately
  covered sandbox directory.

No full repository runtime suite, installed-wheel acceptance, live inference,
Linux kernel sandbox execution or extra Python-version matrix was run. Nothing
was committed or pushed.

## Remaining Work

Follow-up: the [summarization batch](typing-summary-20261002.md) clears the ten
errors in the next production module described below.

The repository still has 45 errors and one warning. The next bounded production
module is `src/nooa/agents/summarization.py`, with ten errors. Its runtime and
worker serialization tests should accompany that cleanup; it was not changed
in this batch.
