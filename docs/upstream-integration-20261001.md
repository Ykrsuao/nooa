# Upstream Integration: 2026-10-01

## Baselines and Recovery

- Previous base: `3374ceae9f1ba13ddbaceca752d8886d1d9dff3b`.
- Integrated upstream: `2ff4485e` (104 new reachable commits, 88 changed files).
- Working branch: `windows-native-phase1`.
- Recovery ref: `backup/windows-native-before-upstream-20261001`.
- Recovery commit: `fc3bee631215926064681fa93d34e269372056cd`.

The recovery ref retains the pre-integration stash, including its third parent
with all 56 previously untracked files. The stash is retained too. Logs,
credentials, ignored caches and virtual environments were not removed or
committed. `CLAUDE.md` remains a local file and is also recoverable from the
snapshot's third parent. Nothing was pushed to GitHub.

The branch fast-forwarded to the pinned upstream commit, then reapplied the
saved local adaptation. Of 240 locally modified tracked files, 29 overlapped
upstream and seven required textual conflict resolution. Comparison against
the snapshot confirmed that all 211 nonoverlapping tracked files were unchanged.
During conflict resolution, only the installed coding fixture among restored
new source/document/test files needed migration to the new turn-result API.
The Windows status document subsequently gained a link to this integration
record.

## Resolution Decisions

No whole-file preference for either side was used.

| Area | Decision and reason |
| --- | --- |
| Interactive and ACP | Adopt upstream `Done`, `NeedInput`, `Waiting`, dispatcher behavior and cancellation display. Migrate the locally added installed-workflow fixture instead of reviving removed `RespondResult`/`RespondReason` APIs. Keep local source-provenance, UTF-8 and concrete-type assertions. |
| SQLite | Keep upstream PID/hostname records, blanking on close/delete, `must_exist`, journal selection and pre-serialized snapshots. Keep local binary-mode lock files and `nooa._filelock` acquisition/release. Using upstream `fcntl` directly would break Windows; omitting upstream record blanking would misreport closed sessions. |
| SQLite tests | Preserve upstream owner-record, journal and threaded snapshot assertions. Use the cross-platform unlock function in the new suite and combine the hostname assertion with the local unlock in the existing lock regression. |
| Shell | Keep upstream `cwd` and persistent-shell `stdin` behavior. Retain local Windows path handling, explicit encoding, original line endings, stream closure and native process ownership. |
| Shell tests | Keep both upstream directory/state/status tests and local byte-exact LF/CRLF tests. Use the existing `PWD_COMMAND` and canonical slash paths in directory assertions; do not rewrite ordinary command output to make tests pass. |
| Connect | Adopt upstream compact saved configuration and improved reasoning/interface probes. Replace old persisted-provenance assertions with the upstream contract, while retaining local UTF-8, atomic replacement, original line endings and cross-platform sidecar locking. |
| Runtime and CodeAct | Keep upstream partial cancellation results, per-cell attribution and shared limit messages. Retain local copied-configuration validation, fail-closed backend admission and preservation of imports needed by staged workers. |
| Benchmark | Keep upstream ATIF export, cancelled-cell filtering and resolved trace endpoints. Retain local lazy imports, UTF-8 I/O, lifecycle cleanup and typed test fixtures. Put the optional ATIF import and its use in the same enabled branch for static analysis. |
| Examples | Keep upstream typed turn-result migration in ARC and optional Harbor ATIF configuration, together with local explicit encodings. |
| Root fixtures | Keep upstream mocked Connect pacing and local capability-based skips for fork-only sandbox tests. Native Windows tests remain independently runnable. |

The initial Windows core regression reproduced a collection failure from the
new SQLite test's unconditional `fcntl` import. That test was ported, not
skipped. The new directory tests run on Windows and Linux using the real shell.

Additional narrow typing corrections preserve the cache-mapping literal type,
check concrete channel-event types in tests, and expose the already-validated
native JSON outer-object invariant to the Responses adapter's checker.
No blanket type suppression or relaxed sandbox policy was added.

## Verification

Initial Windows Python 3.12 checks:

| Scope | Passed | Skipped | Expected failures | Elapsed |
| --- | ---: | ---: | ---: | ---: |
| SQLite, shell and cancellation | 143 | 1 | 0 | 36.90 s |
| ACP, benchmark and interactive results | 279 | 0 | 3 | 110.43 s |
| Selected Connect, reasoning, Responses, cache and history tests | 1186 | 1 | 0 | 68.16 s |

Reports are `logs/upstream-20261001-core-py312.xml`,
`logs/upstream-20261001-packages-py312.xml` and
`logs/upstream-20261001-llm-py312.xml`. The last selection deselected 551 tests;
it was not a complete project run. The three ACP expected failures predate this
integration and concern external skill-package ownership across sessions.

The refreshed Windows-target Pyright report is
`logs/upstream-20261001-pyright-verified.json`: 316 files, 192 errors, one warning.
The first merged check had 202 errors and one warning. Repository-wide typing
is still not clean and is not claimed as passed.

### Broader Regression

All runs below use the integrated source. "Default source" retains the repository
marker exclusions for integration, stress and sandbox tests, and additionally
excludes `tests/runtime/sandbox`; installed-wheel and Linux sandbox runs cover
the relevant native execution paths separately.

