# Interactive typing verification (2026-10-02)

## Scope

Continue from [skill registry typing](typing-skill-registry-20261002.md) on
local Python 3.12 with uv. Preserve unrelated worktree changes, including the
previous summarization typing fixes.

- Declare the host renderer as Callable[..., None], reflecting the existing
  modern keyword-metadata and legacy text-only calling conventions. Dispatch
  and its existing TypeError fallback are unchanged. This annotation does not
  statically validate a host callback's full signature.
- Add context_budget overloads: an integer fallback (including the default)
  guarantees int, while an optional fallback retains int | None. Calculation,
  fallback values, validation and public runtime parameters are unchanged.
- Three new tests cover inferred budget types using assert_type, unknown and
  known model windows, and both renderer signatures with recorded AgentMessage
  identity and metadata. Test agents are closed in finally blocks.

No casts, suppressions, dependency changes or typing exclusions were added.

## Verification

Repository Pyright: **5 errors -> 2 errors**, one warning unchanged, 317 files.
Comparison by diagnostic file and message with
`logs/typing-skill-registry-20261002-project.json` shows exactly three removed
diagnostics and none added. Scoped Pyright checks interactive, summarization
and the new test file: zero errors and warnings.

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
uv run --no-sync python -m pytest tests/test_interactive_typing.py tests/test_interactive_agent.py tests/agents tests/runtime/test_effective_context_limits.py tests/unit/test_pragma_replacements.py -q --junitxml=logs/typing-interactive-20261002-regression.xml
```

Result: **231 passed, 2 skipped**, no warnings, 10.69 seconds, exit code 0.
Reports use prefix `logs/typing-interactive-20261002-`:
`project.json`, `scoped.json`, `regression.log` and `regression.xml`.

Repository Ruff lint, formatting (1094 files), explicit-encoding checks and
SPDX headers (1097 Python files) pass. `git diff --check` passes.

No full repository/runtime suite, native LPAC acceptance, installed-wheel
acceptance, live model calls or additional Python/platform matrix was run.
Public Windows sandbox startup remains disabled. Nothing was committed or pushed.

## Remaining Work

Two errors remain: bench runner's instance-value access and the predict output
serialization test's response constructor. The package export warning remains.
The next bounded batch can address these final two errors and inspect the warning.

Follow-up: [final typing verification](typing-final-20261002.md) clears both
errors and the export warning. Repository Pyright reports zero diagnostics.
