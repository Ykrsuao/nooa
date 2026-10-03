# Windows / Linux sandbox parity plan — 2026-10-03

Status: completed for the selected code sandbox mode, including Windows and WSL
Linux native ACP acceptance and Zed configuration. See the
[final verification record](sandbox-parity-verification-20261003.md).
This does not claim identical operating-system isolation or support for every
platform-specific policy setting.
The same-day follow-up also aligned deadline grace and validation, direct worker
process creation, host-object proxy semantics, cancellation cleanup and common
coding types. Both platforms passed the extended native ACP protocol; details
and platform-specific regression evidence are in the final verification record.
The user selected the original Linux model: isolate generated Python cells and
retain ordinary host terminal tools, skills, libraries and MCP capabilities.

## Boundary and route selection

| Behavior | Selected: original Linux host-tool model | Alternative: strict tool model |
| --- | --- | --- |
| Generated Python cells | Native restricted worker | Native restricted worker |
| `self.shell.run(...)` | Runs the existing host shell | Requires a separately restricted command executor |
| Command writes | Persist in the real working directory | Earlier ACP implementation discarded snapshot writes |
| Host tools and MCP | Keep their configured host permissions, including networking | Must be individually exposed through bounded callbacks |
| Skills and libraries | Existing trusted host loading behavior | Earlier ACP implementation disabled workspace loading |
| Direct worker networking | Controlled by worker policy; independent of host tools | Controlled by worker policy |
| Security claim | Cell isolation; host tools remain trusted capabilities | More limited host interface with additional compatibility work |

The selected route does not imply that the complete coding terminal is isolated.
Granting a host shell grants its host-side effects. Do not describe host commands
as unable to modify host files or reach the network. Linux fork memory and Windows
fresh-process memory also remain different; equal tool behavior is not equal OS
implementation or identical confidentiality guarantees.

## Small integration surface

1. Keep `CodingAgent`, `ActivityShellTools`, `RepoTools` and `SkillRegistry`.
2. Inject a managed sandbox strategy into generated `handle()` calls.
3. Linux retains its live `self.*` proxy; Windows opts into `host_tools=True`.
4. Windows defaults remain exact callback grants for existing sandbox API callers.
5. Retain the LPAC launcher, its Job Object, bounded IPC and owned teardown.
6. Restore ACP host loading and advertise only capabilities actually supported.
7. Keep startup failures explicit, and preserve cancellation and session restore.

Windows `host_tools=True` requires an explicit `live_agent`; mixing it with exact
tool grants or tool predicates is rejected. Framework callback roots remain
separate from the live Agent root. `doc(self.shell)` and other nested introspection
must use the same live host objects the tool calls use.

## Transport and staging

- Worker-to-host messages remain bounded msgpack, never pickle.
- Decode only the Agent's declared data types; do not import worker-chosen classes.
- Parent-to-worker values are trusted and may use pickle, as in original Linux.
- Shell results include `ShellResult`, `Match` and `FileWrite`; treating every
  return value as an existing msgpack DTO would break ordinary tools.
- Worker modules must be staged explicitly. Core `nooa` supplies terminal result
  classes and `Done`, `NeedInput`, `Waiting`; repository results need `nooa_cli`.
- Installed `nooa-cli` source staging must exclude configuration and credentials,
  and must not enable arbitrary third-party editable distribution staging.
- Staging supports the installed first-party CLI source directory, including
  editable development installs, while retaining third-party editable rejection.
  The final native test uses `application_requirements=("nooa-cli",)`.
- Third-party tool result types still require corresponding worker modules.
- `replace(path, old, new)` is the baseline test. `replace(Match, new)` additionally
  needs an explicit safe Match transport representation in both backends.

## Deterministic acceptance scenarios

Use `FakeLLMClient(..., strict_exhaustion=True)` with one `execute_python` tool
call per scripted cell. Every scenario runs in a temporary workspace; no real
provider credentials or live search service are needed.

| Scenario | Cell action and evidence |
| --- | --- |
| Host command persistence | `await self.shell.run("printf 'original' > command.txt")`; assert exit status, then inspect the real host file |
| Read and edit | Read `command.txt`, replace by path, read again; verify real file contains the new text |
| Repository navigation | Create `example.py`; call `self.repo.symbols('example.py', query='parity_symbol')`; check the returned count and text |
| MCP surface | Register and activate `mcp.search`; call `await self.search.lookup(...)`; assert exact host-observed query and fixture result |
| Dynamic documentation | `doc(self.shell)` includes `run`; `doc(self.search)` includes `lookup` |
| Waiting | First cell returns `Waiting(..., on=['system_messages'])`; a later turn returns `Done` and reads the persisted edit |
| Native isolation | Confirm a distinct worker PID and LPAC launcher; direct cell access to a private host file remains denied |
| Network boundary | Default direct cell socket creation is denied; host networking is tested separately with a local fixture server |
| Inbound protocol | Reject both raw pickle frames and pickle nested in broker payload; a host canary remains untouched |
| Recovery | After a rejected cell or replaced worker, the host tool state remains authoritative |
| ACP lifecycle | Run actual stdio initialize/new/prompt/cancel/reuse/close/load; verify notifications and persisted edits |
| Existing API behavior | Re-run exact grants, raw broker denial, undeclared host return values and typed callback tests |

## Current executable evidence

Windows Python 3.12, from the repository:

```powershell
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_host_tools.py -q
uv run --no-sync pytest tests/runtime/sandbox/test_lpac_executor.py tests/runtime/sandbox/test_lpac_codeact.py -q
```

The final native host-tool run passed 4 tests in 110.38 seconds, including
broker-payload rejection, nested-callback cancellation and production staging.
Report: `logs/lpac-host-tools-windows-20261003.xml`.
Six existing exact-mode tests passed in 175.60 seconds; report:
`logs/lpac-exact-host-parity-regressions-20261003.xml`.

The same final ACP protocol scenario passed on Windows (164.39 seconds) and WSL
Linux (82.85 seconds), covering ordinary host commands, persistent file changes,
host HTTP while direct worker networking is disabled, cancellation, reuse and
session restore followed by another turn. CLI and code/strict unit regressions
passed 39 tests. The final verification record links the captured outputs.

Zed's Windows and WSL entries now select code mode explicitly; the WSL launcher
uses this repository and its Python 3.12 environment. The user must restart Zed
and start a new Agent chat. No Zed UI or live model search verification is claimed.
macOS is outside the supported implementation and the user has no Mac device;
no macOS verification is claimed.
