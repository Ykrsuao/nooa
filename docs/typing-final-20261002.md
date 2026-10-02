# Final typing debt verification (2026-10-02)

## Scope

Continue from [interactive typing](typing-interactive-20261002.md) using local
Python 3.12.13 and uv. Preserve all earlier and unrelated worktree changes.

- Benchmark public JSON projection uses the existing runtime-checkable
  SupportsInstanceValues protocol to narrow models before calling the method.
  The existing class-level callable guard and hidden/repr/exclude filtering
  remain. Nested models are still serialized through live public projections,
  not model_dump, so provider state is not exported first.
- Predict output serialization tests construct responses with AssistantText
  parts, matching the current response constructor. Production strategy logic
  is unchanged.
- Declare the top-level ReflexionStrategy export under TYPE_CHECKING using the
  experimental factory, not the underlying class. Runtime lazy resolution and
  the factory's FutureWarning remain unchanged.

New coverage verifies plain/custom model projections, hidden field filtering,
nested response state exclusion and preservation, and silent top-level import
with warning-emitting instantiation. No casts, suppressions, exclusions or
dependency changes were added.

## Verification

Repository Pyright: **2 errors and 1 warning -> 0 errors and 0 warnings**.
The analysis covers 318 files (previously 317); the explicit type-checking
factory import brings its module into analysis. No diagnostic remains.
Scoped checks cover the four initial changed/new Python files and separately
the new public export test, also with no errors or warnings.

Pytest runs used:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
uv run --no-sync python -m pytest packages/nooa-bench/tests src/nooa/strategies/tests tests/strategies tests/unit/test_quick_wins.py -q --junitxml=logs/typing-final-20261002-regression.xml
uv run --no-sync python -m pytest tests/test_public_export_typing.py -q --junitxml=logs/typing-final-20261002-export.xml
```

| Check | Passed | Skipped | Warnings | Time |
| --- | ---: | ---: | ---: | ---: |
| Bench, strategies and experimental helpers | 1148 | 0 | 0 | 31.22 s |
| Top-level experimental export | 1 | 0 | 0 | 2.98 s |

All checks exited successfully. Reports use prefix
`logs/typing-final-20261002-`: `project.json`, `scoped.json`,
`export-scoped.json`, `regression.log`, `regression.xml` and `export.xml`.

Repository Ruff lint, formatting (1096 files), explicit-encoding checks and
SPDX headers (1099 Python files) pass. `git diff --check` passes.

## Limits

This completes the current configured Pyright debt, not every acceptance task.
No full repository/runtime suite, native LPAC acceptance, installed-wheel
acceptance, live model calls or additional Python/platform matrix was run.
Public Windows sandbox startup remains disabled. Nothing was committed or pushed.

Follow-up: [post-typing source acceptance](typing-acceptance-20261002.md) verifies
the accumulated changes with default source plus embedded/memory regressions.
