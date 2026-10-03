# Upstream integration — 2026-10-03

Integrated upstream `564a34014a354f11009cf7dda44a81b039a04273` into the local
Windows/ACP branch starting at `da52f865ff3f0eeb81a6f733bed847ef2dc048cd`.
The common ancestor was `2ff4485ee6c1dfcfa6956716f3df9f5290ef617d`.
This imports 25 reachable upstream commits (17 non-merge commits), changing
43 files. The upstream revision was pinned at fetch time rather than changing
the integration target during verification.

## Included behavior

- Opt-in application-scoped LLM admission control, with local concurrency
  queues and a parent broker for multiprocess applications.
- Retained Responses cache checkpoints and session-affinity headers, including
  synchronous calls and case-insensitive existing-header detection.
- HTTP 408 retries and Connect asking for a replacement key after rejection.
- Stable CodeAct import descriptions that do not change with loaded-module
  state or library re-imports.
- Long shell stdout/stderr retain both their beginning and end. The truncation
  buffer is fed in bounded chunks, but complete command output is still collected
  before truncation; this is not an overall command-memory limit.

Admission control remains an explicit wrapper. Existing ACP clients do not
automatically acquire a new request limit, and model/network settings were not
changed. The Windows/Linux `code` and `strict` sandbox modes remain available.

## Merge decisions

Only two files had textual conflicts; both sides' behavior was retained.

| File | Resolution |
| --- | --- |
| `src/nooa/tools/_bash_session.py` | Import the upstream truncating buffer while retaining the conditional Windows backend import, TCP control channel, path handling, Job Object ownership, line endings and cancellation cleanup. |
| `src/nooa/unifiedllm/unifiedllm.py` | Add upstream `Awaitable`; keep the local `projected_messages` intermediate to preserve typed replay/cache preparation, together with upstream cache, affinity and admission integration. |

The automatically merged runtime retains managed sandbox call admission and
only strips redundant imports for non-sandbox execution. Windows CodeAct still
uses its explicit staged globals; upstream import descriptions do not broaden
worker imports or filesystem access. New first-party admission modules are
included by existing framework staging, and importing the broker does not start
a listener or connect to it.

Compatibility and check fixes:

- Register the Linux fork-reset callback via guarded `getattr`/`callable`, so
  Windows type stubs do not require an unavailable `os.register_at_fork` API.
- A closed real broker can produce connection refusal or hit the connection
  deadline first on Windows. Accept either explicit failure in that socket test,
  assert the corresponding terminal metric and zero leases/calls, and separately
  verify exact refusal classification with a deterministic connection failure.
  Production broker timeout/error behavior is unchanged.
- Add explicit UTF-8 to the existing Linux `/proc/.../stat` test helper, fixing
  an encoding check failure found by the full repository check.

## Verification

All local verification uses uv and Python 3.12: Windows 3.12.13 and WSL Ubuntu
3.12.3. Model behavior is tested with mocks and local sockets. No live provider
requests were made; the upstream live cache tests were imported but not run.

| Scope | Result | Local evidence |
| --- | --- | --- |
| Windows UnifiedLLM and CLI Connect | Initial full selection: 1747 passed, 1 skipped, 5 deselected; one closed-broker socket expectation failed as described above. | [Initial run](../logs/upstream-20261003-llm-windows.xml) |
| Windows admission/broker after fix | 77 passed, including the added refusal classification case. | [Final affected suite](../logs/upstream-20261003-llm-windows-green.xml) |
| WSL admission/broker/cache/affinity/retry/Connect | 448 passed. | [Run](../logs/upstream-20261003-llm-wsl.xml) |
| WSL admission/broker after test update | 77 passed. | [Final affected suite](../logs/upstream-20261003-llm-wsl-green.xml) |
| Windows shell, CodeAct, metrics and bench | 476 passed. | [Run](../logs/upstream-20261003-shell-windows.xml) |
| WSL shell, CodeAct, metrics and bench | 448 passed, 3 Windows-only skips; those cases passed on Windows. | [Run](../logs/upstream-20261003-shell-wsl.xml) |
| WSL native ACP code/strict | 2 passed, including real worker isolation, host HTTP/file effects, cancellation and session restore. | [Run](../logs/upstream-20261003-acp-native-wsl.xml) |
| Windows full ACP suite, including native code/strict | 144 passed, 3 existing expected failures, 497.63 seconds. | [Run](../logs/upstream-20261003-acp-windows.xml) |
| WSL command-runner encoding helper and native regressions | 10 passed. | [Run](../logs/upstream-20261003-linux-command-encoding.xml) |

The actor's `llm_queue` metrics bridge was also invoked directly and correctly
forwarded admission, queued, maximum-depth and wait-time fields. Dependency
manifests and the lockfile did not change, so the existing environments were
used with `--frozen --no-sync`.

Repository-wide Pyright completed with 0 errors and 0 warnings. Ruff check,
format checks (1138 files), explicit-encoding checks, SPDX validation,
merge-marker and Git whitespace checks passed. A direct comparison against the
pre-merge revision confirmed that `packages/nooa-acp`, the native sandbox source
tree and `_win_bash.py` were preserved.
