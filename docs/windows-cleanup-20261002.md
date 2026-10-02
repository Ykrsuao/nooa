# Windows native cleanup follow-up (2026-10-02)

Later public-entry implementation and acceptance are recorded in
[public Windows session acceptance](windows-public-entry-20261002.md).
The release boundary below describes the code at this cleanup run.

## Finding and change

The previous full installed run passed 586 tests after an initial HTTPS test
timeout in `shutil.rmtree`. This follow-up addresses the measured cleanup
bottleneck; it does not claim to reproduce that original intermittent timeout.
All local runs use Python 3.12.13.

A runtime-only diagnostic staged the actual dependency closure and measured
8,318 file deletions. Cleanup took 57.569 seconds, of which 56.705 seconds
were spent in `os.unlink`; AppContainer profile deletion took 0.009 seconds.
The largest contributors were litellm (3,385 files, 20.533 seconds) and openai
(1,257 files, 10.019 seconds). This isolates serial file deletion from Agent
execution, HTTPS cancellation and profile teardown.

`_windows_cleanup._remove_tree` now removes independent subtrees with at most
four workers and 32 queued/in-flight futures. It partitions only the first
four directory levels, then delegates deeper subtrees to `shutil.rmtree`.
Ordinary runtime close and the committed recovery path share this helper.
Every worker is joined before return, including on failure. Parent directories
are removed only after successful child deletion. Ownership validation,
recovery pins, native final-root deletion and retry state remain in place.
Partition-boundary directory junctions are removed with `os.rmdir` without
enumerating their targets. All I/O uses extended Windows path spelling.

## Measurements

| Scenario | Before | After |
| --- | ---: | ---: |
| Runtime-only cleanup, per-file diagnostic enabled | 57.569 s | 33.649 s |
| Real HTTPS deadline/cancellation test: cleanup | 44.375 s | 25.232 s |
| Real HTTPS deadline/cancellation test: test call | 130.73 s | 84.81 s |

The runtime-only comparison reduces cleanup elapsed time by about 42%; the
actual HTTPS test reduces it by about 43%. These are individual local samples,
not percentile measurements or a guarantee under every storage condition.
Setup/cache state differed between runs, so total test improvement must not
be attributed solely to cleanup. Parallel per-file durations overlap and
must not be summed as elapsed wall time. The after tree contains one additional
file, the new cleanup module (8,319 files total).

The original HTTPS scenario passes under its unchanged 180-second deadline.
Its source diagnostic reports 1 passed in 85.24 seconds, with the existing
pytest assertion-rewrite warning from importing the framework before pytest.
No warning filters were changed.

## Regression and installed acceptance

Fourteen focused tests passed in 4.22 seconds: Unicode/long paths, empty trees,
junctions on both sides of partition boundaries, bounded overlapping deletion,
joining workers on failure, ordinary-close retry, root substitution refusal,
and partial recovery deletion retaining a live lease for retry.

The new cleanup test file is included in `scripts/smoke_install.py` so future
complete installed runs exercise it automatically. Selected installed-wheel
acceptance passed **198 tests**, with 60 deliberately deselected and no
failures, errors or skips, in 564.61 seconds. All five wheels were rebuilt,
installed outside the checkout, and tested using the installed interpreter's
`-I` with a copied self-contained suite. The runner exited with code 0 and
printed `Selected clean-install acceptance passed.` Its temporary directory
`C:/Users/QinGu/AppData/Local/Temp/nooa-install-vhaatgrf` was confirmed absent.

The selection covers installed-package provenance, the complete cleanup,
AppContainer, recovery, Windows session and public API files, and both real
HTTPS deadline/cancellation cases. The latter took 83.37 seconds (broker
deadline) and 75.06 seconds (HTTPS deadline), below the unchanged 180-second
deadline. The persistent recovery framework case passed in 77.30 seconds;
all four real managed session cases also passed, including staged application
recovery and writable workspace persistence.

Reproduce the installed selection from the repository root:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTHONUNBUFFERED = '1'
$env:PYTEST_ADDOPTS = '-k "test_windows_cleanup or test_lpac_recovery or test_appcontainer or test_windows_session or test_windows_api or managed_https_deadlines_cancellation_and_new_calls or imports_are_from_installed_wheels" --durations=10 --junitxml=E:/rivon/labs-OO-Agents/logs/windows-cleanup-installed-20261002.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12 --test-file test_windows_cleanup.py --test-file test_lpac_recovery.py --test-file test_appcontainer.py --test-file test_windows_session.py --test-file test_windows_api.py --test-file test_lpac_brokers.py
```

Diagnostic-only scripts and measurements remain under `logs/`:

- `profile_windows_cleanup.py`
- `windows-cleanup-breakdown-20261002.log`
- `windows-cleanup-parallel-20261002.log`
- `windows-cleanup-baseline-20261002.log` and `.xml`
- `windows-cleanup-https-20261002.log` and `.xml`
- `windows-cleanup-installed-20261002.log` and `.xml`

The changed runtime modules pass Pyright with zero errors and warnings.
All seven changed/new Python files pass Ruff lint and formatting, and
`git diff --check` passes. No diagnostic instrumentation was added to packaged
runtime code.

## Release boundary

Public Windows session launch remains disabled. The earlier 586-test full
pass applies to the pre-optimization code; this follow-up uses a targeted
installed selection and must not be presented as another full-matrix pass.
The next release step is actual public-entry acceptance without replacing
the public launch gate. No test deadlines, grants or dependency versions
were changed, and no live model calls, commit or push were performed.
