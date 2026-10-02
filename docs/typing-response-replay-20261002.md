# Response and Replay Typing: 2026-10-02

Connect follow-up: the [next typing batch](typing-connect-20261002.md) clears
the 38 Connect errors recorded here and leaves 99 repository errors.

## Baseline and Scope

This follows the [Viewer startup fix](viewer-startup-20261002.md) and the
[upstream integration](upstream-integration-20261001.md). Local verification
uses Windows Python 3.12.13 and Pyright 1.1.411. The existing checkout, Python
support declaration, dependencies, lockfile and Pyright configuration are
unchanged.

The first batch addresses the response, native JSON and request-projection
contracts. It does not attempt to clear unrelated Connect, sandbox, memory,
Viewer or agent-runtime typing debt.

| Repository check | Files analyzed | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Reproduced baseline | 316 | 192 | 1 |
| After this batch | 316 | 137 | 1 |

Reports: `logs/typing-20261002-baseline.json` and
`logs/typing-20261002-final.json`. Comparing file/message pairs finds no new
diagnostics. The existing CLI exclusion remains in place; this is not a claim
that every Python file in the checkout has been type checked.

## Changes

- Give `freeze` and `json_containers` shape-preserving overloads: objects stay
  objects, arrays change between tuple/list, and scalar leaves retain their
  types. Keep the runtime JSON validation, deep immutability and shared scalar
  ownership unchanged. Opaque provider fields retain their existing dynamic
  value contract.
- Type the response's mapping views and `model_copy` result, preserve the
  registered virtual `Mapping` protocol, and invalidate a copied cached
  projection through `vars(result)`. Public `dict(response)` remains distinct
  from the durable `model_dump()` archive.
- Retain two explicitly documented, line-specific
  `reportIncompatibleMethodOverride` exceptions for `__iter__`: these public
  mappings intentionally iterate keys, whereas Pydantic's base iterator yields
  field/value pairs. Changing that behavior would break the established public
  contract. No file-wide or repository-wide diagnostic rule is disabled.
- Narrow captured reasoning parts before reading their text.
- Build Fake client responses directly with ordered `AssistantText`,
  `AssistantReasoning` and `ToolCall` parts. Preserve empty-text parts, empty
  reasoning behavior, ordering, usage and finish reasons; legacy flat archive
  validation remains supported.
- Declare read-only token accounting and tool-call scans with `Sequence`, and
  keep projected request messages in a separate local variable.
- Remove the earlier, shadowed `ResponsesClient._prepare_call_config`
  definition. The later implementation that was already effective at runtime
  is unchanged.

These changes remove 55 repository diagnostics, including the two intentional
iterator compatibility exceptions above. `response_parts.py` becomes clean
through the shared JSON types without requiring an edit.

## Regression Coverage

New tests check native object/array shape with `typing.assert_type`, borrowed
scalar identity, detached request containers, rejection of non-JSON values,
Fake factory parity with legacy validation in sync and async calls, and cached
public views after shallow/deep part edits.

Before production edits, 48 focused runtime contract tests passed while the new
JSON typing test failed with 23 diagnostics. After the fixes, the focused suite
including cache-boundary and ordered-turn projection tests passed all 143 cases.
Reports are `logs/typing-20261002-contracts-before.xml` and
`logs/typing-20261002-focused.xml`.

Scoped Pyright checks six production modules and the three changed test files:
zero errors and zero warnings in `logs/typing-20261002-scoped.json`.
Deliberately invalid writes in the Fake transcript tests go through dynamically
typed callers and still assert `TypeError` / `FrozenInstanceError`.

### Source Regression and Cache Isolation

The first default source run used the checkout's shared bytecode cache and
finished with 9079 passed, 32 failed, 35 skipped and three expected failures.
The failure stacks referenced `/mnt/e/...` WSL paths on Windows, affecting
source inspection and ellipsis detection. The original failed report and log
are retained as `logs/typing-20261002-source-py312.xml` and `.log`.

Re-running the two initial agentdoc failure files with a separate Windows cache
passed all 109 tests without application-code changes. Reports:
`logs/typing-20261002-agentdoc-probe.xml` (two failures) and
`logs/typing-20261002-agentdoc-isolated.xml` (all passed).

The complete isolated-cache source run passed: 9111 passed, 35 skipped, three
existing expected failures, 256 deselected, and no failures or errors, in
648.19 seconds. It preserves the repository's default integration/stress/sandbox
marker exclusions and excludes `tests/runtime/sandbox`, matching the previous
source regression scope. The 32 failures from the shared-cache run are absent.
The source Viewer startup/assets/shutdown workflow passed in 7.623 seconds.
Reports: `logs/typing-20261002-source-isolated-py312.xml` and `.log`.

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest --ignore=tests/runtime/sandbox -q --junitxml=logs/typing-20261002-source-isolated-py312.xml
```

An additional run covering embedded runtime, strategies, tools and the memory
package passed 498 tests, with 14 dependency/platform skips and one deselection,
in 26.36 seconds. Report: `logs/typing-20261002-embedded-memory-py312.xml`.
The same Windows cache and Python 3.12 interpreter were used:

```powershell
uv run --no-sync python -m pytest src/nooa/runtime/tests src/nooa/strategies/tests src/nooa/tools/tests packages/nooa-memory/tests -q --junitxml=logs/typing-20261002-embedded-memory-py312.xml
```

No shared caches or existing logs were deleted. No test timeout, assertion or
sandbox admission rule was relaxed.

### Static Checks

Repository Ruff lint and explicit-encoding checks passed; Ruff formatting
checked 1083 files. The license-header checker passed for all 1086 source Python
files, and `git diff --check` passed. The separate repository-wide Pyright debt
above remains unresolved.

## Remaining Work

Repository typing is not yet clean: 137 errors and one warning remain.
The next concentrated area is Connect: 30 errors in `connect/__init__.py` and
eight in `connect/_session.py`. Other remaining groups include native sandbox
guards/wire types, optional memory integrations, Viewer memory routes and
summarization/runtime interfaces.

No new Python-version matrix or installed-wheel acceptance is claimed for this
batch. Public Windows sandbox launch remains disabled. Earlier installed-wheel
results remain evidence for their recorded source revisions only.
