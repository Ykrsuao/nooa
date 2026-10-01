# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Real LPAC framework bootstrap and persistent-worker acceptance."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys

import pytest

from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac import _LpacExecutor
from nooa.runtime.sandbox._lpac_runtime import stage_framework
from nooa.runtime.sandbox.errors import SandboxUnavailable, WorkerDiedError

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC"),
    pytest.mark.timeout(180),
]


@pytest.fixture(scope="module")
def framework():
    from nooa.runtime.sandbox import _appcontainer

    make_temp = _appcontainer.tempfile.mkdtemp

    def unicode_temp(**kwargs):
        kwargs["prefix"] += "\u9694\u79bb "
        return make_temp(**kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_appcontainer.tempfile, "mkdtemp", unicode_temp)
        runtime = _AppContainerPython()
    with runtime:
        packages = stage_framework(runtime)
        yield runtime, packages


def test_framework_bootstrap_and_socketless_asyncio(framework):
    runtime, packages = framework
    result = runtime.run(
        "import sys, os\n"
        "sys.stderr.reconfigure(encoding='utf-8')\n"
        f"sys.path.insert(0, {str(packages)!r})\n"
        "os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'\n"
        "os.environ['PYTHON_DOTENV_DISABLED'] = '1'\n"
        "import nooa, json, asyncio\n"
        "async def work():\n"
        "    await asyncio.sleep(0.01)\n"
        "    return await asyncio.to_thread(lambda: 42)\n"
        "print(json.dumps({'value': asyncio.run(work()), 'source': nooa.__file__}))\n",
        # Cold imports of the staged dependency closure can exceed 30 seconds.
        # Match CodeAct's LPAC startup budget; this is not a cell deadline test.
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert json.loads(result.stdout) == {
        "value": 42,
        "source": str(packages / "nooa" / "__init__.py"),
    }


@pytest.fixture
async def runner(framework):
    runtime, _ = framework
    executor = _LpacExecutor(runtime)
    try:
        yield executor
    finally:
        await executor.aclose()


async def _value(executor, code):
    result = await executor.run_cell(code)
    assert result.success, result.error
    return result.returned_value


async def test_persistent_cells_and_async_thread_wakeup(runner):
    pid = await _value(runner, "import os\nx = 40\nos.getpid()")
    assert pid != os.getpid()
    assert (
        await _value(runner, "await asyncio.sleep(0.01)\nx + await asyncio.to_thread(lambda: 2)")
        == 42
    )
    assert await _value(runner, "os.getpid()") == pid


async def test_tools_are_explicit_parent_capabilities(framework):
    runtime, _ = framework
    received = []

    def record(value):
        received.append(value)
        return {"pid": os.getpid(), "count": len(received)}

    executor = _LpacExecutor(runtime, tools={"record": record})
    try:
        assert await _value(executor, "self.record(42)") == {"pid": os.getpid(), "count": 1}
        for code in (
            "self.missing()",
            "self.record.__globals__",
            "self.secret = 3",
            "self.__dict__",
        ):
            # Proxy-local __dict__ must never contain the live host tool or agent.
            result = await executor.run_cell(code)
            if code == "self.__dict__":
                assert result.returned_value == {}
            else:
                assert not result.success
        assert received == [42]
    finally:
        await executor.aclose()


async def test_direct_file_and_network_access_remain_denied(runner, tmp_path):
    canary = tmp_path / "private.txt"
    canary.write_bytes(b"synthetic")
    file_result = await runner.run_cell(f"open({str(canary)!r}, 'rb').read()")
    assert not file_result.success
    assert "PermissionError" in str(file_result.error)
    network = await runner.run_cell("__import__('socket').socket()")
    assert not network.success
    assert "10013" in str(network.error)


async def test_timeout_restarts_with_empty_namespace(runner):
    assert await _value(runner, "x = 42\nx") == 42
    runner._cell_timeout = 0.2
    result = await runner.run_cell("while True: pass")
    assert not result.success
    runner._cell_timeout = 10
    assert await _value(runner, "'x' in globals()") is False


async def test_partial_frame_retires_worker_without_blocking_loop(framework):
    executor = _LpacExecutor(framework[0], frame_timeout_s=0.2)
    try:
        await _value(executor, "42")
        async with asyncio.timeout(5):
            result = await executor.run_cell(
                "import os, time\nos.write(1, b'\\x00')\ntime.sleep(60)"
            )
        assert not result.success
        assert await _value(executor, "42") == 42
    finally:
        await executor.aclose()


async def test_cancellation_reaps_worker_and_can_restart(runner):
    pid = await _value(runner, "__import__('os').getpid()")
    task = asyncio.create_task(runner.run_cell("while True: pass"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._proc is None and runner._conn is None and runner._io_task is None
    assert await _value(runner, "__import__('os').getpid()") != pid
    await runner.aclose()
    with pytest.raises(WorkerDiedError):
        await runner.run_cell("42")


async def test_parent_async_tool_is_cancelled_with_worker(framework):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    executor = _LpacExecutor(framework[0], tools={"slow": slow})
    task = asyncio.create_task(executor.run_cell("await self.slow()"))
    try:
        await asyncio.wait_for(started.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert not executor._tool_tasks
    finally:
        await executor.aclose()
        await asyncio.gather(task, return_exceptions=True)


async def test_raw_broker_requests_cannot_traverse_host_objects(framework):
    touched = []
    executor = _LpacExecutor(framework[0], tools={"record": lambda: touched.append(True)})
    try:
        for request in (
            {"kind": "call", "path": ["record", "__globals__"]},
            {"kind": "attr", "path": []},
            {"kind": "setattr", "path": ["record"]},
            {"kind": "iter", "path": ["record"]},
            {"kind": "call", "root": "introspection", "path": ["doc"]},
        ):
            result = await executor.run_cell(
                "import nooa.runtime.sandbox.worker as worker\n"
                "broker = worker._PROXY_STATE[self][0]\n"
                f"broker._root = {request.get('root', 'agent')!r}\n"
                f"broker._rpc({dict(type='tool_call', **request)!r})"
            )
            assert not result.success
            assert "not explicitly granted" in str(result.error)
        assert touched == []
        assert await _value(executor, "42") == 42
    finally:
        await executor.aclose()


async def test_live_host_objects_cannot_be_returned(framework):
    from types import SimpleNamespace

    executor = _LpacExecutor(
        framework[0], tools={"object": lambda: SimpleNamespace(secret="synthetic")}
    )
    try:
        result = await executor.run_cell("self.object()")
        assert not result.success
        assert "data snapshots" in str(result.error)
    finally:
        await executor.aclose()


async def test_worker_pickle_payload_is_not_executed_in_parent(runner, tmp_path):
    import pickle

    marker = tmp_path / "must-not-exist.txt"

    class Payload:
        def __reduce__(self):
            return eval, (f"__import__('pathlib').Path({str(marker)!r}).write_text('bad')",)

    raw = pickle.dumps(Payload())
    result = await runner.run_cell(
        "import nooa.runtime.sandbox.worker as worker\n"
        f"worker._PROXY_STATE[self][0]._conn.send_bytes({raw!r})"
    )
    assert not result.success
    assert not marker.exists()
    assert await _value(runner, "42") == 42


async def test_bootstrap_does_not_import_unstaged_application_modules(framework):
    executor = _LpacExecutor(framework[0], module_globals={"pytest": pytest})
    try:
        with pytest.raises(SandboxUnavailable, match="pytest"):
            await executor.run_cell("42")
        assert executor._proc is None and executor._conn is None
    finally:
        await executor.aclose()


async def test_sanitized_environment_and_readonly_framework(runner, monkeypatch):
    monkeypatch.setenv("NOOA_LPAC_CANARY", "synthetic-secret")
    env = await _value(runner, "dict(__import__('os').environ)")
    assert "NOOA_LPAC_CANARY" not in env
    assert "USERPROFILE" not in env
    assert "PATH" not in env
    source = runner._packages / "nooa/__init__.py"
    result = await runner.run_cell(f"open({str(source)!r}, 'ab')")
    assert not result.success
    child = await runner.run_cell(
        "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])"
    )
    assert not child.success


async def test_asyncio_many_cross_thread_wakeups_and_repeated_loops(runner):
    assert (
        await _value(
            runner,
            "values = await asyncio.gather(*(asyncio.to_thread(lambda: 1) for _ in range(100)))\n"
            "sum(values)",
        )
        == 100
    )
    assert await _value(runner, "await asyncio.sleep(0.01)\n42") == 42


async def test_assignment_failure_releases_process_and_streams(runner, monkeypatch):
    from nooa.runtime.sandbox import _win_appcontainer as native

    update = native._UpdateAttribute

    def refuse(*args):
        if args[2] == native._PROC_THREAD_ATTRIBUTE_JOB_LIST:
            raise OSError("synthetic assignment failure")
        return update(*args)

    with monkeypatch.context() as patch:
        patch.setattr(native, "_UpdateAttribute", refuse)
        with pytest.raises(SandboxUnavailable, match="assignment failure"):
            await runner.run_cell("42")
    assert runner._proc is None and runner._conn is None
    assert await _value(runner, "42") == 42


async def test_real_completion_callback_stays_in_parent(framework):
    import threading
    from types import SimpleNamespace

    from nooa.strategies.codeact import CodeActStrategy

    call = SimpleNamespace(return_type=int, kwargs={}, lock=threading.Lock())
    builtins = CodeActStrategy()._build_builtins(SimpleNamespace(agent=SimpleNamespace()), call)
    executor = _LpacExecutor(
        framework[0],
        framework_builtins={"return_result": builtins["return_result"], "_call": call},
    )
    try:
        result = await executor.run_cell("return_result(42)")
        assert result.signal is not None
        assert result.signal.result == {"result": 42}
    finally:
        await executor.aclose()


async def test_disabled_recovery_does_not_restart(framework):
    executor = _LpacExecutor(framework[0], cell_timeout=0.2, recovery="disabled")
    try:
        await _value(executor, "42")
        assert not (await executor.run_cell("while True: pass")).success
        assert executor._proc is None
        assert not (await executor.run_cell("42")).success
        assert executor._proc is None
    finally:
        await executor.aclose()


async def test_raw_stderr_overflow_retires_worker(runner, monkeypatch):
    import threading

    from nooa._win_job import ProcessJob

    close = ProcessJob.close
    callers = []

    def record_close(job):
        callers.append(threading.current_thread().name)
        close(job)

    monkeypatch.setattr(ProcessJob, "close", record_close)
    async with asyncio.timeout(30):
        result = await runner.run_cell("import os\nwhile True: os.write(2, b'x' * 8192)")
    assert not result.success
    assert "nooa-lpac-stderr" not in callers
    assert await _value(runner, "42") == 42


def test_staged_runtime_excludes_host_install_hooks(framework):
    runtime, packages = framework
    assert not list(packages.rglob("*.pth"))
    assert not list(packages.rglob("direct_url.json"))
    assert not list(packages.rglob(".env"))
    with pytest.raises(FileExistsError):
        stage_framework(runtime)


async def test_tool_policy_applies_to_raw_requests_and_async_tools(framework):
    touched = []
    checked = []

    async def fetch(endpoint="public", *, limit=1):
        touched.append((endpoint, limit))
        return limit

    def permit(arguments):
        checked.append(dict(arguments))
        return (
            arguments["endpoint"] == "public"
            and type(arguments["limit"]) is int
            and 0 < arguments["limit"] <= 2
        )

    executor = _LpacExecutor(framework[0], tools={"fetch": fetch}, tool_policies={"fetch": permit})
    try:
        assert await _value(executor, "await self.fetch()") == 1
        for args, kwargs in (
            (("private",), {}),
            ((), {"endpoint": "public", "limit": 3}),
            (("public",), {"endpoint": "private"}),  # Invalid duplicate binding.
        ):
            result = await executor.run_cell(
                "import nooa.runtime.sandbox.worker as w\n"
                "broker = w._PROXY_STATE[self][0]\n"
                f"broker.call(['fetch'], {args!r}, {kwargs!r})"
            )
            assert not result.success
            assert "policy denied" in str(result.error)
        assert touched == [("public", 1)]
        assert checked[0] == {"endpoint": "public", "limit": 1}
        assert await _value(executor, "await self.fetch(limit=2)") == 2
    finally:
        await executor.aclose()


@pytest.mark.parametrize("decision", [None, False, 1, "yes"])
async def test_tool_policy_requires_literal_true(framework, decision):
    touched = []
    executor = _LpacExecutor(
        framework[0],
        tools={"record": lambda: touched.append(True)},
        tool_policies={"record": lambda args: decision},
    )
    try:
        request = executor._decode_tool_call(
            {
                "root": "agent",
                "kind": "call",
                "path": ["record"],
                "payload": executor._codec.dumps(((), {})),
            }
        )
        result = await executor._dispatch_tool_call(request)
        assert result["error_type"] == "PermissionError"
        assert not touched
    finally:
        await executor.aclose()


async def test_tool_policy_exception_and_disguised_coroutine_fail_closed(framework):
    touched = []

    def broken(args):
        raise RuntimeError("private policy diagnostics")

    async def permit(args):
        return True

    for policy in (broken, lambda args: permit(args)):
        executor = _LpacExecutor(
            framework[0],
            tools={"record": lambda: touched.append(True)},
            tool_policies={"record": policy},
        )
        try:
            result = await executor._dispatch_tool_call(
                {"root": "agent", "kind": "call", "path": ["record"], "args": (), "kwargs": {}}
            )
            assert result == {
                "ok": False,
                "error_type": "PermissionError",
                "error": "LPAC tool policy denied record",
            }
            assert not touched
        finally:
            await executor.aclose()


def test_invalid_tool_policies_fail_at_construction(framework):
    async def permit(args):
        return True

    with pytest.raises(ValueError, match="explicitly granted"):
        _LpacExecutor(framework[0], tool_policies={"missing": lambda args: True})
    with pytest.raises(TypeError, match="synchronous"):
        _LpacExecutor(
            framework[0], tools={"record": lambda: None}, tool_policies={"record": permit}
        )


@pytest.mark.parametrize("limited", [False, True])
async def test_native_job_memory_limits_apply_at_creation(
    framework, monkeypatch, tmp_path, limited
):
    from nooa import _win_job
    from nooa.runtime.sandbox import _win_appcontainer as native

    size = 512 * 1024 * 1024
    executor = _LpacExecutor(
        framework[0],
        memory_limit_bytes=size if limited else 0,
        cpu_time_limit_s=60 if limited else 0,
        startup_timeout_s=60,
    )
    create = native._CreateProcess
    observed = []

    def verify_creation(*args):
        result = create(*args)
        if not result:
            return result
        process = ctypes.cast(args[-1], ctypes.POINTER(native._ProcessInfo)).contents
        job = executor._job
        assert job.pids() == [process.pid]
        info = _win_job._ExtendedLimitInformation()
        assert _win_job._QueryInformationJobObject(
            job._handle,
            _win_job._JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        basic = info.BasicLimitInformation
        expected_flags = (
            _win_job._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _win_job._JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        if limited:
            expected_flags |= (
                _win_job._JOB_OBJECT_LIMIT_JOB_MEMORY
                | _win_job._JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _win_job._JOB_OBJECT_LIMIT_JOB_TIME
            )
        assert basic.LimitFlags == expected_flags
        assert basic.ActiveProcessLimit == 1
        assert info.ProcessMemoryLimit == info.JobMemoryLimit == (size if limited else 0)
        assert basic.PerJobUserTimeLimit == (60 * 10_000_000 if limited else 0)
        observed.append(process.pid)
        return result

    monkeypatch.setattr(native, "_CreateProcess", verify_creation)
    try:
        pid = await _value(executor, "__import__('os').getpid()")
        # The request alone equals the entire cap, so interpreter overhead counts.
        status = await _value(
            executor,
            "try:\n"
            f"    allocated = bytearray({size})\n"
            "except MemoryError:\n"
            "    status = 'denied'\n"
            "else:\n"
            "    del allocated\n"
            "    status = 'allocated'\n"
            "status",
        )
        assert status == ("denied" if limited else "allocated")
        assert await _value(executor, "__import__('os').getpid()") == pid
        if limited:
            canary = tmp_path / "private.txt"
            canary.write_bytes(b"synthetic-secret")
            for code, expected in (
                (f"open({str(canary)!r}, 'rb').read()", "PermissionError"),
                ("__import__('socket').socket()", "10013"),
            ):
                result = await executor.run_cell(code)
                assert not result.success and expected in str(result.error)
            assert await _value(
                executor,
                "import subprocess, sys\n"
                "try:\n"
                "    subprocess.run([sys.executable, '-c', 'pass'])\n"
                "except OSError as exc:\n"
                "    status = exc.winerror\n"
                "else:\n"
                "    status = 'allowed'\n"
                "status",
            ) in (5, 367, 1260)
        assert observed == [pid]
    finally:
        await executor.aclose()
    assert executor._job is None and not executor._processes


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory_limit_bytes", -1),
        ("memory_limit_bytes", 1 << (8 * ctypes.sizeof(ctypes.c_size_t))),
        ("memory_limit_bytes", True),
        ("memory_limit_bytes", 1.5),
        ("cpu_time_limit_s", -1),
        ("cpu_time_limit_s", (1 << 63) // 10_000_000 + 1),
        ("cpu_time_limit_s", True),
        ("cpu_time_limit_s", 1.5),
    ],
)
async def test_invalid_job_limits_never_create_a_worker(framework, monkeypatch, name, value):
    from nooa import _win_job
    from nooa.runtime.sandbox import _lpac

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid limits must not allocate a job or launch a worker")

    monkeypatch.setattr(_win_job, "_CreateJobObjectW", unexpected)
    monkeypatch.setattr(_lpac, "LpacProcess", unexpected)
    executor = _LpacExecutor(framework[0], **{name: value})
    try:
        with pytest.raises(SandboxUnavailable, match=name):
            await executor.run_cell("42")
        assert executor._job is None and executor._proc is None and executor._conn is None
        assert not executor._processes
    finally:
        await executor.aclose()


async def test_failed_job_limit_installation_never_launches_lpac(framework, monkeypatch):
    from nooa import _win_job
    from nooa.runtime.sandbox import _lpac

    created, closed = [], []
    create, close = _win_job._CreateJobObjectW, _win_job._CloseHandle

    def record_create(*args):
        handle = create(*args)
        created.append(handle)
        return handle

    def record_close(handle):
        closed.append(handle)
        return close(handle)

    def refuse(*args):
        ctypes.set_last_error(5)
        return False

    def unexpected(*args, **kwargs):
        pytest.fail("Failed limit installation must not launch a worker")

    monkeypatch.setattr(_win_job, "_CreateJobObjectW", record_create)
    monkeypatch.setattr(_win_job, "_CloseHandle", record_close)
    monkeypatch.setattr(_win_job, "_SetInformationJobObject", refuse)
    monkeypatch.setattr(_lpac, "LpacProcess", unexpected)
    executor = _LpacExecutor(framework[0], memory_limit_bytes=512 * 1024 * 1024)
    try:
        with pytest.raises(SandboxUnavailable, match="startup failed"):
            await executor.run_cell("42")
        assert len(created) == 1 and closed == created
        assert executor._job is None and executor._proc is None and not executor._processes
    finally:
        await executor.aclose()


async def test_insufficient_startup_memory_fails_closed(framework):
    touched = []
    executor = _LpacExecutor(
        framework[0],
        memory_limit_bytes=1,
        tools={"record": lambda: touched.append(True)},
        recovery="disabled",
    )
    try:
        with pytest.raises(SandboxUnavailable):
            await executor.run_cell("self.record()")
        assert executor._proc is None and executor._job is None and executor._conn is None
        assert executor._io_task is None and not executor._processes
        result = await executor.run_cell("self.record()")
        assert not result.success and "disabled" in str(result.error)
        assert not touched and executor._proc is None
    finally:
        await executor.aclose()


@pytest.mark.parametrize("recovery", ["restart_empty", "disabled"])
async def test_native_job_cpu_spans_cells_and_obeys_recovery(framework, recovery):
    limit = 15
    memory = 512 * 1024 * 1024
    executor = _LpacExecutor(
        framework[0],
        memory_limit_bytes=memory,
        cpu_time_limit_s=limit,
        cell_timeout=None,
        startup_timeout_s=60,
        recovery=recovery,
    )
    try:
        async with asyncio.timeout(120):
            pid = await _value(executor, "import os\nseed = 42\nos.getpid()")
            job = executor._job
            await _value(
                executor,
                f"while os.times().user < {limit - 3}:\n    pass\nos.times().user",
            )
            assert executor._job is job
            # With no cell deadline, only the native job budget stops this loop.
            result = await executor.run_cell("while True: pass")
            assert not result.success and isinstance(result.error, WorkerDiedError)
            assert executor._job is None and executor._proc is None
            assert executor._conn is None and executor._io_task is None
            assert not executor._processes
            if recovery == "disabled":
                result = await executor.run_cell("42")
                assert not result.success and "disabled" in str(result.error)
                assert executor._proc is None
            else:
                assert await _value(executor, "__import__('os').getpid()") != pid
                assert executor._job is not job
                assert await _value(executor, "'seed' in globals()") is False
                assert executor._job_limits == {
                    "memory_limit_bytes": memory,
                    "cpu_time_limit_s": limit,
                    "active_process_limit": 1,
                }
                assert await _value(
                    executor,
                    f"try:\n    bytearray({memory})\n"
                    "except MemoryError:\n    denied = True\n"
                    "else:\n    denied = False\n"
                    "denied",
                )
    finally:
        await executor.aclose()
