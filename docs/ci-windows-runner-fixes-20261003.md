# Windows runner CI follow-up — 2026-10-03

[Run 37107047349](https://github.com/Ykrsuao/nooa/actions/runs/37107047349),
at `a57fc6a7`, passed seven jobs but failed both Windows jobs. Windows 3.12
reported a Viewer encoding failure, command bootstrap failures, and downstream
CLI/ACP failures. Windows 3.13 passed the native lifecycle suite but failed
Viewer startup in the clean wheel installation acceptance suite.

## Viewer stream identity

Redirected Python stdout can use `cp1252` with `surrogateescape` errors.
`click.get_text_stream()` requests `strict` by default and can create a UTF-8
wrapper; `click.echo()` separately resolves its cached, locale-encoded stream.
Formatting for the first stream did not protect the write to the second.

The Viewer now selects the output stream with `errors=None` and supplies that
same stream to both diagnostic formatting and `click.echo(file=...)`. The actual
database path, stream encoding and error strategy are unchanged.

Tests cover strict and surrogateescape streams. The existing real Viewer
acceptance also runs with `PYTHONIOENCODING=cp1252:surrogateescape` on its own
child process, reproducing CI on UTF-8 or GBK developer machines. That case
reproduced the exact original failure before the fix and now verifies HTTP,
assets, the Unicode database path and normal shutdown successfully.

## Command initialization

The runner returned `Access is denied` before the requested shell commands
executed. The original log did not distinguish system executable ACLs, console
initialization, or shell startup. A real controlled test with a host executable
copy lacking LPAC access reproduced the same failure; it does not prove that
this was the runner's only cause.

The new bootstrap removes the environment-dependent `chcp` executable, NUL
redirection and outer cmd parsing. The existing isolated Python runtime directly
calls `SetConsoleCP` and `SetConsoleOutputCP`, then launches a verified copy of
the system `cmd.exe` in the existing private read-only runtime. Console setup
and shell launch failures now identify their stage and Windows error.

There are no system ACL changes, additional capabilities or host fallback.
The hidden console, handle allowlist, atomic Job ownership and configured
process limits remain in force. Two process slots cover initialization and the
command shell; external programs need more slots. The deadline includes startup.
Tests separately check a strict startup deadline and retention of output after
a real child signals readiness, avoiding any required interpreter startup speed.

CI now runs native command and redirected Viewer regressions on both Windows
versions before the long suites. No existing suite was removed or disabled.

## Local evidence

All local tests use uv and Python 3.12. Evidence files remain in ignored `logs/`.

- Viewer stream regressions: 6 passed; real Windows Viewer acceptance: 2 passed.
  Before the fix, the surrogateescape case and the redirected child reproduced
  the original exception. See `ci-viewer-click-stream-{red,green}-20261003.xml`
  and `ci-viewer-redirected-{red,green}-20261003.xml`.
- WSL Viewer diagnostics and real acceptance: 8 passed in 19.65 seconds,
  `ci-viewer-redirected-wsl-20261003.xml`.
- Command/bootstrap/native creation: 17 passed in 59.67 seconds,
  `ci-windows-command-direct-bootstrap-final-20261003.xml`. The final readiness
  handshake test separately passed in 7.48 seconds,
  `ci-windows-command-output-handshake-20261003.xml`.
- Windows CLI and real ACP protocol integration: 4 passed in 270.53 seconds,
  including editing, isolated command execution, cancellation, reuse, close and
  session restoration; `ci-command-cli-acp-green-20261003.xml`.
- Repository Ruff lint/format, explicit encodings, SPDX headers and configured
  Pyright scope passed.

Local passes are not a replacement for the complete hosted Windows matrix.
The GitHub workflow for the follow-up commit is the final runner validation.
