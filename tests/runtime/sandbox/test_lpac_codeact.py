# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Real Agent/CodeAct sessions with explicitly staged application code in LPAC."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.errors import GenerationError
from nooa.events import PythonOutput, ResultStatus
from nooa.runtime.restrictions import RestrictionsConfig
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
from nooa.runtime.sandbox._lpac_runtime import _stage_applications, stage_framework
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC"),
    pytest.mark.timeout(180),
]

_APP_SOURCE = Path(__file__).with_name("lpac_test_app.py")
_spec = importlib.util.spec_from_file_location("lpac_test_app", _APP_SOURCE)
assert _spec is not None and _spec.loader is not None
app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = app
_spec.loader.exec_module(app)


@pytest.fixture(scope="module")
def framework():
    with _AppContainerPython() as runtime:
        stage_framework(
            runtime,
            application_modules={"lpac_test_app": _APP_SOURCE},
            application_requirements=("PyYAML>=6",),
        )
        yield runtime


def _responses(*cells):
    return FakeLLMClient(
        scripted_responses=[
            LLMResponse(
                raw_response=None,
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id=f"c{i}", name="execute_python", arguments=json.dumps({"code": code})
                    )
                ],
            )
            for i, code in enumerate(cells)
        ]
    )


class RecordingStrategy(_LpacCodeActStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executors = []

    def _create_sandbox_executor(self, *args):
        executor = super()._create_sandbox_executor(*args)
        self.executors.append(executor)
        return executor


def _backend(runtime, **kwargs):
    return RecordingStrategy(
        runtime,
        module_globals={"app": app, "increment": app.increment},
        data_types=(app.Request, app.Answer),
        **kwargs,
    )


def _assert_closed(backend):
    assert backend.executors
    for executor in backend.executors:
        assert executor._closed
        assert executor._proc is None and executor._conn is None and executor._io_task is None
        assert not executor._processes and not executor._tool_tasks


def _outputs(agent):
    return [e for e in agent.event_manager.values() if isinstance(e, PythonOutput)]


async def _compute(agent, *args):
    try:
        return await agent.compute(*args)
    except Exception as exc:
        exc.add_note(repr([(e.tool_call_id, e.stdout, e.error) for e in _outputs(agent)]))
        raise


async def test_typed_agent_prefill_persistence_tools_and_inline_completion(framework):
    backend = _backend(framework, tools=("scale",))

    class Demo(Agent, llm=FakeLLMClient()):
        def scale(self, item: app.Item, worker: int) -> app.Answer:
            assert isinstance(item, app.Item)
            self._received = item
            return app.Answer(value=item.amount * 10, worker=worker)

        @strategy(backend)
        async def compute(self, request: app.Request) -> app.Answer:
            """Compute using the granted scale method."""
            offset = 1  # noqa: F841 - consumed by subsequent generated cells
            ...

    agent = Demo(
        llm=_responses(
            "import os\n"
            "assert request.unit.value == 'count'\n"
            "assert 'scale' in doc(self)\n"
            "assert 'scale' in methods(self)\n"
            "assert variables(self) == ''\n"
            "amount = increment(request.item.amount) + offset",
            "answer = self.scale(app.Item(amount), os.getpid())\nreturn_result(answer)",
        )
    )
    result = await _compute(agent, app.Request(item=app.Item(2)))
    assert isinstance(result, app.Answer)
    assert result.value == 40 and result.worker != os.getpid()
    assert agent._received == app.Item(4)
    outputs = _outputs(agent)
    assert len(outputs) >= 4  # Input inspection, pre-ellipsis, two model cells.
    assert all(e.execution_status is ResultStatus.COMPLETE for e in outputs)
    assert any("Return type:" in e.stdout for e in outputs)
    _assert_closed(backend)


async def test_agent_policy_refuses_ungranted_methods_and_raw_introspection(framework):
    backend = _backend(framework, tools=("allowed",), config=CodeActConfig(max_retries=6))
    touched = []

    class Demo(Agent, llm=FakeLLMClient()):
        secret = "must-not-be-documented"

        def allowed(self):
            """Only this method is granted."""
            return 1

        def forbidden(self):
            touched.append(True)

        @strategy(backend)
        async def compute(self) -> int:
            """Exercise access policy."""
            ...

    agent = Demo(
        llm=_responses(
            "assert 'forbidden' not in doc(self)\nassert 'must-not-be-documented' not in doc(self)",
            "self.forbidden()",
            "doc(self.allowed.__globals__)",
            "import nooa.runtime.sandbox.worker as w\n"
            "broker = w._PROXY_STATE[self][0]\n"
            "broker._root = 'introspection'\n"
            "broker.call(['doc'], (['forbidden'],), {})",
            "return_result(7)",
        )
    )
    assert await agent.compute() == 7
    assert not touched
    errors = [e for e in _outputs(agent) if e.execution_status is ResultStatus.ERROR]
    assert len(errors) == 3
    _assert_closed(backend)


async def test_application_function_executes_in_child_and_source_is_readonly(framework):
    backend = _backend(framework)

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> str:
            """Inspect the staged application location."""
            ...

    agent = Demo(
        llm=_responses(
            "open(app.__file__, 'a').write('changed')",
            "assert increment.__module__ == 'lpac_test_app'\n"
            "assert increment(41) == 42\n"
            "return_result(app.__file__)",
        )
    )
    assert Path(await agent.compute()).samefile(framework.runtime / "packages/lpac_test_app.py")
    assert "PermissionError" in _outputs(agent)[0].error
    _assert_closed(backend)


async def test_cancellation_of_real_agent_reaps_worker_and_parent_tool(framework):
    backend = _backend(framework, tools=("slow",))
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Demo(Agent, llm=FakeLLMClient()):
        async def slow(self):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        @strategy(backend)
        async def compute(self) -> int:
            """Wait for the parent tool."""
            ...

    agent = Demo(llm=_responses("await self.slow()"))
    task = asyncio.create_task(agent.compute())
    try:
        await asyncio.wait_for(started.wait(), 90)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        _assert_closed(backend)
        # A new Agent call uses a fresh worker with the same explicit grants.
        assert await Demo(llm=_responses("return_result(8)")).compute() == 8
        _assert_closed(backend)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_agent_timeout_restarts_worker_with_empty_namespace(framework):
    backend = _backend(framework, config=CodeActConfig(cell_timeout=0.5))

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """Recover after a timed-out cell."""
            ...

    agent = Demo(
        llm=_responses(
            "seed = 42",
            "while True: pass",
            "try:\n    seed\nexcept NameError:\n    return_result(1)\nreturn_result(0)",
        )
    )
    assert await _compute(agent) == 1
    assert any(e.execution_status is ResultStatus.ERROR for e in _outputs(agent))
    _assert_closed(backend)


async def test_undeclared_input_data_fails_before_starting_worker(framework):
    backend = RecordingStrategy(framework)

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self, request: app.Request) -> int:
            """Reject an undeclared model."""
            ...

    with pytest.raises(SandboxUnavailable, match="not a data type"):
        await Demo(llm=_responses("return_result(1)")).compute(app.Request(item=app.Item(1)))
    assert not backend.executors


