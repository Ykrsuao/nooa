# Connect Typing: 2026-10-02

## Scope and Baseline

This follows the [response/replay typing batch](typing-response-replay-20261002.md).
Verification uses Windows Python 3.12.13 and Pyright 1.1.411. Existing uncommitted
changes are preserved. Dependencies, lockfile, Python support and Pyright
configuration are unchanged.

The scoped baseline reproduced all 38 Connect errors: 30 in `__init__.py` and
eight in `_session.py`. The four Connect production modules now pass, together
with the new contract test file, with zero errors and warnings.

| Repository check | Files analyzed | Errors | Warnings |
| --- | ---: | ---: | ---: |
| Previous batch | 316 | 137 | 1 |
| After Connect fixes | 316 | 99 | 1 |

No new file/message diagnostic pairs were introduced. The pre-existing CLI
source exclusion is unchanged; this is not a clean whole-checkout type result.
No new diagnostic suppressions or casts were added.

Reports:

- `logs/typing-connect-20261002-before.json`: scoped baseline.
- `logs/typing-connect-20261002-scoped.json`: four modules plus new tests.
- `logs/typing-connect-20261002-project.json`: remaining repository debt.
- `logs/typing-20261002-final.json`: preserved pre-Connect repository baseline.

## Changes

- Annotate discovery/metadata dictionaries with their heterogeneous value
  contract, preserving integer limits and nested provenance when no catalogue
  metadata was supplied.
- Use `ProbeRecord` for produced, reused and session records. Preserve `None`
  for unavailable state/settings evidence, rather than converting it to false.
  Declare internal `source` and `include_rejected` fields while explicitly
  excluding them, and `request`, from `public_record`. The diagnostic field
  allowlist therefore remains unchanged.
- Add shape-preserving scrubber overloads and a typed timeout record. Strings
  remain strings, dictionaries remain dictionaries, and sequences retain the
  existing list projection. Credential redaction and detached containers are
  unchanged.
- Initialize captured reasoning settings before hook construction. Preserve
  the observed HTTP status in exception metadata independently of any status
  synthesized by the SDK. Only an available integer status can produce an
  encrypted-reasoning rejection record.
- Declare the three progress streams as `AsyncGenerator`, including their
  close operation. Type replay history without cache-boundary objects, and
  make the successfully completed seed-turn invariant explicit.
- Close an owned client whose HTTP transport is missing before refusing
  dispatch. Ordinary probes raise a controlled error; session checks report
  `not_confirmed`. Neither path sends an uninstrumented request.

The last item fixes a runtime cleanup gap exposed while examining the optional
transport diagnostics. Before the fix, both paths raised `AttributeError`
before reaching client cleanup. The two regression cases failed before the
production edits and pass afterward.

Budget reservations, configured reply caps, approval gates, lack of network
retries, request reuse, cache/reasoning replay and sanitized error reporting
remain intact. Model-provided tools are never executed by Connect.

## Verification

All runtime checks use the separate Windows cache from the prior batch:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/typing-py312-cache"
uv run --no-sync python -m pytest tests/unifiedllm packages/nooa-cli/tests -q --junitxml=logs/typing-connect-20261002-regression.xml
```

| Scope | Passed | Failed | Skipped | Deselected | Time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing Connect baseline | 246 | 0 | 1 | 0 | 33.64 s |
| New contracts before fixes | 6 | 2 | 0 | 0 | 4.97 s |
| UnifiedLLM and all CLI tests after fixes | 1762 | 0 | 3 | 5 | 95.08 s |

The new eight-case contract suite covers:

- Unknown evidence and exclusion of internal diagnostic fields.
- Scrubber container types, credential redaction and input ownership.
- Closing progress generators before dispatch.
- Missing HTTP transports, no dispatch and exactly one client close.
- Actual runtime clients on success, HTTP 500 and cancellation: hooks are
  removed, clients close and the observed HTTP status is retained.

The broader suite also exercises configured wire bodies, endpoint discovery,
encrypted-reasoning fallback, budget/retry bounds, earlier-result reuse,
multi-turn replay, malformed responses, timeouts and CLI diagnostic output.
Provider requests are mocked below the real SDKs; no paid/live inference was
performed. Existing platform/dependency skips and marker exclusions remain.

Runtime reports and transcripts use the prefix
`logs/typing-connect-20261002-`, followed by `baseline`, `contracts-before`,
`contracts` and `regression`. The new typing assertions were also observed
failing before the fixes and now pass in the scoped check.

Repository Ruff lint, formatting (1084 files), explicit-encoding checks and
license headers (1087 source Python files) pass.

## Remaining Work

Follow-up: the [memory/Viewer typing batch](typing-memory-20261002.md) clears
the 32-error optional memory boundary described below.

Repository typing still has 99 errors and one warning. A useful next batch is
the optional memory/Viewer boundary: 22 errors in the memory package and ten
in Viewer memory routes. Native sandbox guards/wire types account for another
22; other runtime, agent and documentation interfaces remain separate work.

This batch did not repeat the full source suite, installed-wheel acceptance,
Linux sandbox execution or a Python-version matrix. Prior acceptance results
apply only to their recorded revisions. Public Windows sandbox launch remains
disabled, and nothing was committed or pushed.