| Platform / Python | Scope | Passed | Skipped | Expected failures |
| --- | --- | ---: | ---: | ---: |
| Windows / 3.12.13 | Default source | 9084 | 35 | 3 |
| Windows / 3.13.12 | Default source | 9084 | 35 | 3 |
| Windows / 3.12.13 | Embedded runtime, strategies, tools and memory | 498 | 14 | 0 |
| Windows / 3.12.13 | Standalone evaluation pipeline | 309 | 47 | 0 |
| Windows / 3.13.12 | Embedded runtime, tools and memory | 466 | 14 | 0 |
| Windows / 3.13.12 | Embedded strategies | 32 | 0 | 0 |
| Windows / 3.13.12 | Standalone evaluation pipeline | 309 | 47 | 0 |
| Linux / 3.12.3 | SQLite, shell, cancellation, ACP, benchmark, interactive, channels and imports | 432 | 0 | 3 |
| Linux / 3.12.3 | Real sandbox executor and CodeAct | 52 | 0 | 0 |

Reports use the prefix `logs/upstream-20261001-`, followed by:
`source-py312.xml`, `source-py313.xml`, `embedded-memory-py312.xml`, `eval-py312.xml`,
`embedded-memory-py313.xml`, `embedded-strategies-py313.xml`, `eval-py313.xml`,
`source-linux.xml` and `sandbox-linux.xml`. The default source selection
deselected 256 tests; each embedded-memory selection deselected one test.
Optional memory vector backends and tests requiring external services remain
skipped where their dependencies or prerequisites are absent.

The evaluation pipeline was run from `util/eval_pipeline` with its own pytest
configuration. An initial combined collection with other workspace projects
failed because their distinct `tests` packages collided; the standalone runs
above passed without changing application code or excluding evaluation tests.
Windows and WSL runs use separate bytecode caches.

The scoped ACP/public-channel type check covers 16 files with zero errors or
warnings (`logs/upstream-20261001-acp-channels-pyright.json`).

### Rebuilt Wheels

Five packages (`nooa`, `nooa-cli`, `nooa-acp`, `nooa-memory`, `nooa-bench`) are
built as wheels and installed in a temporary environment outside the checkout.
The interpreter runs with `-I`; only self-contained acceptance tests are copied.
Package import provenance is explicitly checked, including the staged Windows
namespace. All four builds used version `0.0.11.dev355`; packaged source was
unchanged between these runs.

The first Python 3.12.13 full run completed in 2172.85 seconds with 585 passed
and one failure (`logs/upstream-20261001-wheel-py312.xml`). All 574 native cases
passed. The Viewer workflow exceeded its existing 30-second startup deadline
before health readiness while source regressions were running concurrently.
The exact same installed wheel passed that test twice independently, in
32.69 and 10.64 seconds total test-run time (including collection), without
source changes or deadline changes. Reports are
`logs/upstream-20261001-viewer-wheel-py312.xml` and
`logs/upstream-20261001-viewer-wheel-repeat-py312.xml`.
The initial full run is retained as a failure, not relabeled as green.

The Python 3.13.12 full run completed in 2025.68 seconds with 585 passed
and one failure (`logs/upstream-20261001-wheel-py313.xml`). Again all 574 native
cases passed, and the only failure was the Viewer's startup deadline.
Unlike the first 3.12 run, this run did not overlap source regression suites,
so concurrent source tests cannot fully explain the startup failures.
The same installed 3.13 wheel passed the standalone Viewer test in 11.75 seconds
(`logs/upstream-20261001-viewer-wheel-py313.xml`).

Both versions then received a new wheel build and a new installation, selecting
only the 12 installed workflows with `PYTEST_ADDOPTS=-k test_installed_workflows`:

| Python | Passed | Failed | Deselected native tests | Elapsed |
| --- | ---: | ---: | ---: | ---: |
| 3.12.13 | 12 | 0 | 574 | 141.47 s |
| 3.13.12 | 11 | 1 | 574 | 192.57 s |

These reports are `logs/upstream-20261001-workflows-wheel-py312.xml` and
`logs/upstream-20261001-workflows-wheel-py313.xml`. The second 3.13 cold
installation again exceeded the existing 30-second Viewer startup deadline,
this time before writing its startup banner. CLI entry points, doctor, ACP
editing/MCP/cancellation recovery, memory persistence and both benchmark
workflows passed in both versions.

**The Viewer cold-start failure is unresolved.** Standalone reruns passing do
not establish that a fresh installation reliably starts within the deadline.
The upstream interval did not change `src/nooa/viewer/main.py`,
`packages/nooa-cli/src/nooa_cli/commands/start_dev.py`, the root or CLI
`pyproject.toml`, or `uv.lock`; this does not rule out indirect effects from
other modules. No startup timeout, assertion, security policy or packaged code
was weakened to turn the failure green. The integration is not claimed to have
an entirely green installed acceptance suite.

All four temporary installation roots were removed by the smoke runner.
The original `.venv`, external verification environment and repository logs
were preserved.

The old wheel passes in `windows-sandbox-policy.md` are not evidence for this
new source revision.

### Final Static Checks

Repository Ruff lint and explicit-encoding checks passed. Ruff formatting
checked 1081 files; the repository license checker checked 1084 Python source
files, using its existing exemption for empty package markers. Changed YAML
files, conflict-marker checks and staged whitespace checks also passed.
The separate repository-wide Pyright debt recorded above remains unresolved.

## Release Boundary

Public Windows sandbox launch remains disabled. The integrated upstream changes
do not relax native grants or expose the staged backend. Native tests that
replace the launch gate remain tests of that explicitly staged path, not proof
of an enabled production entry point.
