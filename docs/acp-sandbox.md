# ACP native sandbox

Enable code isolation with the original Linux Agent/tool model on either host:

```sh
nooa-acp --model YOUR_MODEL --sandbox auto --sandbox-mode code
```

Both platforms use the ordinary `CodingAgent`: shell, repository tools, skills,
libraries and MCP keep their existing implementation. Generated Python runs in
the native sandbox; Agent methods are executed in the trusted host process.
Shell commands can access the host network and their file changes persist.
Those host tools are **not confined by the Python worker's filesystem or
network policy**. This is code isolation, not an isolated development terminal.

| Selection | Generated Python | Shell / skills / MCP |
| --- | --- | --- |
| `--sandbox off` (default) | Host process | Host process |
| `--sandbox auto --sandbox-mode code` | Native Windows / Linux worker | Normal trusted host tools; edits persist |
| `--sandbox auto --sandbox-mode strict` | Native worker with exact callbacks | Bounded file tools and disposable command snapshots; skills/MCP disabled |

`--sandbox-mode` defaults to `strict` to preserve existing sandbox launches.
Set `code` explicitly for the upstream-compatible behavior described above.
`NOOA_ACP_SANDBOX_MODE` and `NOOA_ACP_SANDBOX_NETWORK` provide the corresponding
environment options. `--sandbox-network off` is the default in code mode and
only controls the worker's direct network access; trusted host tools can still
connect. `--sandbox-network on` enables direct worker network capabilities.
It is rejected with sandbox `off` or mode `strict`, rather than ignored.
General Python module restrictions remain independent of OS network grants.
Windows firewall, AppContainer loopback and asynchronous I/O limitations also
remain; the host tools provide normal networking on both platforms.

For the stricter workspace mode, existing launch commands remain valid:

```sh
nooa-acp --model YOUR_MODEL --sandbox auto
# Equivalent CLI plugin:
nooa acp --model YOUR_MODEL --sandbox auto
```

`YOUR_MODEL` is a placeholder for a LiteLLM model name or a configured NOOA
alias. If `NOOA_MODEL` is already set, omit `--model YOUR_MODEL`.

For a source checkout with its environment already installed, use `uv run`.
For example, in PowerShell (replace the project path with your checkout):

```powershell
uv run --project "E:\rivon\labs-OO-Agents" --no-sync nooa-acp --model YOUR_MODEL --sandbox auto
```

This works from outside the checkout and does not require activating `.venv`
or adding `.venv\Scripts` to `PATH`. `--no-sync` uses the existing environment
without changing its dependencies. `nooa-acp` serves ACP JSON-RPC over
stdin/stdout; an ACP client must drive it. Starting it in a terminal waits for
protocol input rather than showing an interactive chat prompt.

`NOOA_ACP_SANDBOX=auto` provides the same selection. The default is `off`,
preserving existing coding skills and MCP behavior. `linux` and `windows`
select a particular native backend and reject a different host. There is no
fallback to host execution when isolation is unavailable.

| Host | Generated Python | Commands |
| --- | --- | --- |
| Windows | LPAC token, owned runtime and Job Object | Dedicated LPAC runtime, bounded child processes in a Job Object |
| Linux | Existing fork worker with Landlock and seccomp; restricted parent callbacks | Fresh interpreter applies Landlock/seccomp before executing bash |
| macOS | Unsupported; startup fails explicitly | Unsupported |

macOS has no backend in this change and has not been tested. Linux native
validation uses Ubuntu in WSL; Windows validation runs directly on Windows.
No version rename is needed: `SandboxSession` is a common lifecycle entry for
the existing platform mechanisms, with their own permission types preserved.

## Strict workspace mode

This section applies to `--sandbox-mode strict` only. Code mode uses the normal
coding tools and does not create these command snapshots.

The session's `cwd` grants one workspace. Generated code accesses it through
`workspace_list`, `workspace_read`, `workspace_create`, `workspace_write` and
`workspace_replace`. Persistent edits use pinned native handles on Windows and
descriptor-relative no-follow operations on Linux. Absolute paths, parent
traversal, symbolic links, junctions, hard links and nonregular files are
rejected. Parents of newly created files must already exist. UTF-8 reads and
writes are bounded at 1 MiB per file; listing is bounded at 512 entries.

`run_command` copies the current workspace into a new private directory, runs
`cmd.exe /d /c` on Windows or `bash --noprofile --norc -c` on Linux, then removes
the private runtime. **Command-created or modified files are discarded.** Use
the workspace tools to persist changes. Results contain stdout, stderr, exit
status, timeout/truncation flags and snapshot exclusions. Output is capped at
1 MiB combined; the default timeout is 30 seconds, with a maximum of 60.

