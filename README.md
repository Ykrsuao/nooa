<div align="center">

<br />

<!-- Absolute URLs, not repo-relative paths: this README is also the PyPI
     long_description, and PyPI renders it standalone with no assets/
     directory alongside it, so relative paths 404 there. -->
<picture>
  <source
    media="(prefers-color-scheme: dark)"
    srcset="https://raw.githubusercontent.com/NVIDIA-NeMo/labs-OO-Agents/main/assets/nvidia-labs-object-oriented-agents-dark.svg"
  >
  <source
    media="(prefers-color-scheme: light)"
    srcset="https://raw.githubusercontent.com/NVIDIA-NeMo/labs-OO-Agents/main/assets/nvidia-labs-object-oriented-agents-light.svg"
  >
  <img
    alt="NVIDIA-labs Object Oriented Agents"
    src="https://raw.githubusercontent.com/NVIDIA-NeMo/labs-OO-Agents/main/assets/nvidia-labs-object-oriented-agents-light.svg"
    width="820"
  >
</picture>

<p align="center"><b>A Pythonic way to build AI agents.</b></p>

[![NVIDIA](https://img.shields.io/badge/NVIDIA-76B900?logo=nvidia&logoColor=white)](https://www.nvidia.com/)
[![Paper](https://img.shields.io/badge/paper-arXiv-b31b1b?logo=arxiv&logoColor=white)](https://arxiv.org/abs/2607.20709)
[![Blog](https://img.shields.io/badge/blog-NVIDIA-76B900?logo=nvidia&logoColor=white)](https://developer.nvidia.com/blog/six-agent-harness-capabilities-for-higher-model-performance/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/LICENSE)

**[Docs](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/docs/README.md)** &nbsp;·&nbsp; **[Quick Start](#quick-start)** &nbsp;·&nbsp; **[Notebook Tutorials](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/notebook_tutorials/README.md)** &nbsp;·&nbsp; **[Examples](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/examples/README.md)** &nbsp;·&nbsp; **[Paper](https://arxiv.org/abs/2607.20709)** &nbsp;·&nbsp; **[Blog](https://developer.nvidia.com/blog/six-agent-harness-capabilities-for-higher-model-performance/)**

<br />

</div>


NVIDIA-labs Object Oriented Agents (NOOA) is a model-agnostic Python framework designed to support reliable AI agent development. Many agent frameworks represent prompts, tools, callbacks, and workflows as separate abstractions. NOOA offers an alternative object-oriented interface that brings these concepts together in a Python class. NOOA lets developers express an agent’s state, capabilities, prompts, and typed interfaces through a single Python class:

```python
from nooa import Agent

# The agent is a Python object.
class SupportAgent(Agent):
    """You are a support agent."""

    # State lives on the object. Fields are typed.
    order_db: OrderDB

    # Ordinary method. Just Python.
    def is_refund_eligible(self, order: Order) -> bool:
        return order.delivered and order.days_since_delivery <= 30

    # Agentic method: the runtime hands this to an LLM.
    async def triage(self, message: str, order: Order) -> Ticket:
        """Create a typed support ticket."""
        ...
```

**What's happening here:**

- **Agents are Python objects.** Fields are state, methods are capabilities, docstrings are prompts, type annotations are contracts.
- **`...` bodies are LLM-driven.** A method with `...` becomes an agentic loop; a real body stays deterministic Python.
- **Generation commits one result.** Deterministic generators are supported, but generated streams need a separate stream contract and are rejected clearly.
- **Code as action.** The model acts by writing Python in a Jupyter-style REPL with access to `self`, imports, and helpers — Python methods and type annotations supply the callable interfaces, reducing the need to write separate tool-schema definitions.
- **Pythonic and agent-ready.** Typed I/O with auto-retry, live-object arguments passed by reference, and model-callable context and event APIs — designed around agent-oriented Python workflows.

This design supports familiar Python testing, tracing, refactoring, and version-control workflows — **just like the rest of your software**. Read [the paper](https://arxiv.org/abs/2607.20709) for the design principles and evaluation results.

Want to see how the pieces compose? Take the [**10-minute tour**](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/docs/tour.md), from one thinking method through tools, typed contracts, deterministic orchestration, and object composition.

## Installation

Add the **core** framework to a new (or existing) Python project with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv init my-agent-project
cd my-agent-project

uv add nooa
```

Or with pip: `pip install nooa`.

<details>
<summary><b>Optional sub-packages</b> — CLI, ACP, memory, benchmarks, evaluation pipeline</summary>

<br />

The CLI, ACP, memory, and benchmark packages are separate distributions. Install
them by name, or pull them in as extras of the core package:

```bash
uv add nooa-cli                 # or: uv add "nooa[cli]"
uv add nooa-acp                 # or: uv add "nooa[acp]"
uv add nooa-memory              # or: uv add "nooa[memory]"
uv add nooa-bench               # or: uv add "nooa[bench]"

uv add "nooa[cli,memory]"       # several at once
```

| Package | Extra | What it adds |
|---|---|---|
| `nooa-cli` | `nooa[cli]` | the `nooa` command, trace viewer, eval runner |
| `nooa-acp` | `nooa[acp]` | coding agent for Agent Client Protocol hosts such as Zed — [setup](packages/nooa-acp/README.md) |
| `nooa-memory` | `nooa[memory]` | long-term memory subsystem (`MemoryManager`) |
| `nooa-bench` | `nooa[bench]` | `BenchAgent` and the Harbor benchmark runner |

`eval_pipeline` is not published to PyPI — install it from the repo:

```bash
uv add "eval_pipeline @ git+https://github.com/NVIDIA-NeMo/labs-OO-Agents.git@main#subdirectory=util/eval_pipeline"
```

</details>

<details>
<summary><b>Installing from source</b> — track <code>main</code> or pin a tag</summary>

<br />

```bash
# latest development state
uv add "nooa @ git+https://github.com/NVIDIA-NeMo/labs-OO-Agents.git@main"

# pinned to a release tag
uv add "nooa @ git+https://github.com/NVIDIA-NeMo/labs-OO-Agents.git@v0.0.7"
```

</details>

<details>
<summary><b>Windows</b> — native support and current limits</summary>

<br />

NOOA runs on native Windows (no WSL) with the regular Windows build of Python.
Agents, Predict and CodeAct generation, the LLM layer, SQLite event storage, tracing, and
the trace viewer all work, and so does `uv sync` from source. UTF-8 mode
(`PYTHONUTF8=1`) is not required: NOOA reads and writes its files as UTF-8 whatever the
system code page is.

`ShellTools` and the bash sessions behind the `nooa-cli` coding agent and the `nooa-acp`
shell tools run in the bash that ships with
[Git for Windows](https://git-scm.com/download/win), which NOOA finds through `git` on
`PATH` or the standard install locations. To use another MSYS2 bash, set `NOOA_BASH` to
its `bash.exe`. `C:\Windows\System32\bash.exe` is never used: it is the WSL launcher, and
its commands would run inside a Linux VM with a different filesystem. The session tracks
the working directory in Windows form (`C:/src`). Python subprocess streams default to
UTF-8 inside the shell (an explicit `PYTHONIOENCODING` is preserved), including when the
Windows system code page is GBK. A command that times out is stopped by
terminating the processes it started. A long-running bash builtin such as a `while true`
loop has no child process to stop, so the session is restarted in the same working
directory instead, and shell state like variables and functions is lost.
Cancelling a command or closing its output stream before completion also stops the
session's process tree. The next command starts a fresh shell in the last reported
working directory. Explicitly close `ShellTools`/`BashSession` when finished, before
closing the event loop; recovery from an already-closed Windows loop releases its
old pipes and process handles as well.

Use the CLI to diagnose the local environment before starting an agent:

```powershell
uv run nooa doctor
uv run nooa doctor --smoke
uv run nooa doctor --json --port 5002
```

The default checks do not load credentials. On Windows, `sandbox` reports the
fork backend limitation as a warning, while `windows_sandbox` separately checks
whether native AppContainer and Job Object API bindings load. That prerequisite
check starts no sandbox and does not verify containment. `--smoke` runs only
shell Unicode file/command, cancellation, recovery, and cleanup checks in a
temporary workspace. Blocking errors return exit status 1. See the
[CLI diagnostics documentation](packages/nooa-cli/README.md#environment-diagnostics)
for checks, exit codes, and JSON output.

For isolated native Windows Agent calls, explicitly enter a
`WindowsSandboxSession` and attach its strategy to the Agent method:

```python
from nooa import Agent, strategy
from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

async def run(llm):
    policy = WindowsSandboxPolicy(inputs={"seed.txt": b"A short input document."})
    async with WindowsSandboxSession(policy) as session:
        backend = session.strategy()

        class Reader(Agent, llm=llm):
            @strategy(backend)
            async def summarize(self) -> str:
                """Read ../inputs/seed.txt and summarize it."""
                ...

        return await Reader().summarize()
```

The policy defaults to a read-only disposable workspace. Explicit named file,
directory and fixed-HTTPS brokers provide bounded parent-side operations; memory
and CPU budgets use native Windows units. Calls are sequential, use fresh
workers and share workspace files. Await every call before leaving the context.
Install application dependencies first, then name them in
`application_requirements`; stage application source files explicitly with
`application_modules`. See the [policy and lifecycle contract](docs/windows-sandbox-policy.md#public-windows-interface)
for dependency, cancellation and cleanup-retry limits, and the
[public-entry verification record](docs/windows-public-entry-20261002.md) for
acceptance scope.

Current Windows limits:

- **The fork sandbox backend.** Its worker relies on `fork`, so
  `CodeActConfig(execution_backend="sandbox")` raises `SandboxUnavailable`. The default
  in-process backend is unaffected. The explicit Windows session does not add
  an `execution_backend="windows"` value or translate `SandboxConfig`.
- **The `SIGUSR2` debug dump.** Windows has no `SIGUSR2`; call
  `nooa.runtime.debug_handler.dump_debug_info()` instead.
- **Secret file permissions.** Unix permission bits (`0o600`) are not enforced. Secrets
  saved under your user profile are protected by its default ACL instead.

The Windows session owns an LPAC runtime, private dependency copies, workers,
brokers and cleanup. Direct network sockets are denied; fixed-HTTPS grants do
not grant worker networking. Its private asyncio runtime supports tasks, timers
and thread wakeups, with no async descriptor I/O or subprocess support. Direct
live-host mounts, general HTTP access and broad application compatibility remain
outside this interface. Filesystem staging and cleanup drain their work before
propagating cancellation, so storage delays remain an operational limitation.
See the
[Windows sandbox status and next steps](docs/windows-sandbox.md).

</details>

## Quick Start

### ⚠️ Before Starting: safety note
NOOA is **research software**, and agents can be configured to execute LLM-generated code. We welcome contributions and fixes, but expect rough edges. LLM-generated code may take dangerous or unwanted actions, including sending private data to uncontrolled locations, deleting files, or modifying its environments.  Ensure you run NOOA agents in a sandboxed environment isolated from your primary filesystem, such as [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell).


NOOA validates generated code (AST checks) and applies module deny-lists before execution. **These are defense-in-depth guardrails, not a containment boundary.** They exist to keep generated code from freezing the event loop and to catch common mistakes early — not to stop code that is actively trying to escape. A static checker over Python cannot provide that guarantee: `open()` gives arbitrary file access, `importlib` can load modules straight from a path, and reflection reaches the rest. **The containment boundary is OS-level isolation** — always run agents that execute generated code inside a sandbox such as a container, VM, or [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell). Do not rely on the in-process validators alone.


### 1. Choose a model

Choose from supported hosted or local [LiteLLM-supported](https://docs.litellm.ai/) model:

```python
from nooa.unifiedllm.registry import get_llm_client

llm = get_llm_client("claude-haiku-4-5")                                            # Anthropic (after `export ANTHROPIC_API_KEY=...`)
llm = get_llm_client("gpt-5-mini")                                                  # OpenAI    (after `export OPENAI_API_KEY=...`)
llm = get_llm_client("ollama_chat/qwen3:1.7b", api_base="http://localhost:11434")   # Ollama    (no key)
llm = get_llm_client("hosted_vllm/Qwen/Qwen3-1.7B", api_base="http://localhost:8000/v1")  # vLLM (no key)
```

### 2. Your first agent

***Agents are Python objects***. Methods with `...` bodies are **generation methods** — implemented at runtime by an LLM-driven strategy. The signature defines the contract; the docstring is the prompt.

```python
import asyncio

from nooa import Agent


class FeedbackAgent(Agent, llm=llm):
    """You are an agent specializing in analyzing customer feedback."""

    async def analyze_feedback(self, text: str) -> str:
        """Analyze customer feedback for sentiment and key topics in one sentence."""
        ...


async def main():
    agent = FeedbackAgent()
    result = await agent.analyze_feedback("Great product, but shipping was slow")
    print(result)


asyncio.run(main())
```

Run the same code from your own project with `python`. You can run the checked-in example:

```bash
uv run python examples/quickstart/01_first_generation_method.py
```

Rename `analyze_feedback` to `analyze_feedback_briefly` and the output changes — your method name, parameters, and docstring *are* the prompt.

Prefer a guided notebook path? Start with the [**notebook tutorials**](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/notebook_tutorials/README.md), which walk through the same ideas in Colab-friendly steps, with more notebooks planned.

Ready to run something specific? Use the [**examples catalog**](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/examples/README.md) to find quickstarts for structured output, tools, strategies, tracing, context blocks, MCP, and more.

### 3. See what your agent is doing

Every LLM call, code execution, and method invocation is traced by default — orchestrators, generation methods, and helpers, with parent-child spans preserved. If you installed the CLI and viewer dependencies, start the trace viewer and open the run in your browser:

```bash
uv run nooa start-dev        # trace viewer on http://localhost:5001
```

If the viewer isn't running, tracing is silently disabled — no configuration needed either way.

## Learn more

- **[Documentation](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/docs/README.md)** — human-oriented reading paths, core concepts, architecture, and safety guidance.
- **[Framework tour](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/docs/tour.md)** — a concise conceptual showcase of NOOA's core ideas and Python-first design.
- **[Notebook tutorials](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/notebook_tutorials/README.md)** — the primary hands-on path for your first agent, strategy selection, CodeAct's live-object workflow, and composing subagents. More notebooks are planned.
- **[Examples catalog](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/examples/README.md)** — runnable quickstarts, advanced mechanics, and complete benchmark systems, indexed by capability and setup requirements.
- **[Paper](https://arxiv.org/abs/2607.20709)** — design principles, harness details, capability tests, and SWE-bench Verified / Terminal-Bench 2.0 results.
- **[Blog post](https://developer.nvidia.com/blog/six-agent-harness-capabilities-for-higher-model-performance/)** — Six Agent Harness Capabilities for Higher Model Performance.
- **[AGENTS.md](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/AGENTS.md)** — conventions used inside this repo (helpful when reading the source).

## Contributing

For a local editable install, clone the repo and sync the development environment with `uv`:

```bash
git clone https://github.com/NVIDIA-NeMo/labs-OO-Agents.git
cd labs-OO-Agents
uv sync --group dev
```

This installs the core framework, workspace packages, development tools, the `nooa` CLI, and the trace viewer runtime in the repo's `.venv`. Run CLI commands through `uv`:

```bash
uv run nooa --help
uv run nooa start-dev       # trace viewer on http://localhost:5001
```

Enable pre-commit hooks and run the test/lint suite:

```bash
uv run pre-commit install
uv run pytest                # run tests
uv run ruff check            # lint
uv run pyright               # type check
```

See [CONTRIBUTING.md](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/CONTRIBUTING.md) for the full workflow.

## Citation

If you use NVIDIA-labs Object Oriented Agents in your research, please cite:

```bibtex
@techreport{nvidia_oo_agents_2026,
  title  = {NVIDIA-labs OO Agents: Native Python Object-Oriented Agents},
  author = {Furgale, Paul and Klingler, Severin and Nolan, James and Staats, Matt and
            Di Lorenzo, Gaia and Martinez Abad, Elisa and Schueler, Christian and
            Dinu, Razvan and Devoto, Alessio and Berard, Pascal and Kaplun, Gal and Sarafian, Elad and
            Roveri, Riccardo and Derczynski, Leon and Silveira Cabral, Ricardo},
  year   = {2026},
}
```

## License

Apache 2.0. See [LICENSE](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/LICENSE) and [THIRD_PARTY_NOTICES.md](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/main/THIRD_PARTY_NOTICES.md).
