# Windows native sandbox v1 delivery (2026-10-02)

## Delivered scope

Windows native sandbox v1 is complete for the explicit
`WindowsSandboxSession` interface. Enter a session with a
`WindowsSandboxPolicy`, obtain `session.strategy()`, and bind that strategy to
Agent generation methods. See the [usage example](../README.md) and
the [policy contract](windows-sandbox-policy.md#public-windows-interface).

The session provides native LPAC isolation, explicit file/directory/HTTPS/tool
grants, native resource limits, sequential Agent calls, staged dependencies,
worker replacement, cancellation draining and retryable owned-resource cleanup.
The prerequisite probe and doctor report API availability without claiming
that their read-only check verifies containment.

The delivered source also includes the accumulated typing-contract fixes and
their regressions, plus Viewer startup and optional-memory import fixes. These
are grouped separately from Windows sandbox changes in the Git history.
The destination is [Ykrsuao/nooa](https://github.com/Ykrsuao/nooa), branch `main`.

## Accepted evidence

All local verification below uses native Windows Python 3.12.13. These are the
completed runs recorded during implementation; delivery preparation does not
claim a new full-suite run. Historical reports retain their original code scope.

| Verification | Result | Record |
| --- | --- | --- |
| Accumulated typing source regression | 9,176 passed, 35 skipped, 256 deselected, 3 documented ACP xfails | [Source acceptance](typing-acceptance-20261002.md) |
| Embedded and memory regression | 514 passed, 14 skipped, 1 deselected | [Source acceptance](typing-acceptance-20261002.md) |
| Public Windows stability | 6 native cases passed; both loaded paths passed again after an audit cleanup fix | [Stability acceptance](windows-public-stability-20261002.md) |
| Load generator and audit cleanup regressions | 6 passed | [Stability acceptance](windows-public-stability-20261002.md) |
| Complete isolated wheel installation | 615 passed, no failures/errors/skips/deselections, 1,870.13 seconds | [Full installed acceptance](windows-public-stability-20261002.md) |
| Configured Pyright | 320 files, zero errors or warnings | [Stability acceptance](windows-public-stability-20261002.md) |

The installed run rebuilt all five wheels, checked installed-module provenance,
used a copied suite with the interpreter's `-I` isolation, and removed its
temporary installation. Resource, unraisable-exception and unhandled-thread
warnings remained errors. The public entry was exercised without admission
substitution. No production code was changed during delivery preparation.

Delivery review found no blocking standards or P1/P2 correctness issues across
the accumulated typing, Viewer and Windows changes. The earlier audit-profile
cleanup finding was already fixed and regression-tested. The repository's
pre-commit checks passed for all 100 delivery files: Ruff lint/format/encoding,
Pyright, repository-wide lint and format checks, whitespace/EOF, YAML,
large-file, conflict-marker and SPDX checks. No notebooks changed, so the
notebook-output hook had no files to check. The local transcript is
`logs/windows-v1-delivery-precommit-20261002.log`.

## Local evidence retention

The detailed reports and diagnostic caches stay in the local `logs/` directory,
which is ignored by Git. The linked records include commands and summarized
results; raw reports and machine-specific caches are not part of the source
delivery. SHA-256 hashes identify the retained final acceptance reports:

| Local report | SHA-256 |
| --- | --- |
| `logs/windows-public-full-20261002.xml` | `97c3c916cefc1d078b3f60e83339024ccd6ffb31b7084da6e12a4171376c53d0` |
| `logs/windows-public-stability-20261002.xml` | `f94e92934b70f3d6f9cb17c137c2c80fef42866c9c208f94b8768673c506c069` |
| `logs/windows-public-stability-final-20261002.xml` | `291fc7a7bf9c553caf3ece07de8eaf22e63a5cb3eab87340d176edc5342f5786` |
| `logs/windows-public-stability-load-final-20261002.xml` | `cb2c8d0b45311f18d5a5d00f10c33bb2015dd84caf9da8c6b3e8cc2a89e8757b` |

## Explicit limits and later work

- The unified `CodeActConfig(execution_backend="sandbox")` path still uses
  the fork backend and refuses native Windows. Callers use the explicit
  Windows session; the ordinary in-process backend does not enable isolation.
- Windows policies are not translations of Linux `SandboxConfig` permissions
  or resource units. Unsupported requested policies are refused.
- Granted host callbacks run with host authority. The worker's restrictions do
  not automatically constrain those callbacks.
- Automatic crash-recovery scheduling, sustained saturation, disk-full testing
  and broader dependency compatibility remain separate follow-up work.
- The three documented ACP xfails and optional/platform test skips retain the
  limits described in the source-acceptance record.

These limits define this version's scope. They do not reopen the completed
public-session and bounded-stability acceptance.
