# ACP sandbox integration verification — 2026-10-03

This change completes the two approved stages: a common native session entry,
then ACP integration with explicit workspace capabilities and lifecycle cleanup.
It keeps the previous Windows fixes in the working tree. No commit or push was
made. The operating guide is [ACP native sandbox](acp-sandbox.md).

## Delivered behavior

1. Mapped generated Python, file tools, shell commands and workspace startup
   imports. The sandbox ACP branch does not construct the ordinary coding agent.
2. Added `SandboxSession`, selecting Linux or Windows without translating native
   policies or silently falling back to host execution.
3. Made parent callbacks explicit. Restricted Linux workers reject nested,
   private and ungranted broker requests before decoding their arguments.
4. Verified native worker isolation and sequential session ownership. Linux
   restricted workers close inherited host descriptors and isolate ACP stdio.
5. Added ACP `--sandbox off|auto|linux|windows` and `NOOA_ACP_SANDBOX`.
6. Added pinned workspace file operations and isolated commands in disposable
   snapshots. Commands do not copy changes back; file tools persist edits.
7. Connected session new/load/cancel/close and retained failed cleanup owners.
   Sandbox session storage is outside the granted workspace.
8. Exercised real ACP stdio with a deterministic model, real native workers,
   persistent edits, private commands, cancellation/reuse and transcript restore.

The default remains `off`; enable the feature with `--sandbox auto`. Sandbox
mode does not load workspace Python skills/libraries/settings or forwarded MCP
servers. macOS has no backend and rejects requests explicitly. No Mac hardware
or Mac execution was used.

## Acceptance runs

All runs use Python 3.12 through `uv`. Windows uses the existing native virtual
environment (3.12.13). Ubuntu WSL uses a separate virtual environment (3.12.3)
and separate bytecode cache. No live/paid model is required by these tests.

| Scope | Environment | Result | Evidence |
| --- | --- | --- | --- |
| ACP, CLI, native sandbox agent, file tools, activity, platform command runner, common session API | Windows | 198 passed; 1 platform skip; 3 existing expected failures; 2 deselected | `logs/acp-final-windows-20261003.xml` |
| Existing native LPAC files/directories and AppContainer controls | Windows | 147 passed; 5 deselected | `logs/acp-native-regressions-windows-20261003.xml` |
| ACP, CLI, native sandbox agent, file tools, activity, Linux commands, common session API | Ubuntu WSL | 199 passed; 3 existing expected failures; 7 deselected | `logs/acp-final-linux-20261003.xml` |
| Final filter revision: real ACP including a turn after restore, native agent, command isolation, inherited descriptors and syscall ABI probes | Ubuntu WSL | 29 passed | `logs/acp-final-linux-containment-20261003.xml` |
| Independent review: scoped descriptors, commands, common facade and syscall ABI | Ubuntu WSL | 63 passed | `logs/linux-independent-review-20261003.xml` |
| Final real ACP lifecycle, complete replay and another generation after restore | Windows | 1 passed | `logs/acp-sandbox-protocol-windows-20261003.xml` |
| Same final protocol test with notification waits and post-restore generation | Ubuntu WSL | 1 passed | `logs/acp-sandbox-protocol-linux-20261003.xml` |

The broad ACP runs include the real protocol test. Native containment tests
marked `sandbox` are selected separately. Deselections in the AppContainer run
exclude abrupt-owner-exit scenarios and an older full Agent staging test;
direct child-process bans, atomic Job membership, environment isolation,
ungranted file/network access, normal cleanup and broker regressions ran.

The final protocol test waits for both complete replay messages before sending
the next prompt. ACP SDK notification callbacks are scheduled independently of
request responses, so an earlier immediate assertion after `load_session`
occasionally inspected the client before callbacks ran. The corrected test
drains original replies first, waits for both replayed replies, and verifies
another successful native turn after restore. No persistence change was needed.

The final Linux tests cover inherited file/socket descriptors, raw ACP standard
streams, unauthorized callbacks, socketpair redirection, indirect signals via
file notifications, changes to another process's limits, namespace clone
escapes, foreign syscall architectures and x32. Positive controls retain
ordinary threads, asyncio and worker/session reuse. Independent isolated
before/after probes confirmed the observed host-signal and resource-limit
bypasses were rejected after the fixes. Expected Python 3.12 warnings about
forking a multithreaded host remain visible; the backend architecture was not
replaced in this task.

Repository Pyright reports zero errors and warnings. Ruff checks and formatting,
SPDX headers, UTF-8 decoding of changed source/documents and `git diff --check`
pass. CLI help exposes the new selection and environment variable.

## Limits of the evidence

This is source-checkout acceptance on one Windows machine and its WSL Linux
environment. It does not constitute a new installed-wheel matrix, full-repo
test rerun, live-provider validation, macOS support or verification on every
Linux kernel/architecture. Earlier broader Windows verification remains
recorded separately in [Windows fixes](windows-review-fixes-20261003.md).

Linux Python cells retain the underlying fork memory limitation; there is no
claim that copied host credentials are erased from memory. Linux command
resources have per-process limits and process-group cleanup, without cgroup
quotas or a PID namespace. Windows commands use a dedicated LPAC profile and
Job Object while ordinary CodeAct LPAC keeps its child-process ban. See the
operating guide for snapshot limits, dependencies and persistent edit behavior.