@pytest.mark.parametrize("name", ["../escape", "os", "JSON", "x..y", "CON", "a.b"])
def test_application_staging_rejects_invalid_names_and_missing_package_parents(tmp_path, name):
    destination = tmp_path / "packages"
    destination.mkdir()
    with pytest.raises(ValueError):
        _stage_applications(destination, {name: _APP_SOURCE})
    assert not list(destination.iterdir())


def test_application_staging_copies_only_explicit_files_and_package_parents(tmp_path):
    source = tmp_path / "\u5e94\u7528 source"
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "data.py").write_text("value = 42\n", encoding="utf-8")
    (source / ".env").write_text("synthetic-secret", encoding="utf-8")
    destination = tmp_path / "packages"
    destination.mkdir()
    _stage_applications(
        destination, {"example": source / "__init__.py", "example.data": source / "data.py"}
    )
    assert sorted(p.name for p in (destination / "example").iterdir()) == ["__init__.py", "data.py"]


@pytest.mark.parametrize(
    "policy",
    [
        SandboxConfig(network=True),
        SandboxConfig(max_memory_mb=512),
        SandboxConfig(max_cpu_seconds=15),
        SandboxConfig(workspace="workspace"),
        SandboxConfig(require=False),
    ],
)
def test_public_policy_is_not_silently_ignored(framework, policy):
    with pytest.raises(ValueError, match="public sandbox policy"):
        _backend(
            framework,
            config=CodeActConfig(sandbox=policy),
            memory_limit_bytes=512 * 1024 * 1024,
            cpu_time_limit_s=15,
        )


