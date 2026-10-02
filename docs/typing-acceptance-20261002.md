# Post-typing source acceptance (2026-10-02)

## Scope

Follow up [typing completion](typing-final-20261002.md) with a combined source
regression on Windows Python 3.12.13. This run verifies the accumulated worktree
changes together, not just the latest batch. No implementation, test assertion,
timeout, marker, dependency or security-policy changes were made in this batch.

Default pytest configuration excludes integration, stress and sandbox markers.
The complete `tests/runtime/sandbox` directory is additionally ignored to avoid
unmarked native LPAC acceptance. Directory names alone are not marker filters;
unmarked offline tests under `tests/integration` still run.

## Results

| Selection | Passed | Skipped | Deselected | Xfailed | Warnings | Time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Default source | 9176 | 35 | 256 | 3 | 19 | 537.63 s |
| Embedded tests and memory | 514 | 14 | 1 | 0 | 1 | 23.23 s |

Both runs exited with code 0, with no unexpected failures. The default source
selection collected 9470 cases and selected 9214. The embedded/memory selection
covers test locations omitted by the default testpaths, including embedded
agentdoc tests.

The three strict ACP xfails remain the documented external-package lifecycle
gap: conflicting packaged skills and shared-checkout ownership until the final
session closes. They are not passing tests and were not relabeled or removed.
See `packages/nooa-acp/tests/test_server.py` for the existing markers.

Default skips include platform-only behavior, unavailable symlink privileges,
optional Tree-sitter support, external credentials/live-model prerequisites
and installed-package provenance. Memory skips retain optional backend/service
requirements. Warnings include deprecated context/pytest/Starlette/websocket
APIs and a trace-explorer time-range fallback warning. No warning filters were
changed to obtain these results.

Fresh repository Pyright: **318 files, zero errors and zero warnings**.
Ruff lint, formatting (1096 files), explicit-encoding checks and SPDX headers
(1099 source Python files) pass. `git diff --check` passes.

## Reproduction

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
uv run --no-sync python -m pytest --ignore=tests/runtime/sandbox -q --junitxml=logs/typing-acceptance-20261002-source.xml
uv run --no-sync python -m pytest src/nooa/runtime/tests src/nooa/strategies/tests src/nooa/tools/tests src/nooa/agentdoc/tests packages/nooa-memory/tests -q --junitxml=logs/typing-acceptance-20261002-embedded-memory.xml
uv run --no-sync pyright --outputjson
```

Reports use prefix `logs/typing-acceptance-20261002-`: `source.log`, `source.xml`,
`embedded-memory.log`, `embedded-memory.xml`, and `project.json`.

## Remaining Boundaries

This is default source acceptance, not an unrestricted all-tests run. Native
LPAC, rebuilt-wheel acceptance, the separately configured evaluation pipeline,
live provider validation and additional Python/platform matrices were not run.
Earlier Viewer clean-wheel results are documented in
[Viewer startup verification](viewer-startup-20261002.md); they are not new
installed-wheel evidence for the accumulated typing changes.

Public Windows sandbox startup remains disabled. All pre-existing worktree
changes are preserved. Nothing was committed or pushed.

Follow-up: [current clean-install workflows](typing-wheel-20261002.md) rebuilds
all five wheels and passes all twelve offline installed workflows on Python
3.12.13. Native tests remain outside that follow-up's scope.
