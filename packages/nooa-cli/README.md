# nooa-cli

CLI for [nemo-oo-agents](https://github.com/NVIDIA-NeMo/labs-OO-Agents). Ships the `nooa` command with subcommands for running evaluations, browsing traces, and managing config.

## Install

```bash
uv add nooa-cli

# ...with numpy/pandas/plotly/scipy/sklearn pre-loaded into the LLM REPL
uv add "nooa-cli[datascience]"
```

`nooa-cli` automatically pulls in matching `nemo-oo-agents` (the core framework). The `[datascience]` extra adds libraries the LLM can use in REPL-generated code.

## Usage

```bash
nooa --help
nooa doctor          # inspect the local environment without loading credentials
nooa start-dev        # launch the trace viewer
nooa eval ...         # eval pipeline runner
nooa traces ...       # inspect/manage trace files
```

Install the separate `nooa-acp` package to add the `nooa acp` plugin command and
run the NOOA coding agent from an ACP-compatible client:

```bash
uv add nooa-acp
export NOOA_MODEL=nvidia_nim/nvidia/nemotron-3-super-120b-a12b
export NVIDIA_API_KEY=nvapi-...
uv run nooa-acp
```

See the main repo [README](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/README.md) for the framework documentation.

## Environment diagnostics

```powershell
uv run nooa doctor
uv run nooa doctor --smoke
uv run nooa doctor --json --workspace "C:\src\my project" --port 5002
```

`doctor` checks Python 3.12/3.13, the Bash executable selected by the runtime,
Git/ripgrep on `PATH`, optional viewer package metadata, workspace/configuration
path permissions, and availability of the viewer's loopback port. On Windows it
uses Git for Windows/MSYS2 discovery and rejects WSL launcher overrides.
`--workspace` selects the directory used for relative path checks. Project/user
directory and trace database overrides follow the runtime's normal environment
variables.

Default checks do not launch shell commands, load secrets/settings, change
configuration, or contact model providers. Permission results are estimates
(especially for Windows ACLs), not proof that an actual write will succeed.
The port probe briefly binds a local socket without starting a server.

Sandbox checks are separate. `sandbox` describes the existing fork executor,
which remains a warning on native Windows. `windows_sandbox` checks prerequisites
for the explicit `WindowsSandboxSession`: `ok` means the AppContainer and Job
Object API bindings loaded; a load failure is a warning. The read-only
`probe_windows_sandbox()` API returns `WindowsSandboxCapabilities` with
`native_windows`, `native_api_available`, `detail`, and
`containment_verified=False`. Loading bindings creates no profile, job, worker
or sandbox files and cannot establish that session provisioning or containment
will succeed. See the [Windows interface contract](../../docs/windows-sandbox-policy.md#public-windows-interface).

`--smoke` opts into a real shell check only, in a disposable directory containing
Chinese characters and spaces. It verifies UTF-8 file/command output, cancellation
of a running command, recovery in the same shell-tool instance, and process
cleanup. Temporary files are removed on normal completion or handled failure.
The worker does not inherit API keys or shell startup hooks. Explicit
`PYTHONIOENCODING` overrides are preserved so encoding problems remain visible.

The exit status is **0** when no blocking errors were found, **1** for diagnostic
errors (including smoke failures), and **2** for invalid command options.
Missing optional viewer packages, Git/ripgrep, occupied ports, the unavailable
fork backend and missing Windows sandbox prerequisites are warnings, not blocking
errors. Every warning/error includes
repair advice where applicable. A successful doctor report is not a security
or sandbox certification.

`--json` emits one JSON object with `schema_version: 1`, `ok`, interpreter/platform
information, and a `checks` array. Each check contains `id`, `status` (`ok`,
`warning`, `error`, or `skipped`), `message`, and `fix`. JSON uses ASCII escapes
so redirected output stays valid under Windows code pages.

## Interactive coding sessions

`nooa_cli.sessions` owns durable coding-agent session identity, metadata, and
conversation replay shared by CLI hosts such as the native TUI and ACP. The
process running an agent owns the writable session handle; other hosts attach
through their transport or use read-only discovery. Generic event and SQLite
storage primitives remain in the core `nooa` package.