@pytest.mark.parametrize("package", [False, True])
def test_application_staging_rejects_case_aliases(tmp_path, package):
    destination = tmp_path / "packages"
    destination.mkdir()
    init = tmp_path / "__init__.py"
    init.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="collision"):
        _stage_applications(
            destination,
            {"Example": init if package else _APP_SOURCE, "example": _APP_SOURCE},
        )
    assert not list(destination.iterdir())


def test_application_staging_rejects_shared_package_shadowing(tmp_path):
    destination = tmp_path / "packages"
    (destination / "nooa").mkdir(parents=True)
    with pytest.raises(ValueError, match="shadows"):
        _stage_applications(destination, {"nooa": _APP_SOURCE})


async def test_reconfigured_internal_strategy_never_runs_in_process(framework):
    backend = _backend(framework)
    backend.config = CodeActConfig(execution_backend="inprocess")
    with pytest.raises(SandboxUnavailable, match="another backend"):
        await backend.execute(None, None)
    assert not backend.executors


async def test_unstaged_import_failure_never_falls_back_to_host(framework, tmp_path):
    backend = RecordingStrategy(
        framework,
        module_globals={"pytest": pytest},
        config=CodeActConfig(max_iterations=1),
    )
    marker = tmp_path / "must-not-be-created.txt"

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """A failed bootstrap must not execute a cell in the host."""
            ...

    agent = Demo(llm=_responses(f"open({str(marker)!r}, 'w').write('bad')"))
    with pytest.raises((GenerationError, SandboxUnavailable)):
        await agent.compute()
    assert not marker.exists()
    _assert_closed(backend)


async def test_invalid_return_value_can_be_corrected_in_real_agent(framework):
    backend = _backend(framework)

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> app.Answer:
            """Return a validated answer."""
            ...

    agent = Demo(
        llm=_responses(
            "return_result(value='not-an-int', worker=1)",
            "return_result(value=42, worker=1)",
        )
    )
    assert await _compute(agent) == app.Answer(value=42, worker=1)
    assert "validation error" in _outputs(agent)[0].stderr
    _assert_closed(backend)


async def test_third_party_native_extension_in_agent_remains_contained(framework, tmp_path):
    # Exercise kernel network denial, not CodeAct's earlier AST socket guard.
    backend = _backend(
        framework,
        config=CodeActConfig(
            restrictions=RestrictionsConfig(
                blocked_modules=RestrictionsConfig().blocked_modules - {"socket"}
            )
        ),
    )
    canary = tmp_path / "private.txt"
    canary.write_text("synthetic-secret", encoding="utf-8")

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """Parse a value using the installed application dependency."""
            ...

    agent = Demo(
        llm=_responses(
            "import yaml, yaml._yaml\n"
            "from pathlib import Path\n"
            "assert yaml.__with_libyaml__\n"
            "assert Path(yaml.__file__).samefile("
            f"{str(framework.runtime / 'packages/yaml/__init__.py')!r})\n"
            "value = yaml.load('answer: 42', Loader=yaml.CSafeLoader)['answer']",
            "open(yaml._yaml.__file__, 'ab')",
            f"open({str(canary)!r}, 'r')",
            "import socket\nsocket.socket()",
            "return_result(value)",
        )
    )
    assert await _compute(agent) == 42
    errors = [e.error for e in _outputs(agent) if e.execution_status is ResultStatus.ERROR]
    assert len(errors) == 3
    assert "PermissionError" in errors[0] and "PermissionError" in errors[1]
    assert "10013" in errors[2]
    _assert_closed(backend)


