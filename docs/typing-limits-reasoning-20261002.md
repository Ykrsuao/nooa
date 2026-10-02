# Limits and reasoning typing verification (2026-10-02)

## Scope

Continue from [runtime typing](typing-runtime-20261002.md) on local Python
3.12.13 with uv. Preserve unrelated worktree changes.

- Declare the expected return types of dynamically discovered context-limit
  and reply-reduction callables. Existing callable checks, legacy fallbacks,
  argument forwarding and exception propagation remain unchanged. This is a
  typing contract for custom clients, not runtime validation of their results.
- Share reasoning declaration checks through a normal private method, invoked
  both by the Pydantic after-validator and by settings selection. The validator
  returns Self. Nested configuration edits still undergo declaration checks.
- Add seven contract cases for custom-hook forwarding and result identity,
  non-callable fallback attributes, hook exceptions, and mutations affecting
  an unselected level or the declared default.

No casts, suppressions, dependency changes or typing exclusions were added.

## Verification

Repository Pyright: **10 errors -> 7 errors**, one warning unchanged, 317 files.
Compared by diagnostic file and message with
`logs/typing-runtime-20261002-project.json`, exactly three diagnostics disappear
and none are added. Scoped analysis of both modules and the new test file has
zero errors and warnings.

Pytest runs used the local cost map and separate Windows bytecode cache:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
```

| Check | Passed | Skipped | Warnings | Time |
| --- | ---: | ---: | ---: | ---: |
| New contracts, reasoning levels/wire, effective limits, context recovery/safety net | 178 | 0 | 0 | 10.22 s |
| tests/runtime excluding sandbox, src/nooa/runtime/tests | 1319 | 19 | 9 | 21.69 s |

Both runs exited successfully. Suites overlap; counts are not disjoint.
Runtime warnings are existing deprecations.
Reports use prefix `logs/typing-limits-reasoning-20261002-`:
`project.json`, `scoped.json`, `regression.log`, `regression.xml`, `runtime.log`
and `runtime.xml`.

Repository Ruff lint, formatting (1092 files), explicit-encoding checks and
SPDX headers (1095 Python files) pass. `git diff --check` passes.

No full repository test run, native LPAC acceptance, installed-wheel acceptance,
live model calls or extra Python/platform matrix was run. Public Windows sandbox
startup remains disabled. Nothing was committed or pushed.

## Remaining Work

Seven errors remain: bench runner (1), interactive (3), skill registry (2),
and predict serialization test (1). The package export warning is unchanged.
The next bounded batch can address the skill registry's two optional-key errors.

Follow-up: [skill registry typing](typing-skill-registry-20261002.md) clears those
two errors, leaving five errors and one warning.
