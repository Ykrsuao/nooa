# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in, bounded same-volume I/O contention through the public Windows session."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import gc
import json
import os
import queue
import shutil
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from nooa import Agent, strategy
from nooa.runtime.sandbox import _lpac, _lpac_runtime
from nooa.runtime.sandbox.windows import FileGrant, WindowsSandboxPolicy, WindowsSandboxSession
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC stability")
_BLOCK = b"x" * (32 * 1024)
_MAX_WRITTEN = 512 * 1024**2
_MIN_FREE = 2 * 1024**3


class _DiskLoad:
    """Two rate-limited create/fsync/read/unlink loops; never fill the volume."""

    def __init__(self, root, enabled=True):
        self.root = root
        self.enabled = enabled
        self.stop_event = threading.Event()
        self.ready = [threading.Event() for _ in range(2)]
        self.threads = []
        self.errors = queue.SimpleQueue()
        self.cycles = [0, 0]
        self.written = [0, 0]
        self._owned = False

    def _cycle(self, path):
        with path.open("xb") as stream:
            stream.write(_BLOCK)
            stream.flush()
            os.fsync(stream.fileno())
        assert path.read_bytes() == _BLOCK
        path.unlink()

    def _run(self, index):
        path = self.root / f"load-{index}.bin"
        try:
            while not self.stop_event.is_set():
                if self.written[index] + len(_BLOCK) > _MAX_WRITTEN // 2:
                    raise RuntimeError("I/O load reached its cumulative write budget")
                if shutil.disk_usage(self.root).free < _MIN_FREE:
                    raise RuntimeError("I/O load reached its free-space floor")
                self._cycle(path)
                self.written[index] += len(_BLOCK)
                self.cycles[index] += 1
                self.ready[index].set()
                self.stop_event.wait(0.02)
        except BaseException as exc:
            self.errors.put(exc)
            self.stop_event.set()
        finally:
            self.ready[index].set()

    def _check(self):
        if not self.errors.empty():
            raise RuntimeError("I/O load failed") from self.errors.get_nowait()

    def start(self):
        if not self.enabled:
            return
        if shutil.disk_usage(self.root.parent).free < _MIN_FREE:
            raise RuntimeError("insufficient free space for bounded I/O load")
        self.root.mkdir()  # Refuse existing directories, including junctions.
        self._owned = True
        try:
            for index in range(2):
                thread = threading.Thread(
                    target=self._run, args=(index,), name=f"nooa-stability-io-{index}"
                )
                self.threads.append(thread)
                thread.start()
            assert all(event.wait(5) for event in self.ready), "I/O load never started"
            self._check()
            assert all(self.cycles), "both I/O threads must complete a real cycle"
        except BaseException:
            self.stop()
            raise

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in self.threads), "I/O thread leaked"
        if self._owned and self.root.exists():
            assert self.root.resolve() == self.root.absolute(), "I/O directory was substituted"
            for index in range(2):
                (self.root / f"load-{index}.bin").unlink(missing_ok=True)
            self.root.rmdir()  # Never recursively delete a caller-owned tree.
            self._owned = False
        self._check()