async def test_agent_tool_policy_binds_defaults_and_denies_before_effects(framework, tmp_path):
    approved = tmp_path / "approved.txt"
    approved.write_text("allowed snapshot", encoding="utf-8")
    secret = tmp_path / "private.txt"
    secret.write_text("synthetic-secret", encoding="utf-8")
    touched = []

    def permit(arguments):
        return arguments["name"] == "approved" and arguments["mode"] == "read"

    backend = _backend(framework, tools=("read",), tool_policies={"read": permit})

    class Demo(Agent, llm=FakeLLMClient()):
        def read(self, name, mode="read"):
            touched.append((name, mode))
            # Logical names avoid a check/open race against worker-chosen host paths.
            return {"approved": approved, "private": secret}[name].read_text(encoding="utf-8")

        @strategy(backend)
        async def compute(self) -> str:
            """Read only the approved named document."""
            ...

    agent = Demo(
        llm=_responses(
            "self.read('private')",
            "self.read(name='approved', mode='write')",
            "return_result(self.read(name='approved'))",
        )
    )
    assert await _compute(agent) == "allowed snapshot"
    assert touched == [("approved", "read")]
    assert len([e for e in _outputs(agent) if "policy denied" in (e.error or "")]) == 2
    _assert_closed(backend)


async def test_real_agent_reads_and_writes_only_named_file_grants(framework, tmp_path):
    from nooa.runtime.sandbox._lpac_files import _FileBroker, _FileGrant

    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"approved")
    output.write_bytes(b"")
    backend = _backend(framework, tools=("read", "write"))
    async with _FileBroker(
        {"source": _FileGrant(source), "output": _FileGrant(output, writable=True)}
    ) as files:

        class Demo(Agent, llm=FakeLLMClient()):
            async def read(self, name: str) -> bytes:
                return await files.read(name)

            async def write(self, name: str, data: bytes) -> int:
                return await files.write(name, data)

            @strategy(backend)
            async def compute(self) -> str:
                """Copy the approved named document into the granted output."""
                ...

        agent = Demo(
            llm=_responses(
                f"await self.read({str(source)!r})",
                "await self.write('source', b'bad')",
                "await self.write('output', await self.read('source'))\n"
                "return_result((await self.read('output')).decode())",
            )
        )
        assert await _compute(agent) == "approved"
        errors = [e.error for e in _outputs(agent) if e.execution_status is ResultStatus.ERROR]
        assert len(errors) == 2
        assert "not granted" in errors[0] and "read-only" in errors[1]
        _assert_closed(backend)
    assert source.read_bytes() == output.read_bytes() == b"approved"


async def test_real_agent_uses_native_job_memory_limit(framework):
    memory = 512 * 1024 * 1024
    backend = _backend(framework, memory_limit_bytes=memory, cpu_time_limit_s=60)

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """Continue after a denied allocation."""
            ...

    agent = Demo(
        llm=_responses(
            "import os\nworker = os.getpid()",
            f"bytearray({memory})",
            "assert os.getpid() == worker\nreturn_result(42)",
        )
    )
    assert await _compute(agent) == 42
    errors = [e.error for e in _outputs(agent) if e.execution_status is ResultStatus.ERROR]
    assert len(errors) == 1 and "MemoryError" in errors[0]
    assert backend.executors[0]._job_limits == {
        "memory_limit_bytes": memory,
        "cpu_time_limit_s": 60,
        "active_process_limit": 1,
    }
    _assert_closed(backend)


@pytest.mark.parametrize("recovery", ["restart_empty", "disabled"])
async def test_real_agent_native_cpu_limit_and_recovery(framework, recovery):
    backend = _backend(
        framework,
        cpu_time_limit_s=15,
        recovery=recovery,
        config=CodeActConfig(cell_timeout=None, max_iterations=3),
    )

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """Handle a terminated worker."""
            ...

    agent = Demo(
        llm=_responses(
            "seed = 42\nwhile True: pass",
            "try:\n    seed\nexcept NameError:\n    return_result(7)\nreturn_result(0)",
            "return_result(8)",
        )
    )
    async with asyncio.timeout(120):
        if recovery == "disabled":
            with pytest.raises(GenerationError):
                await agent.compute()
        else:
            assert await _compute(agent) == 7, [(e.stdout, e.error) for e in _outputs(agent)]
    errors = [e.error for e in _outputs(agent) if e.execution_status is ResultStatus.ERROR]
    assert any("WorkerDiedError" in error for error in errors)
    assert not any("CellTimeoutError" in error for error in errors)
    if recovery == "disabled":
        assert any("disabled" in error for error in errors)
    _assert_closed(backend)
