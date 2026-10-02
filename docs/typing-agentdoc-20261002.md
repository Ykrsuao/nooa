# Agentdoc Typing: 2026-10-02

## Scope and Results

This follows the [MCP tool batch](typing-mcp-20261002.md). Verification uses
Windows Python 3.12.13 and Pyright 1.1.411. Production changes are confined to
`_docs.py`, `_discover.py` and the Plotly adapter. Existing dirty changes,
dependencies, lockfile and type-checker configuration are preserved.

| Type check | Files | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Three-module baseline | 3 | 8 | 0 |
| Same modules plus new contract tests | 4 | 0 | 0 |
| Repository after MCP | 317 | 29 | 1 |
| Repository after this batch | 317 | 21 | 1 |

No new file/message diagnostic pairs were introduced. No casts, diagnostic
suppressions or exclusions were added. The existing CLI exclusion remains;
the full repository is not type-clean.

## Changes

- Import `is_hidden_field` alongside the existing local structured extractor
  import. Both class and instance paths now have a definitely-bound helper.
  Visibility filtering, field selection, descriptor avoidance and referenced
  type ordering are unchanged.
- Mark the private omission sentinel as `Any`. This is deliberately limited
  to the opaque default, not the public parameter types: `max_length`,
  `max_string` and `max_depth` retain `int | None`. Omitting a parameter still
  means no override, while explicit `None` means unlimited and zero remains
  an explicit value. No sentinel type leaks into generated API documentation.
  This does not add static validation of the sentinel itself.
- Load Plotly modules through `import_module` inside the opt-in adapter.
  These remain runtime plugin dependencies, not required typing dependencies.
  Explicit adapter import still requires Plotly; `register_all()` still skips
  unavailable libraries. Curated documentation and registration targets are
  unchanged. No Plotly installation was performed.

## Verification

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest tests/agentdoc tests/context_blocks tests/agents tests/strategies -q --junitxml=logs/typing-agentdoc-20261002-regression.xml
```

| Runtime check | Passed | Skipped | Time |
| --- | ---: | ---: | ---: |
| Existing agentdoc baseline | 722 | 0 | 14.12 s |
| New contract tests | 7 | 0 | 4.54 s |
| Agentdoc, context blocks, agents and strategies | 2018 | 2 | 35.92 s |

All runs exited with code 0. Baseline and regression each retain three existing
pytest deprecation warnings. The seven new cases are included in the 2018-case
regression count, not additional to it.

New contracts cover omitted/None/zero/finite rendering limits, metadata retained
across imperative updates, unchanged public annotations, instance-specific
visibility versus class discovery, seen/field filters, curated module
registration and optional-dependency absence. Plotly registration tests run in
isolated subprocesses with simulated modules, avoiding global registry leakage
into the agentdoc suite. They do not verify real Plotly integration or rendering;
Plotly is absent in this environment.

Repository Ruff lint, formatting (1090 files), explicit-encoding checks and
license headers (1093 source Python files) pass. Final `git diff --check` passes.

Reports use the prefix `logs/typing-agentdoc-20261002-`:

- `before.json`, `scoped.json`, `project.json`: Pyright reports.
- `baseline.xml` / `.log`: existing agentdoc tests.
- `contracts.xml`: seven-case boundary tests.
- `regression.xml` / `.log`: related-module regression.

The preceding `logs/typing-mcp-20261002-project.json` is preserved for comparison.
No full repository runtime suite, native LPAC acceptance, installed-wheel
acceptance, live model calls or extra Python/platform matrix was run. Public
Windows sandbox startup remains disabled. Nothing was committed or pushed.

## Remaining Work

The repository has 21 errors and one warning. A next bounded runtime batch is
the actor and lock-loop boundary: three errors in `src/nooa/runtime/actor.py`
and eight in `src/nooa/runtime/tests/test_gl212_lock_loop.py`. Those files were
not changed in this batch.

Follow-up: [runtime actor and lock-loop typing](typing-runtime-20261002.md)
clears those eleven errors, leaving ten errors and one warning.
