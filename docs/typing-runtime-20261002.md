# Runtime typing boundary verification (2026-10-02)

## Scope

Continue from [agentdoc typing](typing-agentdoc-20261002.md), using local
Python 3.12.13 and uv. Preserve unrelated worktree changes.

- Actor message construction now declares dictionary, LLMResponse and
  CacheBoundary entries, matching the middleware request contract. Read-only
  snapshot helpers accept Sequence of these entries. No conversion, replay
  mutation, retry-policy change or lock implementation change is introduced.
- Loop-change tests use actual event loops closed in finally blocks, explicit
  exception-holder types and a non-None check before notification-pair access.
- Four snapshot tests cover mixed message identity, native response state,
  system event emission, non-dictionary edges and dictionary-only input.

## Verification

Repository Pyright: **21 errors -> 10 errors**, one warning unchanged, 317 files.
Comparison by diagnostic file and message against
`logs/typing-agentdoc-20261002-project.json` shows exactly 11 removed diagnostics
and none added. Actor accounts for three, lock-loop tests for eight.
Scoped Pyright checks all three changed Python files: zero errors or warnings.

All pytest runs used:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
```

| Check | Passed | Skipped | Warnings | Time |
| --- | ---: | ---: | ---: | ---: |
| tests/runtime excluding sandbox, src/nooa/runtime/tests | 1319 | 19 | 9 | 22.38 s |
| Middleware, replay ownership/reasoning/collapse, snapshot contracts | 153 | 0 | 0 | 10.14 s |

Both runs exited successfully. The four new snapshot cases appear in both runs;
these counts are not disjoint. Runtime warnings concern existing deprecations.
Reports: `logs/typing-runtime-20261002-{project,scoped}.json`,
`logs/typing-runtime-20261002-{regression,replay}.{log,xml}`.

Repository Ruff lint, formatting (1091 files), explicit encoding checks and
SPDX headers (1094 Python files) pass. `git diff --check` passes.

No full repository test run, native LPAC acceptance, installed-wheel acceptance,
live model calls or extra Python/platform matrix was performed. Public Windows
sandbox startup remains disabled. Nothing was committed or pushed.

## Remaining Work

The ten remaining errors are in bench runner (1), interactive (3), skill
registry (2), predict serialization test (1), UnifiedLLM limits (2) and reasoning
(1). The package export warning is unchanged. A subsequent bounded batch can
address the UnifiedLLM limits/reasoning contracts with focused regression tests.

Follow-up: [limits and reasoning typing](typing-limits-reasoning-20261002.md)
clears those three errors, leaving seven errors and one warning.
