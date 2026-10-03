# nooa-acp

**Run the NOOA coding agent inside your editor.** `nooa-acp` is an
[Agent Client Protocol](https://agentclientprotocol.com) server, so any
ACP-speaking client — Zed today — can drive the same agent the terminal host
uses: CodeAct, repository tools, a persistent shell, installed skills, workspace
slash commands and durable sessions, with file edits and terminal commands
surfaced as structured activity.

By default it hosts `nooa_cli.coding.CodingAgent` directly. Repository instructions
(`AGENTS.md`), coding tools, summarization, installed `nooa.skills` entry points
and semantic file and terminal activity therefore have no separate ACP
implementations — fix something here and the terminal host gets it too.

This is new and we would like it exercised. If something breaks, please say so.

`--sandbox auto --sandbox-mode code` isolates generated Python in the native
Windows or Linux sandbox while keeping this same `CodingAgent`, including its
trusted host shell, skills and MCP. Host commands can connect to the network
and their file changes persist; they are not isolated by the Python sandbox.
`--sandbox auto` alone keeps the existing `strict` mode, with bounded workspace
file tools, disposable isolated commands and no skills/MCP.
macOS is currently unsupported. See [sandbox operation and limits](../../docs/acp-sandbox.md)
for setup, command snapshot behavior and platform differences.

## Install

```bash
uv add nooa-acp                 # or: uv add "nooa[acp]"
```

There is no default model. Set `NOOA_MODEL` or pass `--model`, or the command
exits with a usage error.

## Quick start: Zed

Zed launches ACP agents as "external agents". Add NOOA to `settings.json`
(`cmd-,`):

```json
{
  "agent_servers": {
    "NOOA": {
      "type": "custom",
      "command": "uvx",
      "args": ["nooa-acp"],
      "env": {
        "NOOA_MODEL": "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
        "NVIDIA_API_KEY": "nvapi-..."
      }
    }
  }
}
```

Open a repository, then pick **NOOA** from the `+` menu in the agent panel. Zed
runs the command with your worktree as its working directory, so repository
instructions, project skills and sessions resolve against the open project.

Credentials go in `env` here rather than in Zed's own settings: the agent is a
separate process and inherits only what Zed passes it. Use a secret-manager
wrapper as the `command` if you would rather not put a key in `settings.json`.

From a checkout whose environment is already installed, use `uv run` to find
the local executable. For example, in PowerShell:

```powershell
uv run --project "E:\rivon\labs-OO-Agents" --no-sync nooa-acp --model YOUR_MODEL
```

Replace the path with your checkout and `YOUR_MODEL` with a LiteLLM model name
or configured NOOA alias; omit `--model YOUR_MODEL` when `NOOA_MODEL` is set.
The absolute project path works from any directory, without activating `.venv`
or adding `.venv\Scripts` to `PATH`. `--no-sync` uses the existing environment
without changing dependencies. To configure an ACP client, use `uv` as its
command and the arguments above starting with `run`. Add
`--sandbox auto --sandbox-mode code` for the shared upstream-compatible tool
model, or `--sandbox auto --sandbox-mode strict` for restricted workspace tools.
`--sandbox-network on` optionally enables direct Python-worker networking in
code mode; host tools retain their networking independently. The server waits for ACP input on stdin/stdout; it
does not open an interactive terminal chat.

### MCP servers do not carry over from Zed

**Remote MCP servers you authenticated inside Zed are not usable from an ACP
agent.** Zed holds those OAuth tokens itself and does not pass them down, so a
server showing a green indicator in Zed's own UI arrives at the agent either
with no tools at all or with nothing but its `authenticate` /
`__complete_authentication` stubs. Local stdio MCP servers are unaffected.

This is a known Zed limitation, tracked in
[zed-industries/zed#54410](https://github.com/zed-industries/zed/issues/54410)
(open, labelled `area:ai/mcp` + `area:ai/acp`). A maintainer has said the
plumbing largely exists and the work is queued, but as of this writing it is
unresolved.

Configure the MCP server directly for NOOA instead — through NOOA's own
`.mcp.json` — and it works normally, because the agent then owns the
connection and its credentials rather than borrowing Zed's.

## Launching the server yourself

```bash
nooa-acp --model nvidia_nim/nvidia/nemotron-3-super-120b-a12b
```

This is a JSON-RPC server, not an interactive program: it speaks ACP on
stdin/stdout and exits when its input closes, so running it in a terminal
without a client does nothing. Launch it this way to wire up an ACP client
other than Zed, or to watch the diagnostics it writes to stderr while a client
drives it. `--model` accepts any LiteLLM model name or configured NOOA alias.

## Opening a repository runs code from it

This section describes both default `--sandbox off` and `--sandbox-mode code`:
workspace Python and host tools are trusted in both. Only `--sandbox-mode strict`
skips workspace Python skills/libraries/settings and rejects forwarded MCP servers.

**Creating a session imports Python from the workspace, before you send a
prompt.** This is deliberate — it is how workspace skills work — but it means
opening a folder is enough to execute code it contains. Treat opening a
repository with NOOA as equivalent to running its build.

Three paths load workspace code at `session/new` and `session/load`:

- **Skill roots.** Every `.py` file under `.agents/skills`, `.cursor/skills`,
  `.claude/skills`, or `.claude/commands` is imported. Module-level code runs
  during import, before anything checks whether the file defines a skill, so the
  contents are irrelevant.
- **Workspace settings.** `<workspace>/.nooa/settings.yaml` and the legacy
  `.nooa/config.toml` may name *additional* skill roots. Those paths are not
  confined to the workspace: a relative path escaping it, an absolute path, or a
  symlink is accepted as written.
- **Libraries.** `<workspace>/.nooa/libs/<package>/` is imported and its
  directory is prepended to `sys.path` for the life of the process. One ACP
  server serves several workspaces, so a package name there can shadow the same
  import for later sessions on other workspaces.

The agent runs as you, in a process holding your model credentials. There is no
consent prompt on these paths.

**Open repositories you would run.** For anything else, use an OS-level sandbox,
or start a separate server per workspace with credentials scoped to that task.

## How it behaves

ACP uses standard input and output for JSON-RPC. Diagnostics are written to
standard error. In the default `--sandbox off` mode, the agent can execute generated Python and shell commands, so
use an OS-level sandbox for untrusted tasks. Generated code shares the agent's
process environment, including model credentials; launch it with only the
credentials and network access that the session may use.
Cancellation stops cooperative local work immediately. An in-flight provider
request may finish in the background when its client does not support
transport-level aborts. Slash commands run on the agent's event loop so they
have the same semantics as the native TUI and can safely start agent jobs. An
async command is cooperatively cancellable; a synchronous command that blocks
that loop cannot be preempted by the current in-process adapter. The planned
one-process-per-agent boundary is the safe kill mechanism for that case.

## Sessions and skills

Each ACP session has an independent live agent and allows one foreground prompt
at a time. Sessions are stored in `<workspace>/.nooa/sessions`, where the TUI
and ACP adapter can share list and replay metadata. These files are inside the
workspace trust boundary: a repository can supply session records that appear
in `session/list` and are replayed as conversation history by `session/load`.
Open only repositories whose code and conversation history you trust. The
adapter also advertises session close; closing a live session preserves its
durable history.

Sandbox sessions instead store transcripts under the trusted user directory,
outside the granted workspace. Their file edits persist, but commands run in
disposable snapshots and command changes are discarded. The regular skills,
shell and MCP behavior described below applies to `--sandbox off`.

The current stdio adapter hosts those live agents in its own process. That is
an adapter-private implementation detail rather than part of the durable
session API: the live-session registry is isolated inside `nooa-acp` so it can
later be replaced by handles to an agent daemon without changing stored
sessions, the shared coding agent, or the ACP protocol surface.

Python skill packages use the interpreter's normal import machinery. Multiple
sessions may use distinct skill package names, but two workspaces must not load
different checkouts under the same top-level Python package name in one ACP
server process. Launch a separate stdio server for those workspaces. A future
one-process-per-agent daemon will make that isolation an OS process boundary.

Installed `nooa.skills` entry points are loaded into the shared skill registry
but remain opt-in. The agent can activate a relevant skill with
`self.skills.activate(["name"])`. Stdio MCP servers supplied by an ACP client
are registered and activated as `mcp.<name>` skills for that session.

Workspace and user skill roots are shared with the terminal host through
layered `settings.yaml`. New configuration should use:

```yaml
coding:
  additional_skills_dirs:
    - ../nemo-oo-skills
```

The existing `tui.additional_skills_dirs` key remains supported during the
migration, as does the older project-local `.nooa/config.toml` key
`[tui].libs_dirs`. Packaged libraries declared through `nooa.skills`, `SKILL.md`
skills, and standalone Python skills are discovered from each configured root.
Loaded `@slash_command` methods are advertised through ACP and matching
`/command arguments` prompts are dispatched through the shared typed command
router. Command discovery is refreshed when loaded skills change.

The current adapter accepts text and resource-link prompts plus stdio, HTTP,
and SSE MCP servers forwarded by an ACP client. ACP-transport MCP proxies,
additional workspace directories, images, and embedded resources are not
advertised yet. An unavailable, duplicate, or unsupported MCP server is skipped
with a session warning so it cannot prevent a new or restored NOOA session from
opening.
