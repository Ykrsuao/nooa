# Viewer Startup: 2026-10-02

## Diagnosis

Local development and acceptance use Windows Python 3.12.13, per the project's
Python 3.12 baseline. The earlier integration reports remain historical evidence;
this follow-up does not validate Python 3.13.

The existing `test_viewer_start_assets_and_stop` failed before modification with
its unchanged 30-second readiness deadline. The isolated acceptance invocation
used a Windows-specific bytecode cache:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
$env:PYTHONPYCACHEPREFIX = "E:/rivon/labs-OO-Agents/logs/viewer-py312-cache"
uv run --no-sync python -m pytest -c tests/acceptance/pytest.ini --confcutdir tests/acceptance tests/acceptance/test_installed_workflows.py -k viewer
```

`logs/viewer-20261002-baseline.xml` records the first failure (38.98 seconds
including collection). A second failure with temporary launcher instrumentation
is in `logs/viewer-20261002-probe.xml` (33.18 seconds). CLI discovery completed in
0.562 seconds; a 20-second faulthandler snapshot showed this import chain:

```text
start_dev.command
  nooa.viewer.main
    nooa.viewer.trace_routes
      nooa.unifiedllm.registry (package initialization)
        nooa.unifiedllm.fake
          nooa.unifiedllm.unifiedllm
            litellm -> provider modules -> jinja2 source compilation
```

The process had not reached SQLite initialization or Uvicorn startup. Importing
the registry helper eagerly initialized the entire model-client package, even
though displaying traces does not require inference. Temporary launcher timing
and traceback instrumentation were removed after diagnosis.

## Fix and Regression Coverage

Move `resolve_api_key_from_config` into its existing `if model_config` branch in
`run_inference`. The resolver, environment-variable allowlist, endpoint handling
and model-client behavior are unchanged; only import timing changes.

`tests/viewer/test_startup.py` imports the real Viewer application in a fresh
interpreter with LiteLLM imports blocked. It failed on the exact import chain
above before the fix and passes after it. This checks the dependency boundary
without relying on cache warmth or machine speed.

Two Playground regression cases exercise the real resolver with an offline
completion stub: an allowed environment variable supplies the configured key,
and a disallowed variable is not forwarded. Existing historical Python-tool
continuation tests also pass.

No startup timeout, health assertion, asset check, shutdown check, database
release check or Windows sandbox policy was relaxed.

## Verification

- Viewer plus offline workflow source regression: 167 passed, one provenance
  check skipped outside installed-wheel mode, two stress tests deselected;
  88.85 seconds. Report: `logs/viewer-20261002-regression.xml`.
- The source Viewer workflow, including startup, asset requests and shutdown,
  took 5.549 seconds. This is total test time, not a readiness-only measurement.
- Focused import and Playground regression: five passed in 5.39 seconds.
  Report: `logs/viewer-20261002-focused.xml`.

### Clean Wheels

Both runs rebuilt all five wheels as `0.0.11.dev356`, installed into separate
new environments outside the checkout, and verified installed package
provenance. Both use Windows Python 3.12.13 and the original acceptance
configuration, including its 30-second Viewer readiness deadline.

| Run | Passed | Failed | Deselected | Suite time | Viewer workflow time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full acceptance | 586 | 0 | 0 | 1994.48 s | 8.493 s |
| Fresh-install workflow repeat | 12 | 0 | 574 | 121.73 s | 8.384 s |

The full run includes all 574 native tests and all 12 installed workflows,
with no skips. The repeat selects only installed workflows. Viewer times
include health readiness, configuration, HTML/JS/CSS requests, shutdown,
port release and database rename. Neither run merely repeats a warm Viewer
process in the same installation.

Reports and command transcripts:

- `logs/viewer-20261002-wheel-py312.xml` and `.log`
- `logs/viewer-20261002-wheel-repeat-py312.xml` and `.log`

```powershell
$env:PYTEST_ADDOPTS = "--junitxml=E:/rivon/labs-OO-Agents/logs/viewer-20261002-wheel-py312.xml"
uv run --no-sync python scripts/smoke_install.py --python 3.12
$env:PYTEST_ADDOPTS = "-k test_installed_workflows --junitxml=E:/rivon/labs-OO-Agents/logs/viewer-20261002-wheel-repeat-py312.xml"
uv run --no-sync python scripts/smoke_install.py --python 3.12
```

The smoke runner cleans its temporary installations; the existing project
environment and diagnostic logs are retained. The two builds differ only
in formatting of the changed production file, not behavior.

### Static Checks and Scope

Changed Python files pass Ruff lint, formatting and explicit-encoding checks.
Targeted Pyright for `src/nooa/viewer/trace_routes.py` reports zero errors and
zero warnings. Repository-wide typing debt is outside this fix; no new
Python-version matrix is run. Public Windows sandbox launch remains disabled.
