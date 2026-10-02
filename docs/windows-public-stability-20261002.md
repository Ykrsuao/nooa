# Public Windows stability and full installed acceptance (2026-10-02)

## Scope

Continue the [public-entry acceptance](windows-public-entry-20261002.md) with
the existing bounded storage-contention and interruption suite, followed by
the complete installed matrix on native Windows Python 3.12.13.

The stability suite now imports `WindowsSandboxSession`, `WindowsSandboxPolicy`
and `FileGrant` from the public `nooa.runtime.sandbox.windows` module. Entry,
policy validation, runtime creation and cleanup run without admission overrides.
Observation wrappers retain the real runtime and worker objects; the two
interruption gates pause staging or close and then call the original operation.
No production code, grants, dependency versions or deadline budgets change.

The six native cases comprise one idle and three loaded complete lifecycles,
plus idle and loaded staging cancellation. Complete lifecycles exercise a staged
application and PyYAML, a worker timeout and replacement, retained workspace
files, host-file denial and granted broker writes, callback cancellation,
a successful subsequent call and repeated cancellation during close. Both
paths explicitly repeat `aclose()` after cleanup.

Two bounded I/O threads create, fsync, read and unlink files on the runtime's
volume. Each case retains the 512 MiB cumulative write cap, 64 KiB simultaneous
payload, 2 GiB free-space floor and 180-second deadline. Every loaded phase must
record real I/O cycles. The five fast checks cover the load generator's work,
ownership boundaries, low-space refusal, worker failure and write budget.
A sixth fast check verifies that a failed verification-profile close retains
ownership and succeeds on teardown retry without hiding the initial failure.

Resource auditing checks pinned worker process objects are signaled, native
process/thread and Job Object handles are released, streams are closed and
stderr threads have stopped. The original AppContainer profile's exact name
must be available for fresh creation; that verification profile is retained
until teardown and closed, including a retry if its first close fails.
The runtime, its recovery entry and the I/O pressure directory must be absent.
Deleting the granted output file confirms its broker handle no longer pins it.
Process-wide handle counts are auxiliary measurements; initialization and
shared pools make strict equality unsuitable as a leak assertion.

## Verification

The complete six-case source stress selection passed with no failures, errors
or skips in **517.99 seconds**. All six runtime/profile/recovery entries were
released and all 16 tracked workers exited. The loaded cases completed 17,975
real I/O cycles and wrote 561.72 MiB cumulatively across four cases; each
individual case remained below its 512 MiB cap.

| Case | Lifecycle seconds | I/O cycles | Workers |
| --- | ---: | ---: | ---: |
| Complete lifecycle, idle | 126.200 | 0 | 4 |
| Complete lifecycle, load 1 | 97.349 | 5,247 | 4 |
| Complete lifecycle, load 2 | 93.854 | 5,134 | 4 |
| Complete lifecycle, load 3 | 101.327 | 5,167 | 4 |
| Cancel provisioning, idle | 47.400 | 0 | 0 |
| Cancel provisioning, load | 47.169 | 2,427 | 0 |

Review then identified a cleanup gap in the newly added test audit: a failed
first close of its verification profile discarded the reference. The audit now
retains that object for teardown retry. This changes test cleanup only; an
initial verification failure still fails the case. All six fast checks passed
after the fix in **4.67 seconds**, including its failure-injection regression.
Both loaded native paths were repeated with this final audit: full lifecycle
and provisioning cancellation passed in **134.30 seconds**, with lifecycle
times of 89.981 and 40.892 seconds. Their additional four workers and two owned
runtimes/profiles/recovery entries were also released. The source evidence is
a complete six-case pass followed by this two-case verification of the final
test cleanup revision, not two complete runs.

Reports: `logs/windows-public-stability-20261002.log` and `.xml`,
`logs/windows-public-stability-final-20261002.log` and `.xml`, and
`logs/windows-public-stability-load-final-20261002.xml`.
The earlier five load-generator checks passed in 17.88 seconds, before the
audit cleanup regression was added; their report is
`logs/windows-public-stability-load-20261002.xml`.

