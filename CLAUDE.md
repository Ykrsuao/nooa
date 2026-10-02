# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read `AGENTS.md` first.** It is the source of truth for the framework's authoring rules: ellipsis = LLM generation, visibility defaults, strategy selection, the reserved `reasoning` parameter, context blocks, tracing, and generator-method limits. This file does not repeat those rules. For deeper authoring guidance, see `skills/nooa-agent-authoring/SKILL.md`.

## Project

NOOA (NVIDIA-labs Object Oriented Agents) is a Python framework where an agent is a Python class. Its fields are state, its methods are capabilities, docstrings are prompts, and return types are contracts. Requires Python 3.12 or 3.13.

This is a **uv workspace**. The root package `nooa` lives in `src/nooa`. The workspace members are:
- `packages/nooa-cli`: the `nooa` CLI, including `nooa start-dev`, which runs the trace viewer on port 5001
- `packages/nooa-acp`: Agent Client Protocol server
- `packages/nooa-memory`
- `packages/nooa-bench`: benchmarks
- `util/eval_pipeline`

## Commands

Use `uv` only. Never use pip, poetry, or conda.

```bash
uv sync --all-extras --no-extra sandbox    # same install as CI
uv run ruff check .                        # lint
uv run ruff format --check .               # format check (line length 100)
uv run pyright                             # type check (runs in pre-commit)
uv run python scripts/check_license_headers.py   # every source file needs an SPDX header

uv run pytest                              # default: excludes integration, stress, and sandbox markers
uv run pytest tests/test_metaclass.py::test_name   # run one test
uv run pytest -m sandbox                   # forks a real sandbox worker (Linux Landlock/seccomp)
uv run pytest -m integration               # makes live LLM API calls; needs keys (see .env.example)
```

How pytest is configured:
- `asyncio_mode = "auto"`
- 300s timeout per test; a hang counts as a failure
- `--import-mode=importlib`
- `pythonpath` includes `src` and `packages/nooa-cli/src`
- `testpaths` also covers the `packages/*/tests` directories

Test gotcha: the tests in `tests/tools/test_shell_tools_modern.py` silently skip when `rg` (ripgrep) is not on PATH.

The React trace viewer frontend is in `src/nooa/viewer/frontend-react`. CI checks that the committed `dist/` build matches a rebuild made with `npm ci --ignore-scripts`. If you change the frontend, rebuild and commit `dist/`.

## Architecture

The call path is described in `docs/architecture.md`:

1. **Class creation (`metaclass.py`, `ellipsis_detection.py`)**: the `Agent` metaclass inspects each method. An async method whose body ends in `...` is wrapped as an agentic method. Every other method keeps its body and is wrapped only for tracing and other runtime services (`runtime/method_wrapper.py`).
2. **Call resolution**: for an agentic call, the runtime resolves:
   - the LLM (override order: call → method → instance → class → parent; see `llm_config.py`, `method_llm.py`)
   - the strategy (default is CodeAct)
   - method-scoped context and event filters
   - truncation settings
3. **Prompt assembly (`runtime/context_builder.py`, `context_blocks/`, `prompts.py`)**: the prompt is built from named blocks:
   - the role
   - framework and strategy instructions
   - `doc(type(self))` (from `agentdoc/`)
   - visible instance state
   - developer `Context` blocks
   - the event history
   - the method signature, docstring, and arguments

   Fixed prefix blocks are ordered for provider prompt caching; see `docs/stable-prefix-caching.md`.
4. **Strategies (`strategies/`)**:
   - `PredictStrategy`: a single structured LLM call.
   - `CodeActStrategy` (`codeact.py`, `codeact_v2.py`): an iterative loop with `execute_python` and `return_result`. It creates a fresh REPL for each agentic call.

   Both validate the return type and send validation errors back to the model for another attempt. Built-in strategies lock the agent instance for the duration of a generation call. Generated code can run in the OS-level sandbox (`runtime/sandbox/`).
5. **Events (`runtime/event_manager.py`, `event_backend.py`, `storage/`)**: an event-sourced history of each agent instance. Backends include SQLite. It is separate from context blocks, which are deliberate prompt insertions.
6. **LLM layer (`unifiedllm/`)**: a provider abstraction on top of litellm.
7. **Tracing (`tracing/`, `viewer/`, `trace_explorer/`)**: nested spans that follow Python call nesting (method → generation → litellm / code_execution / method_call).

Built-in skills and tools are registered through the `nooa.skills` entry points in `pyproject.toml`. Examples include `ShellTools`, `TodoManager`, `ContextApi`, and `EventsApi`, which map to modules under `tools/` and `runtime/`.

## Contribution rules (from CONTRIBUTING.md)

- Sign off every commit with `git commit -s` (DCO).
- **Automated agents must end every commit message, PR title, issue title, and PR/issue comment with `🤖🤖🤖`.** Submissions without it are closed without review.
- Every experiment in `experiments/` needs a `README.md` that covers the research question, design, metrics, how to run it, and results.
- Pre-commit runs `nbstripout` on notebooks. It also runs ruff, pyright, and an SPDX header check.
