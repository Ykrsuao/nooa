# Skill registry typing verification (2026-10-02)

## Scope

Continue from [limits and reasoning typing](typing-limits-reasoning-20261002.md)
using local Python 3.12 and uv. Preserve unrelated worktree changes.

Python-source reload now checks that the previous module name is non-None
before removing it from the registry's string-keyed module map. The existing
identity check still protects sys.modules entries replaced by third parties.
Missing source module names skip old-module cleanup; new module tracking and
normal registry shutdown remain unchanged. No casts or suppressions were added.

Three new tests perform actual temporary-file discovery, reload and close for
missing, owned and externally replaced old module names. They verify fresh
skill identity, old object usability, new module tracking, shutdown cleanup and
preservation of third-party replacements. Test-created replacement modules are
removed in finally blocks.

## Verification

Repository Pyright: **7 errors -> 5 errors**, one warning unchanged, 317 files.
Comparison by diagnostic file and message with
`logs/typing-limits-reasoning-20261002-project.json` shows exactly two removed
diagnostics and none added. Scoped Pyright checks the registry and new test
file: zero errors and warnings.

The regression run used:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
uv run --no-sync python -m pytest tests/test_skill_registry.py tests/test_skill_registry_extended.py tests/test_skill_registry_typing.py tests/tools/test_single_module_skill_reload.py tests/tools/test_skill.py tests/unit/test_skill.py tests/unit/test_library_as_skill.py tests/agentdoc/test_skill_lifecycle_visibility.py tests/strategies/test_skills_section_context_events.py tests/test_skill_frontmatter.py -q --junitxml=logs/typing-skill-registry-20261002-regression.xml
```

Result: **148 passed**, no skips or warnings, 13.73 seconds, exit code 0.
Reports use prefix `logs/typing-skill-registry-20261002-`:
`project.json`, `scoped.json`, `regression.log` and `regression.xml`.

Repository Ruff lint, formatting (1093 files), explicit-encoding checks and
SPDX headers (1096 Python files) pass. `git diff --check` passes.

No full repository/runtime suite, native LPAC acceptance, installed-wheel
acceptance, live model calls or additional Python/platform matrix was run.
Public Windows sandbox startup remains disabled. Nothing was committed or pushed.

## Remaining Work

Five errors remain: interactive (3), bench runner (1), and predict serialization
test (1). The package export warning remains. The next bounded batch can address
the interactive context-budget and event-update contracts.

Follow-up: [interactive typing](typing-interactive-20261002.md) clears the three
errors (budget and renderer callback contracts), leaving two errors and one warning.