Repository Ruff lint, formatting (1,103 files), explicit text encoding and SPDX
headers (1,106 files) passed. Configured Pyright analyzed 320 files with zero
errors and warnings; its report is
`logs/windows-public-stability-pyright-20261002.json`.

The complete installed suite passed **all 615 tests** in **1870.13 seconds
(31 minutes 10 seconds)**, with zero failures, errors, skips or deselections.
JUnit records 1870.086 seconds. The runner exited with code 0 and printed
`Clean-install acceptance passed.` All five wheels were rebuilt as
`0.0.11.dev356`; installed module provenance passed. The temporary installation
at `C:/Users/QinGu/AppData/Local/Temp/nooa-install-i_n6oyhc` was confirmed absent
after the runner exited.

All seven real public-session/managed-HTTPS cases passed through the actual
public entry. The slowest test call was the named directory-broker Agent at
112.32 seconds, below its unchanged 180-second limit. Public callback
cancellation/broker deadline took 84.36 seconds; managed HTTPS deadline cases
took 86.53 and 93.11 seconds. All offline installed workflows also passed,
including memory persistence and database release. No production source
changed between the stability runs and this rebuilt-wheel matrix.

The transcript is `logs/windows-public-full-20261002.log`; the complete JUnit
report is `logs/windows-public-full-20261002.xml`. This is one unfiltered
615-case pass on the current code, not the earlier 262-case public selection
or the historical 586-case full run.

| Review axis | Result |
| --- | --- |
| Standards | No actionable findings in the scoped public-entry/stability diff |
| Spec | One audit-profile cleanup finding, fixed and regression-tested; none open |

Review covered the public-session/prerequisite changes and this stability audit,
against the documented public-entry contract and continuation plan. It did not
review unrelated earlier typing changes. This follow-up changes the stability
test and documentation only. Final `git diff --check` passed. No additional
Python/platform matrix, live model calls, commit or push was performed.

## Reproduction

Run sequentially from the repository root to avoid interference between the
storage-pressure suite and installed acceptance:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
$env:PYTHONPYCACHEPREFIX = 'E:/rivon/labs-OO-Agents/logs/typing-py312-cache'
$env:PYTHONUNBUFFERED = '1'
$env:PYTEST_ADDOPTS = ''
uv run --no-sync python -m pytest tests/runtime/sandbox/test_windows_stability.py -m stress -q -s -o junit_family=xunit1 -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -W error::pytest.PytestUnhandledThreadExceptionWarning --durations=10 --junitxml=logs/windows-public-stability-20261002.xml
uv run --no-sync python -m pytest tests/runtime/sandbox/test_windows_stability.py -m 'not stress' -q -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -W error::pytest.PytestUnhandledThreadExceptionWarning --junitxml=logs/windows-public-stability-load-final-20261002.xml
uv run --no-sync python -m pytest tests/runtime/sandbox/test_windows_stability.py -m stress -k 'io-1 or (test_cancel_real_staging_under_io and not idle)' -q -s -o junit_family=xunit1 -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning -W error::pytest.PytestUnhandledThreadExceptionWarning --durations=5 --junitxml=logs/windows-public-stability-final-20261002.xml
$env:PYTEST_ADDOPTS = '--durations=15 --junitxml=E:/rivon/labs-OO-Agents/logs/windows-public-full-20261002.xml'
uv run --no-sync python scripts/smoke_install.py --python 3.12
```

The unchanged installer rebuilds all five wheels, installs outside the checkout,
copies the self-contained suite and runs the installed interpreter with `-I`.
The full run uses no `--test-file`, `-k` or `-m` selection. The stability suite
remains a separate opt-in source check and is not part of the installed matrix.
Resource, unraisable-exception and unhandled-thread warnings are errors in
both runs. The installer's full-suite timeout remains 3600 seconds.

## Limits

These finite runs cover the named scenarios and tracked owned resources. They
do not establish sustained saturation, disk-full behavior, arbitrary dependency
compatibility, untested Windows profile storage artifacts or every host cache.
The caller-owned empty recovery directory is retained. Earlier unowned resources
are not adopted or deleted. Host callbacks retain their explicitly granted
host authority. Automatic crash recovery and a Windows backend selector remain
separate implementation work.