def _handle_count():
    from nooa.runtime.sandbox import _win_appcontainer as native

    gc.collect()
    get_count = native._fn(
        native._kernel,
        "GetProcessHandleCount",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    count = wintypes.DWORD()
    native._check(get_count(native._GetCurrentProcess(), ctypes.byref(count)))
    return count.value


class _Audit:
    def __init__(self, monkeypatch):
        from nooa import _win_job
        from nooa.runtime.sandbox import _win_appcontainer as native

        self.native = native
        self.runtimes = []
        self.processes = []
        self.native_processes = []
        self.profiles = []
        retain = WindowsSandboxSession._retain_runtime
        launch = _lpac.LpacProcess

        def retained(owner, runtime):
            self.runtimes.append(runtime)
            retain(owner, runtime)

        def launched(runtime, job, *args, **kwargs):
            process = launch(runtime, job, *args, **kwargs)
            # Pin the kernel process object before retirement, avoiding PID reuse.
            handle = _win_job._OpenProcess(0x100000, False, process.pid)
            if not handle:
                job.close()
                process.close()
                process.close_streams()
                raise ctypes.WinError()
            self.processes.append((process, job, handle))
            self.native_processes.append(process._native)
            return process

        monkeypatch.setattr(WindowsSandboxSession, "_retain_runtime", retained)
        monkeypatch.setattr(_lpac, "LpacProcess", launched)

    def verify(self):
        assert len(self.runtimes) == 1, "the case must own exactly one real runtime"
        for runtime in self.runtimes:
            assert runtime._closed and not runtime.root.exists()
            assert not runtime.root.parent.exists(), "recovery entry survived cleanup"
            assert runtime._profile is not None and not runtime._profile._created
            assert not runtime._profile.sid, "profile SID allocation survived cleanup"
            # A flag alone cannot establish that Windows deleted the profile.
            # Profile refuses existing names; never adopt or delete a collision.
            replacement = self.native.Profile(name=runtime._profile.name)
            self.profiles.append(replacement)
            replacement.close()
        for process, job, handle in self.processes:
            assert self.native._Wait(handle, 0) == 0, "worker still running"
            assert process._closed and process._native is None
            assert not process._stderr_thread.is_alive()
            assert process.connection._reader.closed and process.connection._writer.closed
            assert job._handle is None, "worker Job Object handle still owned"
        for native_process in self.native_processes:
            assert native_process is not None
            assert not native_process._info.process, "native process handle still owned"
            assert not native_process._info.thread, "native thread handle still owned"

    def close_profiles(self):
        for profile in tuple(self.profiles):
            profile.close()
            self.profiles.remove(profile)

    def close_pins(self):
        for _, _, handle in self.processes:
            self.native._check(self.native.CloseHandle(handle))


@contextlib.contextmanager
def _phase(report, load, name):
    before = sum(load.cycles)
    start = time.perf_counter()
    try:
        yield
    finally:
        report["phases"][name] = {
            "seconds": time.perf_counter() - start,
            "io_cycles": sum(load.cycles) - before,
        }


@pytest.fixture
def stability_case(request, tmp_path, monkeypatch, record_property):
    mode = request.param
    load = _DiskLoad(tmp_path / "pressure", enabled=mode != "idle")
    audit = _Audit(monkeypatch)
    report = {
        "mode": mode,
        "python": sys.version,
        "entry": f"{WindowsSandboxSession.__module__}.{WindowsSandboxSession.__name__}",
        "phases": {},
        "cleanup_ok": False,
    }
    before = _handle_count()
    start = time.perf_counter()
    try:
        load.start()
        yield load, audit, report
        assert all(runtime.root.drive == load.root.drive for runtime in audit.runtimes)
        audit.verify()
        load.stop()
        assert not load.root.exists(), "I/O pressure directory survived cleanup"
        report["cleanup_ok"] = True
    finally:
        try:
            load.stop()
        finally:
            try:
                audit.close_profiles()
            finally:
                audit.close_pins()
                report.update(
                    seconds=time.perf_counter() - start,
                    io_cycles=load.cycles,
                    io_bytes=sum(load.written),
                    workers=len(audit.processes),
                    handles_before=before,
                    handles_after=_handle_count(),
                )
                record_property("stability", json.dumps(report, sort_keys=True))
                print("Windows stability: " + json.dumps(report, sort_keys=True), flush=True)
    assert report["seconds"] < 180, "lifecycle exceeded the existing acceptance budget"


def _responses(*cells):
    return FakeLLMClient(
        scripted_responses=[
            LLMResponse(
                raw_response=None,
                finish_reason="tool_calls",
                parts=(
                    ToolCall(
                        id=f"c{index}", name="execute_python", arguments=json.dumps({"code": code})
                    ),
                ),
            )
            for index, code in enumerate(cells)
        ]
    )


async def _cancel_at_boundary(task, entered, release):
    try:
        assert await asyncio.to_thread(entered.wait, 60), "lifecycle boundary was not reached"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned before owned I/O finished"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled(), "lifecycle cancellation was not propagated"


@pytest.mark.stress
@pytest.mark.timeout(180)
@pytest.mark.parametrize("stability_case", ["idle", "io-1", "io-2", "io-3"], indirect=True)
async def test_repeated_managed_lifecycle_under_io(stability_case, tmp_path, monkeypatch):
    load, audit, report = stability_case
    output = tmp_path / "output.txt"
    output.write_bytes(b"before")
    policy = WindowsSandboxPolicy(
        workspace_access="read_write",
        files={"output": FileGrant(output, writable=True)},
        tools=("slow",),
        cell_timeout_s=1,
        broker_timeout_s=2,
        recovery_directory=tmp_path / "ledger",
    )
    owner = WindowsSandboxSession(
        policy,
        application_modules={"stability_app": Path(__file__).with_name("lpac_test_app.py")},
        application_requirements=("PyYAML>=6",),
    )
    started, stopped = asyncio.Event(), asyncio.Event()
    try:
        with _phase(report, load, "provision"):
            await owner.__aenter__()
        runtime = owner._runtime
        assert runtime is not None
        assert runtime.root.drive == load.root.drive
        backend = owner.strategy()

        class Demo(Agent, llm=FakeLLMClient()):
            async def slow(self):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()

            @strategy(backend)
            async def compute(self) -> int:
                """Compute using the explicit application and broker grants."""
                ...

        with _phase(report, load, "recovery"):
            agent = Demo(
                llm=_responses(
                    "import yaml, stability_app\n"
                    "assert yaml.safe_load('value: 2')['value'] == 2\n"
                    f"try:\n    open({str(output)!r}, 'rb')\n"
                    "except PermissionError:\n    pass\n"
                    "else:\n    raise AssertionError('direct host access was allowed')\n"
                    "assert await self.write_file('output', b'after') == 5\n"
                    "with open('retained.txt', 'w') as f:\n    f.write('workspace')\n"
                    "seed = 9",
                    "while True: pass",
                    "try:\n    seed\nexcept NameError:\n    pass\n"
                    "else:\n    raise AssertionError('worker globals survived replacement')\n"
                    "with open('retained.txt') as f:\n    assert f.read() == 'workspace'\n"
                    "import stability_app\nreturn_result(stability_app.increment(4))",
                )
            )
            assert await agent.compute() == 5
            assert len(audit.processes) == 2, "the timed-out worker was not replaced"
            assert not owner._executors and not owner._active
        with _phase(report, load, "cancel_call"):
            task = asyncio.create_task(Demo(llm=_responses("await self.slow()")).compute())
            try:
                await asyncio.wait_for(started.wait(), 60)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert stopped.is_set() and not owner._executors and not owner._active
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        with _phase(report, load, "fresh_call"):
            assert await Demo(llm=_responses("return_result(42)")).compute() == 42
            assert not owner._executors and not owner._active
        assert len(audit.processes) == 4
        entered, release = threading.Event(), threading.Event()
        close = runtime.close

        def close_at_boundary():
            entered.set()
            assert release.wait(10), "close gate was not released"
            close()

        monkeypatch.setattr(runtime, "close", close_at_boundary)
        with _phase(report, load, "cancel_close"):
            await _cancel_at_boundary(asyncio.create_task(owner.aclose()), entered, release)
        assert owner._state == "closed"
        await owner.aclose()
        await owner.aclose()
        assert owner._runtime is None and owner._files is None
        assert not owner._executors and not owner._active
        assert output.read_bytes() == b"after"
        output.unlink()  # A pinned broker handle must no longer prevent deletion.
        if load.enabled:
            for phase in report["phases"].values():
                assert phase["io_cycles"] > 0, "pressure stopped too early"
    finally:
        await owner.aclose()


@pytest.mark.stress
@pytest.mark.timeout(180)
@pytest.mark.parametrize("stability_case", ["idle", "io"], indirect=True)
async def test_cancel_real_staging_under_io(stability_case, tmp_path, monkeypatch):
    load, audit, report = stability_case
    entered, release = threading.Event(), threading.Event()
    package_files = _lpac_runtime._package_files

    def files_at_boundary(*args, **kwargs):
        for index, pair in enumerate(package_files(*args, **kwargs)):
            # At least 32 real copy futures have drained before reaching this gate.
            if index == 64:
                entered.set()
                assert release.wait(10), "staging gate was not released"
            yield pair

    monkeypatch.setattr(_lpac_runtime, "_package_files", files_at_boundary)
    owner = WindowsSandboxSession(
        WindowsSandboxPolicy(recovery_directory=tmp_path / "ledger"),
        application_requirements=("PyYAML>=6",),
    )
    try:
        with _phase(report, load, "cancel_provision"):
            await _cancel_at_boundary(asyncio.create_task(owner.__aenter__()), entered, release)
        assert owner._state == "closed" and owner._runtime is None
        assert not owner._executors and not audit.processes
        await owner.aclose()
        await owner.aclose()
        if load.enabled:
            assert report["phases"]["cancel_provision"]["io_cycles"] > 0
    finally:
        release.set()
        await owner.aclose()


def test_profile_audit_retains_failed_close_for_teardown(tmp_path, monkeypatch):
    audit = _Audit(monkeypatch)
    audit.runtimes.append(
        SimpleNamespace(
            _closed=True,
            root=tmp_path / "removed-entry" / "runtime",
            _profile=SimpleNamespace(_created=False, sid=None, name="owned-profile"),
        )
    )
    calls = []

    class Probe:
        def __init__(self, *, name):
            assert name == "owned-profile"
            self.closed = False

        def close(self):
            calls.append(self)
            if len(calls) == 1:
                raise OSError("injected profile delete failure")
            self.closed = True

    monkeypatch.setattr(audit.native, "Profile", Probe)
    try:
        with pytest.raises(OSError, match="injected profile delete failure"):
            audit.verify()
        assert audit.profiles == calls and not calls[0].closed
    finally:
        audit.close_profiles()
    assert len(calls) == 2 and calls[0].closed and not audit.profiles
    audit.close_profiles()
    assert len(calls) == 2


def test_io_load_performs_real_bounded_work_and_removes_only_its_directory(tmp_path):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_bytes(b"keep")
    load = _DiskLoad(tmp_path / "pressure")
    try:
        load.start()
        assert all(load.cycles)
        assert sum(load.written) <= _MAX_WRITTEN
    finally:
        load.stop()
    assert not load.root.exists() and sentinel.read_bytes() == b"keep"


def test_io_load_refuses_an_existing_directory(tmp_path):
    load = _DiskLoad(tmp_path)
    sentinel = tmp_path / "load-0.bin"
    sentinel.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        load.start()
    load.stop()
    assert sentinel.read_bytes() == b"keep"


def test_io_load_refuses_low_free_space(tmp_path, monkeypatch):
    load = _DiskLoad(tmp_path / "pressure")
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=_MIN_FREE - 1))
    with pytest.raises(RuntimeError, match="insufficient free space"):
        load.start()
    assert not load.root.exists() and not load.threads


def test_io_load_propagates_worker_failure_and_stops_threads(tmp_path, monkeypatch):
    load = _DiskLoad(tmp_path / "pressure")

    def fail(_):
        raise OSError("injected I/O failure")

    monkeypatch.setattr(load, "_cycle", fail)
    with pytest.raises(RuntimeError, match="I/O load failed"):
        load.start()
    assert not load.root.exists()
    assert all(not thread.is_alive() for thread in load.threads)


def test_io_load_enforces_cumulative_write_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_MAX_WRITTEN", 0)
    load = _DiskLoad(tmp_path / "pressure")
    with pytest.raises(RuntimeError, match="I/O load failed"):
        load.start()
    assert not any(load.written) and not load.root.exists()
