# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Resource ownership across command cancellation and event-loop replacement."""

import asyncio
import gc
import shlex
import sys

import pytest

from nooa.tools._bash_session import BashSession
from nooa.tools.shell_tools import ShellTools


async def _wait_for_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancel_reaps_command_and_readers(tmp_path, streaming):
    session = BashSession(cwd=tmp_path)
    await session.start()
    proc = session._process
    existing = asyncio.all_tasks()
    ready = tmp_path / "ready"
    command = f"echo ready > {shlex.quote(ready.as_posix())}; sleep 60"

    async def execute():
        if streaming:
            return [item async for item in session.run_stream(command)]
        return await session.run(command)

    task = asyncio.create_task(execute())
    try:
        await _wait_for_file(ready)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leaked = [t for t in asyncio.all_tasks() - existing if not t.done()]
        assert not leaked, f"Readers still running after cancellation: {leaked}"
        assert proc.returncode is not None
        assert proc.stdin.is_closing()
        assert session._process is None
        assert await session.run("printf fresh") == ("fresh", "", 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()


async def test_cancel_queued_command_does_not_stop_owner(tmp_path):
    async with BashSession(cwd=tmp_path) as session:
        ready = tmp_path / "ready"
        owner = asyncio.create_task(
            session.run(f"echo ready > {shlex.quote(ready.as_posix())}; sleep 0.3; echo owner")
        )
        try:
            await _wait_for_file(ready)
            queued = asyncio.create_task(session.run("echo queued"))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert await owner == ("owner", "", 0)
            assert session._start_count == 1
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)


async def test_concurrent_close_is_idempotent(tmp_path):
    session = BashSession(cwd=tmp_path)
    await session.start()
    proc = session._process
    await asyncio.gather(session.close(), session.close(), session.close())
    assert proc.returncode is not None
    assert proc.stdin.is_closing()
    assert session._process is None
    async with session:
        assert await session.run("echo restarted") == ("restarted", "", 0)


async def test_repeated_cancellation_cannot_interrupt_cleanup(tmp_path, monkeypatch):
    session = BashSession(cwd=tmp_path)
    await session.start()
    proc = session._process
    started = asyncio.Event()
    release = asyncio.Event()
    close = session._close_impl

    async def delayed_close():
        started.set()
        await release.wait()
        await close()

    monkeypatch.setattr(session, "_close_impl", delayed_close)
    task = asyncio.create_task(session.close())
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
        assert proc.stdin.is_closing()
        assert session._process is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()


async def test_abandoned_shell_stream_releases_session(tmp_path):
    shell = ShellTools(cwd=tmp_path)
    stream = shell.run_stream("echo output")
    try:
        assert (await anext(stream)).text.strip() == "output"
        await stream.aclose()
        assert not shell._session._lock.locked()
        assert shell._session._process is None
        assert (await shell.run("echo next")).stdout == "next"
    finally:
        await stream.aclose()
        await shell.close()


async def test_completed_shell_stream_preserves_state(tmp_path):
    shell = ShellTools(cwd=tmp_path)
    try:
        events = [event async for event in shell.run_stream("export SESSION_VALUE=retained")]
        assert events[-1].returncode == 0
        assert not shell._session._lock.locked()
        assert (await shell.run("echo $SESSION_VALUE")).stdout == "retained"
        assert shell._session._start_count == 1
    finally:
        await shell.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor transport ownership")
def test_closed_loop_recovery_closes_old_transports(tmp_path):
    session = BashSession(cwd=tmp_path)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(session.run("echo first"))
        proc = session._process
        transport = proc._transport
        pipes = [transport.get_pipe_transport(fd) for fd in (0, 1, 2)]
        control = session._control_transport
        native = transport.get_extra_info("subprocess")
    finally:
        loop.close()

    async def recover():
        try:
            assert await session.run("echo second") == ("second", "", 0)
            assert native.poll() is not None
            assert transport.is_closing()
            assert all(pipe.is_closing() for pipe in pipes)
            assert all(pipe._sock is None for pipe in pipes)
            assert control._sock is None
        finally:
            await session.close()

    asyncio.run(recover())
    gc.collect()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handle accounting")
def test_repeated_loop_replacement_does_not_leak_handles(tmp_path):
    import ctypes
    from ctypes import wintypes

    get_count = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessHandleCount
    get_count.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    get_count.restype = wintypes.BOOL

    def count():
        gc.collect()
        value = wintypes.DWORD()
        assert get_count(wintypes.HANDLE(-1), ctypes.byref(value))
        return value.value

    def cycle():
        session = BashSession(cwd=tmp_path)
        try:
            for _ in range(2):
                assert asyncio.run(session.run("echo cycle")) == ("cycle", "", 0)
        finally:
            asyncio.run(session.close())

    cycle()  # Warm up the thread pool and Windows process-wait machinery.
    before = count()
    for _ in range(6):
        cycle()
    assert count() <= before + 4
