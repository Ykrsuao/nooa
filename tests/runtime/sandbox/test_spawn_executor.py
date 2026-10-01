# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Real native spawn tests. Deliberately NOT marked sandbox: no containment."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from nooa.runtime.sandbox._spawn import _SpawnExecutor
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import CellTimeoutError, SandboxUnavailable, WorkerDiedError
from nooa.runtime.sandbox.executor import SandboxedExecutor

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows spawn experiment")


class SpawnReport(BaseModel):
    kind: str
    count: int


class _PicklableTool:
    def __init__(self):
        self.values = []

    def record(self, value):
        self.values.append(value)
        return len(self.values)


class _LiveBag(dict):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()


class _ToolAgent:
    def __init__(self):
        self.lock = threading.Lock()
        self.closure = lambda value: value + 1
        self.value = 41
        self.bag = _LiveBag()
        self.picklable = _PicklableTool()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.worker_pid = None

    def seen(self, pid):
        self.worker_pid = pid
        self.started.set()

    def echo(self, report: SpawnReport) -> SpawnReport:
        return report

    async def double(self, value: int) -> int:
        return value * 2

    async def slow(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(60)
        finally:
            self.cancelled.set()


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("NEMO_OO_PROJECT_DIR", str(tmp_path / "project-config"))


def _executor(agent=None, *, return_type=SpawnReport, **kwargs):
    from nooa.strategies.codeact import CodeActStrategy

    agent = agent or _ToolAgent()
    # Keep the actual CodeAct closure/CurrentCall-like non-picklable state in the parent.
    call = SimpleNamespace(return_type=return_type, kwargs={"arg": 7}, lock=threading.Lock())
    builtins = CodeActStrategy()._build_builtins(SimpleNamespace(agent=agent), call)
    return _SpawnExecutor(
        agent,
        unsafe_no_isolation=True,
        module_globals={"SpawnReport": SpawnReport, "CONSTANTS": [1, 2]},
        framework_builtins={"return_result": builtins["return_result"], "_call": call, "arg": 7},
        **kwargs,
    )


@pytest.fixture
async def runner():
    ex = _executor()
    try:
        yield ex
    finally:
        await ex.aclose()


async def _value(ex, code):
    result = await ex.run_cell(code)
    assert result.success, result.error
    return result.returned_value


def test_spawn_is_internal_and_public_sandbox_still_fails_closed():
    with pytest.raises(SandboxUnavailable, match="unsafe_no_isolation"):
        _SpawnExecutor(_ToolAgent())
    for require in (False, True):
        with pytest.raises(SandboxUnavailable, match="fork"):
            SandboxedExecutor(_ToolAgent(), SandboxConfig(require=require), cell_timeout=1)
    with pytest.raises(ValueError):
        SandboxConfig(start_method="spawn")


async def test_persistent_namespace_and_parent_brokering(runner):
    assert await _value(runner, "self.value = 42\nx = arg * 2\nself.closure(x)") == 15
    assert runner._agent.value == 42
    assert await _value(runner, "x + await self.double(14)") == 42
    assert await _value(runner, "self.bag['x'] = 8\nself.bag['x']") == 8
    assert runner._agent.bag["x"] == 8
    # An individually picklable bound method must still execute on the live parent.
    assert await _value(runner, "self.picklable.record(9)") == 1
    assert runner._agent.picklable.values == [9]
    assert await _value(runner, "list(self.bag)") == ["x"]


async def test_typed_arguments_parameters_and_real_return_result(runner):
    report = await _value(runner, "self.echo(SpawnReport(kind='ok', count=arg))")
    assert report == SpawnReport(kind="ok", count=7)
    for code, expected in (
        ("return_result(42)", {"result": 42}),
        ("return_result(kind='ok', count=7)", {"kind": "ok", "count": 7}),
        ("return_result('ok', count=arg)", {"kind": "ok", "count": 7}),
    ):
        result = await runner.run_cell(code)
        assert result.error is None, result.error
        assert result.signal.result == expected
    invalid = await runner.run_cell("return_result('ok', kind='other')")
    assert "both positionally and by keyword" in str(invalid.error)
    assert await _value(runner, "_call.return_type.__name__") == "SpawnReport"


@pytest.mark.parametrize("return_type", [list[SpawnReport], dict[str, int], SpawnReport | None])
async def test_generic_return_type_snapshot(return_type):
    ex = _executor(return_type=return_type)
    try:
        assert await _value(ex, "str(_call.return_type)") == str(return_type)
        result = await ex.run_cell("return_result('ok', count=7)")
        assert "Cannot mix positional and keyword arguments" in str(result.error)
    finally:
        await ex.aclose()


async def test_introspection_and_readonly_globals(runner):
    assert "double" in await _value(runner, "doc(self)")
    assert "value" in await _value(runner, "variables(self)")
    assert "double" in await _value(runner, "methods(self)")
    assert "double" in await _value(runner, "doc(self.double)")
    assert "SpawnReport" in await _value(runner, "doc(_call.return_type)")
    result = await runner.run_cell("CONSTANTS.append(3)")
    assert "cannot mutate module-level state" in str(result.error)
    assert await _value(runner, "list(CONSTANTS)") == [1, 2]


async def test_failed_cell_keeps_state_and_error_location(runner):
    result = await runner.run_cell("kept = 17\nraise ValueError('bad')", execution_count=43)
    assert "Cell In[43], line 2" in result.error.diagnostic
    assert await _value(runner, "kept") == 17


@pytest.mark.parametrize("recovery", ["restart_empty", "disabled"])
async def test_timeout_reaps_worker_and_applies_recovery(recovery):
    ex = _executor(cell_timeout=0.2, recovery=recovery)
    try:
        assert await _value(ex, "kept = 4\nkept") == 4
        proc = ex._proc
        result = await ex.run_cell("while True:\n    pass")
        assert isinstance(result.error, CellTimeoutError)
        assert ex._proc is None and ex._io_task is None
        assert proc._closed
        result = await ex.run_cell("'kept' in globals()")
        if recovery == "disabled":
            assert "disabled" in str(result.error)
        else:
            assert result.returned_value is False
    finally:
        await ex.aclose()


@pytest.mark.parametrize(
    "code", ["self.seen(__import__('os').getpid())\nwhile True:\n    pass", "await self.slow()"]
)
async def test_cancellation_drains_reader_and_parent_tool_before_restart(code):
    agent = _ToolAgent()
    ex = _executor(agent, cell_timeout=None, broker_timeout_s=0)
    task = asyncio.create_task(ex.run_cell(code))
    try:
        await asyncio.wait_for(agent.started.wait(), 30)
        proc = ex._proc
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert ex._io_task is None and not ex._tool_tasks and proc._closed
        if "slow" in code:
            assert agent.cancelled.is_set()
        assert await _value(ex, "6 * 7") == 42
    finally:
        await ex.aclose()
        await asyncio.gather(task, return_exceptions=True)


async def test_aclose_cancels_active_cell_and_waiting_cells():
    agent = _ToolAgent()
    ex = _executor(agent, cell_timeout=None)
    running = asyncio.create_task(ex.run_cell("await self.slow()"))
    queued = None
    try:
        await asyncio.wait_for(agent.started.wait(), 30)
        queued = asyncio.create_task(ex.run_cell("1"))
        await asyncio.wait_for(ex.aclose(), 5)
        with pytest.raises(asyncio.CancelledError):
            await running
        with pytest.raises(WorkerDiedError, match="closed"):
            await queued
        assert agent.cancelled.is_set()
        assert ex._conn is None and ex._proc is None
    finally:
        await ex.aclose()
        await asyncio.gather(running, *([queued] if queued else []), return_exceptions=True)


async def test_broker_timeout_has_separate_budget():
    ex = _executor(cell_timeout=0.1, broker_timeout_s=0.3)
    try:
        result = await ex.run_cell("await self.slow()")
        assert "broker_timeout" in str(result.error)
        assert ex._agent.cancelled.is_set()
        assert await _value(ex, "21 * 2") == 42
    finally:
        await ex.aclose()


@pytest.mark.parametrize("raw", [b"\xc1", b"\x90", b"\x81\xa4type\xa5bogus"])
async def test_malformed_ipc_retires_worker(raw, runner):
    result = await runner.run_cell(
        "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
        f"_PROXY_STATE[self][0]._conn.send_bytes({raw!r})\n"
        "while True:\n    pass"
    )
    assert isinstance(result.error, WorkerDiedError)
    assert runner._io_task is None
    assert await _value(runner, "42") == 42


async def test_hostile_pickle_payload_does_not_execute_in_parent(monkeypatch, runner):
    marker = "NOOA_SPAWN_PICKLE_TEST"
    monkeypatch.delenv(marker, raising=False)
    result = await runner.run_cell(
        "import pickle\n"
        "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
        "class Bomb:\n"
        "    def __reduce__(self):\n"
        f"        return (eval, (\"__import__('os').environ.__setitem__('{marker}', 'bad')\",))\n"
        "_PROXY_STATE[self][0]._conn.send_bytes(pickle.dumps(Bomb()))\n"
        "while True:\n    pass"
    )
    assert isinstance(result.error, WorkerDiedError)
    assert marker not in os.environ
    assert await _value(runner, "42") == 42


@pytest.mark.parametrize("value", [threading.Lock(), lambda: None])
def test_unsupported_bootstrap_values_fail_explicitly(value):
    with pytest.raises(SandboxUnavailable, match="bad_value"):
        _SpawnExecutor(_ToolAgent(), unsafe_no_isolation=True, module_globals={"bad_value": value})


async def test_startup_timeout_leaves_no_worker_or_reader():
    ex = _executor(startup_timeout_s=0.001)
    try:
        with pytest.raises(SandboxUnavailable, match="startup deadline"):
            await ex.run_cell("42")
        assert ex._proc is None and ex._conn is None and ex._io_task is None
    finally:
        await ex.aclose()


async def test_job_assignment_failure_never_releases_bootstrap(monkeypatch):
    from nooa._win_job import ProcessJob

    def refuse(self, pid):
        raise OSError("test assignment refusal")

    monkeypatch.setattr(ProcessJob, "assign", refuse)
    ex = _executor()
    try:
        with pytest.raises(SandboxUnavailable, match="assignment refusal"):
            await ex.run_cell("self.value = 99")
        assert ex._agent.value == 41
        assert ex._proc is None and ex._conn is None
    finally:
        await ex.aclose()


async def test_assigned_pid_and_descendants_are_reaped(runner):
    from nooa._win_job import _image_name

    pid = await _value(runner, "__import__('os').getpid()")
    assert pid == runner._proc.pid and pid in runner._job.pids()
    child_pid = await _value(
        runner,
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys._base_executable, '-I', '-S', '-c', "
        "'import time; time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "child.pid",
    )
    assert child_pid in runner._job.pids()
    proc = runner._proc
    await runner.aclose()
    assert proc._closed
    async with asyncio.timeout(5):
        while any(_image_name(p) for p in (pid, child_pid)):
            await asyncio.sleep(0.05)


async def test_one_process_budget_counts_the_actual_worker_only():
    ex = _executor(active_process_limit=1)
    try:
        assert await _value(ex, "__import__('os').getpid()") == ex._proc.pid
        result = await ex.run_cell(
            "import subprocess, sys\n"
            "subprocess.run([sys._base_executable, '-I', '-S', '-c', 'pass'], "
            "creationflags=subprocess.CREATE_NO_WINDOW, check=True)"
        )
        assert not result.success
        assert ex._job.pids() == [ex._proc.pid]
    finally:
        await ex.aclose()


async def test_cancellation_during_startup_finishes_cleanup():
    ex = _executor()
    task = asyncio.create_task(ex.run_cell("42"))
    try:
        async with asyncio.timeout(30):
            while ex._proc is None:
                await asyncio.sleep(0)
        proc = ex._proc
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert proc._closed and ex._io_task is None and ex._conn is None
    finally:
        await ex.aclose()
        await asyncio.gather(task, return_exceptions=True)


async def test_bootstrap_import_failure_never_runs_a_cell():
    ex = _executor()
    ex._bootstrap["module"]["bad_import"] = {
        "kind": "import",
        "ref": ("nooa_nonexistent_spawn_test_module", ""),
    }
    try:
        with pytest.raises(SandboxUnavailable, match="ModuleNotFoundError"):
            await ex.run_cell("self.value = 99")
        assert ex._agent.value == 41
        assert ex._proc is None and ex._io_task is None
    finally:
        await ex.aclose()


async def test_oversized_ipc_frame_is_rejected(runner):
    result = await runner.run_cell(
        "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
        "_PROXY_STATE[self][0]._conn.send_bytes(b'x' * (33 * 1024 * 1024))"
    )
    assert isinstance(result.error, WorkerDiedError)
    assert runner._io_task is None
    assert await _value(runner, "42") == 42


async def test_malformed_broker_root_returns_error_without_killing_worker(runner):
    result = await _value(
        runner,
        "from nooa.runtime.sandbox import wire\n"
        "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
        "pipe = _PROXY_STATE[self][0]._conn\n"
        "wire.send(pipe, {'type': 'tool_call', 'kind': 'attr', 'path': ['value'], 'root': []})\n"
        "pipe.recv()['error_type']",
    )
    assert result == "CellSerializationError"
    assert await _value(runner, "42") == 42