Snapshots preserve binary file bytes, are bounded at 512 entries and 16 MiB,
and exclude directories named `.git`, `.nooa`, `.venv`, `node_modules` and
`__pycache__` at every depth. Unsupported links, unreadable files and exceeded
limits stop the command before execution. Concurrent trusted host edits can
produce a snapshot from more than one point in time; this is not a Git commit.

Commands receive a small environment and system tools plus Python. They do not
receive model credentials or internet access. Project dependencies are not
installed automatically. A test needing excluded dependencies must report the
limitation; it must not report a successful check or retry on the host.

Only these workspace/command methods and `message` are granted parent
callbacks. Sandbox sessions do not load workspace Python skills, libraries,
settings, installed skills or forwarded MCP servers. Forwarded MCP requests
are rejected before any server is launched. Repository `AGENTS.md` can be read
as text through the file tool.

## Session ownership

New and restored sessions use the same sandbox setup. A restored session is
published only after transcript replay finishes. There is one foreground turn
per session. Cancellation waits for command cleanup before allowing reuse;
normal completion, timeout and cancellation retire command descendants.

Sandbox transcript databases live under the trusted user directory:
`~/.config/nooa/acp-sandbox-sessions/<workspace-hash>` for strict mode and
`~/.config/nooa/acp-code-sandbox-sessions/<workspace-hash>` for code mode
(or the corresponding `NEMO_OO_USER_DIR`). Their histories are separate, so
selecting the code entry does not implicitly reopen a strict-mode session.
The store must be outside the granted workspace. They are separate from the
ordinary ACP/TUI store at `<workspace>/.nooa/sessions`. Closing a session keeps
the transcript for later loading. A cleanup failure retains the resource owner
and storage handle so a subsequent close can retry.

## Platform limits

The server, model client, installed runtime and other processes running as the
same host user remain trusted. File grants authorize changes throughout the
workspace; prompts and repository text cannot expand them.

In code mode the whole Agent tool interface is trusted, as in the original
Linux backend. Host shell commands, workspace Python skills, libraries and MCP
can have effects outside the project and can access host credentials. The
restricted callback and command guarantees below describe strict mode.

Windows cells retain the original child-process ban. Only the dedicated
command profile allows descendants, bounded by a Job Object with memory, CPU
and process limits. Commands never change the original workspace's ACLs.

Linux generated Python still uses the existing fork backend. Restricting
callbacks and closing inherited descriptors does **not** erase all inherited
Python memory. This is not a credential-confidentiality boundary for hostile
generated Python; use a separate OS account/container or server with scoped
credentials when that boundary is required. The command runner starts a fresh
interpreter and then execs bash, so commands do not inherit the Agent object.
The restricted Python worker redirects native standard input/output away from
ACP transport, closes inherited host descriptors, and denies child processes,
host-process signaling and new sockets. Python's captured print output remains
available. Commands are launched through the dedicated command method.
Linux command limits apply per process (512 MiB memory headroom, a CPU limit,
64 MiB per output file and 128 file descriptors). There is no aggregate cgroup
memory/process/disk quota or PID namespace. Process-group cleanup also does not
provide Windows Job Object behavior after an abrupt host crash.

## Zed example

Add the flags to the existing external-agent configuration, preserving its
executable path, model and environment. This example selects code mode:

```json
{
  "agent_servers": {
    "NOOA sandbox": {
      "type": "custom",
      "command": "nooa-acp",
      "args": ["--model", "YOUR_MODEL", "--sandbox", "auto", "--sandbox-mode", "code"]
    }
  }
}
```

Save, restart the external Agent (or Zed) and create a new conversation. Existing
running sessions do not acquire a different policy. Code mode accepts forwarded
MCP servers using the normal ACP behavior; strict mode rejects them.

## Python entry

The common `nooa.runtime.sandbox.SandboxSession` owns sequential calls and
cleanup. Both hosts accept `SandboxConfig(require=True)` for code isolation.
With this configuration, `tools=None` selects the trusted live Agent proxy;
an explicit `tools=(...)` restricts parent callbacks and `tools=()` denies all.
Windows can additionally take `WindowsSandboxPolicy` for exact native grants
and Job Object budgets. Its explicit `host_tools=True` selects the live proxy
and is mutually exclusive with exact broker/tool grants.

The common interface does not pretend native mechanisms are identical. Windows
rejects Linux direct `workspace` / `allow` rules, disabled filesystem/system
guards, nondefault Linux resource limits and polling semantics. Both hosts honor
the shared `timeout_grace_s` (default 2 seconds) and exclude time spent in host
tools from the cell deadline. Set `CodeActConfig.cell_timeout` when constructing
the session; per-strategy configuration cannot change that budget on either host.
Code-mode
ACP accesses project files through the normal host tools, so it does not need
those direct worker grants. Windows stages installed framework dependencies and
the first-party CLI source; arbitrary editable third-party packages remain
unsupported. Third-party return types may need explicit dependency staging.
Use the strategy only while its owning session is entered.
