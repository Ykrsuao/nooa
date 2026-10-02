# MCP Tool Typing: 2026-10-02

## Scope and Results

This follows the [summarization batch](typing-summary-20261002.md), using
Windows Python 3.12.13 and Pyright 1.1.411. Production changes are confined to
`src/nooa/mcp/tool.py`. Existing dirty work, dependencies, lockfile and type
checker configuration are unchanged.

| Type check | Files | Errors | Warnings |
| --- | ---: | ---: | ---: |
| MCP tool baseline | 1 | 6 | 0 |
| MCP tool plus new contract tests | 2 | 0 | 0 |
| Repository after summarization | 317 | 35 | 1 |
| Repository after this batch | 317 | 29 | 1 |

No new file/message diagnostic pairs were introduced. No casts, suppressions
or exclusions were added. The existing CLI exclusion remains, and the full
repository is not type-clean.

## Changes

- Read optional text attributes once with a sentinel. SDK text content and
  legacy duck-typed content keep their existing behavior: return the first
  text attribute, including empty text or an explicit `None`; otherwise return
  the structured content list. Empty results remain the original result.
  Images, audio, resource links and embedded resources are not coerced to text.
- Validate refresh transport before OAuth. Missing/unsupported transport now
  returns `False` without starting authentication or replacing the client.
  Valid refreshes retain transport, timeout, command/environment, copied headers
  and the existing unattended settings. The one-retry policy is unchanged.
- Resolve null/empty OAuth callback configuration to the existing fallback.
  Initial connection keeps its `127.0.0.1` fallback; unattended refresh keeps
  its `localhost` fallback. Explicit caller configuration still wins over server
  configuration. The resolved initial address is also saved for later refresh.

Dynamic method/schema generation, optional argument omission, exception
flattening, content ordering and transport implementations are unchanged.

## Verification

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest tests/test_mcp packages/nooa-acp/tests packages/nooa-cli/tests -q --junitxml=logs/typing-mcp-20261002-regression.xml
```

| Runtime check | Passed | Skipped | Xfailed | Time |
| --- | ---: | ---: | ---: | ---: |
| Existing MCP baseline | 147 | 0 | 0 | 27.99 s |
| MCP, ACP and CLI regression | 635 | 2 | 3 | 176.16 s |
| Final new-contract rerun | 18 | 0 | 0 | 10.54 s |

All final runs exited with code 0. The new eighteen cases are included in the
635-case regression count. No skip/xfail declarations or test markers were
changed.

New contracts cover mixed SDK content, empty/legacy text, structured non-text
results, session exit before result projection, omitted `None` arguments,
invalid refresh transports without OAuth, all supported transport values,
timeout/header preservation and callback-address fallback/precedence after a
simulated 401. Existing tests include local MCP transports, discovery, OAuth
flows, generated tool methods, ACP integration and CLI behavior.

No real external authorization or paid model inference was performed. This
batch did not run the entire repository runtime suite, native LPAC acceptance,
installed-wheel acceptance, or an extra Python/platform matrix. Public Windows
sandbox startup remains disabled.

Repository Ruff lint, formatting (1089 files), explicit-encoding checks and
license headers (1092 source Python files) pass. Final `git diff --check` passes.

Evidence uses the prefix `logs/typing-mcp-20261002-`:

- `before.json`, `scoped.json`, `project.json`: Pyright reports.
- `baseline.xml` / `.log`: existing MCP tests.
- `contracts.xml`: final eighteen-case boundary suite.
- `regression.xml` / `.log`: MCP, ACP and CLI regression.

The preceding `logs/typing-summary-20261002-project.json` is preserved for
comparison. Nothing was committed or pushed.

## Remaining Work

Follow-up: the [agentdoc batch](typing-agentdoc-20261002.md) clears the eight
errors in the documentation boundary described below.

The repository has 29 errors and one warning. A next related batch is agentdoc:
three errors in `_docs.py`, two in `_discover.py` and three in the Plotly
adapter. The eight runtime lock-loop test errors are separate test typing
debt. Neither area was changed in this batch.
