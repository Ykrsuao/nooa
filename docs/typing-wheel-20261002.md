# Post-typing clean-install workflows (2026-10-02)

## Scope

Follow up [source acceptance](typing-acceptance-20261002.md) by rebuilding and
installing the current worktree outside the checkout. Use Windows Python
3.12.13 only. No application code, dependency declarations, tests, assertions,
timeouts or sandbox policy were changed for this run.

All five wheels were freshly built as `0.0.11.dev356`: `nooa`, `nooa-cli`,
`nooa-acp`, `nooa-memory` and `nooa-bench`. The unchanged version label is not
evidence of reused artifacts: the command transcript records all five builds
into this run's new temporary directory.

The existing smoke runner installs these wheels in a fresh environment under
the system temporary directory, with a Unicode/space-containing environment
path. It copies only self-contained acceptance tests, removes PYTHONPATH and
PYTHONHOME from the test environment, and invokes the installed interpreter
with `-I`. Import provenance checks confirm all five packages and the staged
Windows namespace resolve inside that installation, not the source checkout.

## Results

**12 passed, 0 failed, 0 skipped, 574 deselected**, 148.19 seconds of pytest time.
The whole smoke runner exited with code 0 after cleanup. The temporary root
`C:/Users/QinGu/AppData/Local/Temp/nooa-install-1w_094k5` no longer exists.

| Workflow | Result | Test time |
| --- | --- | ---: |
| Installed import provenance | Passed | 2.397 s |
| Four CLI entry points | Passed | 0.280-2.205 s each |
| Benchmark help import boundary | Passed | 0.368 s |
| Offline doctor and user-config isolation | Passed | 4.058 s |
| Viewer startup, assets, shutdown and resource release | Passed | 6.981 s |
| ACP editing, MCP, cancellation and recovery | Passed | 31.855 s |
| Memory cross-process persistence and database release | Passed | 39.331 s |
| Benchmark execution and export | Passed | 9.251 s |
| RLM benchmark execution and export | Passed | 8.437 s |

Times include the relevant test's setup and teardown, not just readiness.
Viewer retained the original 30-second readiness deadline. No standalone rerun
was needed to obtain a pass. Package installation/build/cleanup time is outside
the reported pytest duration.

## Reproduction

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTEST_ADDOPTS = '-k test_installed_workflows --junitxml=E:/rivon/labs-OO-Agents/logs/typing-wheel-20261002-workflows.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12
```

Full build/install/test transcript: `logs/typing-wheel-20261002-workflows.log`.
JUnit report: `logs/typing-wheel-20261002-workflows.xml`.
The environment settings above were confined to the execution shell.
The existing development environment and earlier reports were preserved.
Final `git diff --check` passes.

## Boundaries

This is installed offline workflow acceptance, not full native acceptance.
The 574 native cases were collected but deselected, not executed or counted as
passes. The staged Windows module was imported only for provenance validation;
public Windows sandbox startup remains disabled. No live provider calls,
additional Python/platform matrix, publication, commit or push was performed.
The three previously documented source ACP xfails remain outside these passing
workflow claims. Prior zero-diagnostic typing and broad source regression
results are linked above; this run adds current clean-install evidence.
