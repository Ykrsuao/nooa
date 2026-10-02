# Complete installed Windows native acceptance (2026-10-02)

Later implementation evidence: [cleanup optimization](windows-cleanup-20261002.md).
The 586-case result below describes the code before that optimization.
The subsequent [public-entry acceptance](windows-public-entry-20261002.md)
enables the explicit Windows session and verifies its actual installed entry;
the closed-gate statements below describe this earlier run.

## Scope

Continue the [189-case selection](windows-native-current-20261002.md) with
the complete `scripts/smoke_install.py` suite on Windows Python 3.12.13.
Each invocation rebuilds all five wheels, installs outside the checkout and
runs a copied, self-contained suite with the installed interpreter's `-I`.
The suite collects 586 cases: 574 cases from the 14 configured native test
files and 12 offline installed workflows. No source-tree pythonpath or root
conftest is used by the wheel runner.

No production code, grants, test assertions, warning filters, dependency
versions or timeout budgets were changed in this follow-up. The full-run
budget remains 3600 seconds and the HTTPS cases retain their 180-second
test deadline. Public Windows launch remains disabled.

## Initial Failure And Diagnostic Evidence

The first full invocation reported 115 passes before pytest-timeout terminated
the process during
`test_managed_https_deadlines_cancellation_and_new_calls[https]`.
The stack was in `_AppContainerPython.close()` -> `shutil.rmtree()` ->
`os.unlink()`, while the event loop waited. It does not show a blocked HTTPS
request at the time of termination. This invocation failed with exit code 1
and produced no final JUnit report; it is not a completed acceptance pass.
The transcript is `logs/windows-native-full-20261002-installed.log`.
Its temporary installation, `nooa-install-l9vure18`, was confirmed absent.

The same two parameterized tests were then run with timing wrappers around
runtime construction, framework staging and runtime close. The wrappers call
the original functions and do not change test assertions or deadlines.

| Environment / case | Runtime | Framework | Cleanup | Test call |
| --- | ---: | ---: | ---: | ---: |
| Source / broker deadline | 1.657 s | 29.943 s | 49.009 s | 137.61 s |
| Source / HTTPS deadline | 1.433 s | 10.412 s | 45.772 s | 102.93 s |
| Fresh wheels / broker deadline | 1.436 s | 28.253 s | 46.317 s | 131.74 s |
| Fresh wheels / HTTPS deadline | 1.451 s | 11.746 s | 46.104 s | 104.31 s |

Source diagnostics passed both cases in 241.27 seconds, with one warning.
Installed diagnostics passed both cases plus package provenance (3 passed,
60 deselected) in 239.72 seconds. The installed diagnostic retained the wheel
runner's build, isolated interpreter, copied suite, warning policy and cleanup;
only the pytest entry was wrapped for timing. Its logged framework module
resolves inside the isolated environment's `site-packages`. The runner exited
successfully and its temporary installation was removed.

The failure did not reproduce in these smaller runs. Cleanup is a substantial
measured cost, but these timings cannot establish the original failure's exact
cause or prove that storage variability is resolved. No speculative runtime
change or deadline increase was made.

Diagnostic scripts remain explicitly under `logs/profile_windows_https.py`
and `logs/profile_installed_https.py`; no debug instrumentation was added to
packaged source. Their reports are `logs/windows-https-profile-20261002.log`
and `.xml`, and `logs/windows-https-installed-profile-20261002.log` and `.xml`.

## Complete Retry

The uninstrumented full-suite retry passed **all 586 cases**, with zero
failures, errors or skips, in 2364.81 seconds (39 minutes 25 seconds).
This is a single complete run, not an aggregate of partial selections.
The runner exited with code 0 and printed `Clean-install acceptance passed.`
Its temporary installation, `nooa-install-yjz1u20t`, was confirmed absent.
All five wheels were rebuilt as `0.0.11.dev356`.

| Selection | Passed | Failures / errors / skips |
| --- | ---: | ---: |
| Fourteen configured native files | 574 | 0 / 0 / 0 |
| Installed package and offline workflows | 12 | 0 / 0 / 0 |
| Total | 586 | 0 / 0 / 0 |

The formerly interrupted HTTPS case took 104.25 seconds. The slowest test
call was managed-session cancellation/broker timeout at 125.77 seconds,
below its unchanged 180-second deadline. All four real public-session
fixture cases passed, including staged application recovery and writable
workspace persistence. JUnit reports 2364.780 seconds; the pytest console
summary reports 2364.81 seconds.

This completes current-code installed native acceptance on Python 3.12.
It does not erase the initial timeout or establish stable performance under
all storage conditions. No runtime fix is claimed for the unreproduced delay.

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTHONUNBUFFERED = '1'
$env:PYTEST_ADDOPTS = '--durations=10 --junitxml=E:/rivon/labs-OO-Agents/logs/windows-native-full-20261002-retry.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12
```

The transcript is `logs/windows-native-full-20261002-retry.log`.
The final report is `logs/windows-native-full-20261002-retry.xml`.

## Public Release Boundary

The current public `WindowsSandboxSession.__aenter__` still refuses launch.
Four native session acceptance cases replace only `_require_public_launch`
in a test fixture; the separate API suite checks that the unmodified public
entry refuses before provisioning. These tests do not establish a supported
installed public launch, regardless of a full-matrix result.

The next implementation step is an explicit public-session release candidate:
exercise the actual installed entry without gate substitution, retain native
policy validation and paired allow/deny behavior, and validate lifecycle,
cancellation, recovery and cleanup through that entry. Capability and doctor
reporting must describe that supported path and its enforced policy. The
fork-based `SandboxConfig` is not a Windows policy translation.

Storage variability, interruption recovery and broader dependency coverage
remain operational work. The timeout above is evidence of that limitation,
not evidence that increasing a timeout makes it disappear. Automatic cleanup
must not adopt unidentified or uncommitted native resources.

Only local Python 3.12 was used; earlier Python 3.13 and Linux results are
historical evidence. No live model calls, commit or push was performed.
Both retained diagnostic scripts pass Ruff lint and formatting.
Final `git diff --check` passes.
