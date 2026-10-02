# Summarization Typing: 2026-10-02

## Scope and Results

This follows the [sandbox guards/wire batch](typing-sandbox-20261002.md).
Verification uses Windows Python 3.12.13 and Pyright 1.1.411. Production edits
are confined to `src/nooa/agents/summarization.py`; existing dirty changes,
dependencies, lockfile and type-checker configuration are preserved.

| Type check | Files | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Summarization baseline | 1 | 10 | 0 |
| Summarization plus new contract tests | 2 | 0 | 0 |
| Repository after sandbox batch | 317 | 45 | 1 |
| Repository after this batch | 317 | 35 | 1 |

No new file/message diagnostic pairs were introduced. No casts, suppressions
or exclusions were added. Existing suppressions and the CLI source exclusion
were not changed; the whole repository is not type-clean.

## Changes

- Narrow the optional parent before resolving its client. A detached standalone
  summarizer retains its own client; a task-local override still takes priority.
- Preserve the rendered-history string and token-counter contracts in typed
  locals. This restores information lost through dynamic decorators without
  changing shared decorators or the callable-counter fallback.
- Reject token-budget installation without a parent before adding subscriptions.
  If its event manager has been detached, the interceptor passes the parent
  call through without allocating a summary task.
- Use `LLMCallContext` and `LLMCallNext` for fork dispatch. A missing manager or
  effective client, or a middleware result of the wrong context type, follows
  the existing contained-failure path: no summary application, preserved
  history, and existing consecutive-failure accounting.
- The typed context exposed an additional optional-runtime access. When an
  automatic input budget lacks its runtime/reply reserve, skip the fork after
  returning the completed parent response rather than making an unbudgeted
  request or failing the parent call.

Normal context-window calculations, client switching, request snapshot
ownership, cache-prefix replay, filtered-history handling, tool rejection,
stale-source checks, BeforeTurn application and cancellation remain unchanged.
No middleware dispatch abstraction or sandbox policy was changed.

## Verification

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest tests/agents tests/config/test_summarizer_configs.py tests/unit/test_remaining_full_coverage.py tests/runtime --ignore=tests/runtime/sandbox -q --junitxml=logs/typing-summary-20261002-regression.xml
```

| Runtime check | Passed | Skipped | Time |
| --- | ---: | ---: | ---: |
| Existing agents and summarizer configuration baseline | 124 | 2 | 10.71 s |
| Agents, configuration, edge cases and ordinary runtime | 1331 | 20 | 25.30 s |
| Final new-contract rerun | 11 | 0 | 9.32 s |

All final runs exited with code 0. The broader regression retains nine existing
runtime API-deprecation warnings. The eleven new tests are included in the
1331-case regression count, not additional to it. They cover detached client
selection, partial-install prevention, rendered/provided input, token-counting
fallback, detached interception, missing automatic-budget runtime, and invalid
fork manager/client/middleware contexts.

Existing tests also cover parent-model changes, configured reply reserves,
real SDK wire bodies with mocked providers, no tool execution, failure limits,
request detachment, source-ID invalidation, cancellation and release scenarios.
No paid/live model calls were made. Long-running native LPAC tests, the full
repository runtime suite, installed-wheel acceptance and additional platform
or Python-version matrices were not run in this batch. Public Windows sandbox
startup remains disabled.

Repository Ruff lint, formatting (1088 files), explicit-encoding checks and
license headers (1091 source Python files) pass. Final `git diff --check` passes.

Reports use `logs/typing-summary-20261002-`:

- `before.json`, `scoped.json`, `project.json`: Pyright results.
- `baseline.xml` / `.log`: existing agent/configuration tests.
- `contracts.xml`: final eleven-case contract run.
- `regression.xml` / `.log`: 1331-case regression.

The prior `logs/typing-sandbox-20261002-project.json` remains available for
diagnostic comparison. Nothing was committed or pushed.

## Remaining Work

Follow-up: the [MCP tool batch](typing-mcp-20261002.md) clears the six errors
in the external tool boundary described below.

The repository still has 35 errors and one warning. A next bounded production
batch is `src/nooa/mcp/tool.py`, with six errors at the external tool boundary.
The eight errors in the runtime lock-loop test are separate test typing debt.
Neither was changed here.
