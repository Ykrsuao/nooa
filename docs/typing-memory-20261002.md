# Memory and Viewer Typing: 2026-10-02

## Scope and Results

This follows the [Connect typing batch](typing-connect-20261002.md), using
Windows Python 3.12.13. Existing uncommitted changes are preserved. Dependencies,
lockfile, supported Python versions and Pyright configuration are unchanged.

| Type check | Files | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Memory package and Viewer memory routes, before | 18 | 32 | 0 |
| Same scope plus two new contract test files, after | 20 | 0 | 0 |
| Repository after Connect | 316 | 99 | 1 |
| Repository after this batch | 317 | 67 | 1 |

No new file/message diagnostic pairs were introduced. The existing CLI source
exclusion remains; this is not a clean whole-repository type result. No casts
or diagnostic suppressions were added.

## Changes

- Type LiteLLM's heterogeneous keyword options as `dict[str, Any]`, matching
  the dynamic SDK boundary rather than treating every value as `object`.
  Preserve fractional timeouts, zero retries, optional-key omission, batching
  and normalization. This boundary remains dynamically typed; it is not static
  validation of every provider option.
- Define structural `ReflectionClient` and text-response protocols for the
  synchronous completion surface. The getter remains lazy per call, accepts
  both UnifiedLLM and structural clients, and does not eagerly import the model
  runtime just to evaluate annotations.
- Sum narrowed rank/score values. Missing evidence remains `None`; zero values
  remain observed values and contribute to the mean.
- Make the already-enforced non-null callback invariants explicit inside
  reflection operations. Ordering, interruption, owner isolation and model
  call budgets remain unchanged.
- Guard the optional tracing module where a deferred event handler uses it.
  Without the tracer, installation and already-created handlers remain no-ops.
- Resolve optional sqlite-vec/Chroma modules through `import_module` only when
  their backend is selected. These are runtime plugin boundaries, not required
  typing dependencies. Missing-package errors and backend configuration are
  preserved; no optional packages were installed.
- Separate Viewer annotation imports from runtime imports. A data request
  without `nooa-memory` now returns HTTP 503 before store resolution rather
  than reaching unbound names. Database discovery and Viewer startup remain
  usable. Existing path containment, owner validation, cache eviction and
  database migration behavior are unchanged.

## Verification

Runtime checks used the separate Windows bytecode cache:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest packages/nooa-memory/tests tests/viewer packages/nooa-cli/tests -q --junitxml=logs/typing-memory-20261002-regression.xml
```

| Runtime check | Passed | Skipped | Deselected | Time |
| --- | ---: | ---: | ---: | ---: |
| Existing memory baseline | 274 | 13 | 1 | 19.43 s |
| Memory, Viewer and CLI regression | 811 | 15 | 3 | 54.47 s |
| Final new-contract rerun | 14 | 0 | 0 | 6.02 s |

All final runs exited successfully. The regression retains one pre-existing
Starlette TestClient deprecation warning. Existing optional-dependency skips
and marker exclusions were not changed.

The new contracts cover SDK argument preservation, runtime and structural
client typing, unavailable/zero/recorded statistics, missing vector plugins,
mocked sqlite-vec loading and all three Chroma client configurations, deferred
tracing handlers, and isolated Viewer startup plus four HTTP 503 responses
when the memory package is blocked from import.

Actual sqlite-vec/Chroma integration was not run: their modules are absent.
Mock module tests validate adapter configuration, not the external engines.
No paid/live inference, full repository runtime suite, wheel acceptance,
Linux sandbox execution or extra Python-version matrix was run.

Repository Ruff lint, formatting (1086 files), explicit-encoding checks and
license headers (1089 source Python files) pass. Final `git diff --check` passes.

Evidence under `logs/typing-memory-20261002-`:

- `before.json`, `scoped.json`, `project.json`: Pyright reports.
- `baseline.xml` / `.log`: existing memory suite.
- `contracts.xml`: final 14-case boundary suite.
- `regression.xml` / `.log`: memory, Viewer and CLI regression.

The prior `logs/typing-connect-20261002-project.json` is retained for comparison.

## Remaining Work

Follow-up: the [sandbox guards/wire batch](typing-sandbox-20261002.md) clears
the 22-error boundary described below.

The repository still has 67 errors and one warning. A next bounded batch is
native sandbox guards/wire typing: 15 errors in `guards.py` and seven in
`wire.py`. This batch did not start that work or enable public Windows sandbox
launch. Nothing was committed or pushed.
