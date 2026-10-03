# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native LPAC command tests in disposable workspaces, without live host grants."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nooa.runtime.sandbox._windows_command import WindowsCommandSession
from nooa.runtime.sandbox.errors import SandboxUnavailable

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC"),
    pytest.mark.timeout(120),
]


@pytest.fixture
async def session():
    async with WindowsCommandSession() as value:
        yield value


async def test_cmd_quotes_staged_python_and_workspace_writes(session):
    cwd = session.workspace / "snapshot with spaces"
    cwd.mkdir()
    result = await session.run(
        'echo command> "file with spaces.txt" & type "file with spaces.txt"', cwd
    )
    assert result["returncode"] == 0 and result["timed_out"] is False, result
    assert result["stdout"].strip() == "command"
    assert (cwd / "file with spaces.txt").read_text().strip() == "command"
    result = await session.run("python -I -X utf8 -S -c \"print('quoted text 中文')\"", cwd)
    assert result["returncode"] == 0, result
    assert result["stdout"].strip() == "quoted text 中文"
    result = await session.run("echo 中文 🚀", cwd)
    assert result["returncode"] == 0 and result["stdout"].strip() == "中文 🚀"
    assert session._runtime is not None
    python = session._runtime.runtime / "python.exe"
    result = await session.run(f'"{python}" -I -X utf8 -S -c "print(\'quoted 中文 🚀\')"', cwd)
    assert result["returncode"] == 0 and result["stdout"].strip() == "quoted 中文 🚀", result
    result = await session.run("exit /b 7", cwd)
    assert result["returncode"] == 7


async def test_builtin_and_python_output_share_utf8_and_console_is_hidden(session, monkeypatch):
    import ctypes

    from nooa.runtime.sandbox import _win_appcontainer as native

    create = native._CreateProcess
    launches = []
    expect_console = True

    def record_creation(*args):
        flags = args[5]
        startup = ctypes.cast(args[8], ctypes.POINTER(native._StartupInfoEx)).contents
        launches.append(flags)
        assert bool(flags & 0x10) == expect_console  # CREATE_NEW_CONSOLE
        assert bool(flags & 0x08000000) != expect_console  # CREATE_NO_WINDOW
        assert flags & 0x80000 and flags & 0x400 and flags & 0x4
        assert startup.info.dwFlags & 0x100  # STARTF_USESTDHANDLES
        assert bool(startup.info.dwFlags & 0x1) == expect_console
        assert startup.info.wShowWindow == 0
        return create(*args)

    monkeypatch.setattr(native, "_CreateProcess", record_creation)
    result = await session.run(
        "echo shell 中文 🚀 & "
        "python -I -X utf8 -S -c \"import sys; print('python 中文 🚀'); "
        "print('python error 中文 🚀', file=sys.stderr)\" & "
        "echo shell error 中文 🚀 1>&2",
        session.workspace,
    )
    assert result["returncode"] == 0, result
    assert [line.strip() for line in result["stdout"].splitlines()] == [
        "shell 中文 🚀",
        "python 中文 🚀",
    ]
    expected_stderr = [
        "python error 中文 🚀",
        "shell error 中文 🚀",
    ]
    allowed_stderr = [expected_stderr]
    if sys.version_info[:2] == (3, 13):
        # CPython 3.13 added a Windows getpath realpath query. LPAC can deny
        # GetFinalPathNameByHandleW even when reading/executing the file works.
        # Each of these two Python processes may emit this exact startup line.
        # Preserve product stderr and reject every other path/message/order.
        assert session._runtime is not None
        warning = f"Failed to find real location of {session._runtime.runtime / 'python.exe'}"
        allowed_stderr.extend([[warning, *expected_stderr], [warning, warning, *expected_stderr]])
    assert [line.strip() for line in result["stderr"].splitlines()] in allowed_stderr
    code = """
import ctypes as c, json
kernel = c.WinDLL('kernel32', use_last_error=True)
user = c.WinDLL('user32', use_last_error=True)
kernel.GetConsoleWindow.restype = c.c_void_p
user.IsWindowVisible.argtypes = [c.c_void_p]
window = kernel.GetConsoleWindow()
print(json.dumps([bool(window), bool(user.IsWindowVisible(window)), kernel.GetConsoleOutputCP()]))
"""
    (session.workspace / "console.py").write_text(code, encoding="utf-8")
    result = await session.run("python -I -S console.py", session.workspace)
    assert result["returncode"] == 0, result
    assert json.loads(result["stdout"]) == [True, False, 65001]
    assert len(launches) == 2
    expect_console = False
    assert session._runtime is not None
    worker = await asyncio.to_thread(session._runtime.run, "print('default worker')")
    assert worker.returncode == 0 and worker.stdout.strip() == b"default worker"
    assert len(launches) == 3


async def test_shell_lookup_ignores_snapshot_executables(session):
    # Invalid executables make accidental current-directory lookup fail loudly.
    (session.workspace / "cmd.exe").write_bytes(b"not a PE executable")
    (session.workspace / "cmd.cmd").write_text("@echo hijacked\n", encoding="ascii")
    (session.workspace / "chcp.exe").write_bytes(b"not a PE executable")
    (session.workspace / "chcp.com").write_bytes(b"not a COM executable")
    (session.workspace / "chcp.cmd").write_text("@echo hijacked\n", encoding="ascii")
    result = await session.run("echo trusted shell 中文 🚀", session.workspace)
    assert result["returncode"] == 0, result
    assert result["stdout"].strip() == "trusted shell 中文 🚀"


@pytest.mark.parametrize("name", ["chcp.com", "cmd.exe"])
async def test_bootstrap_does_not_require_system_executable_access(
    session, tmp_path, monkeypatch, name
):
    import ctypes

    from nooa.runtime.sandbox import _win_appcontainer as native

    system_program = Path(os.environ["SystemRoot"]) / "System32" / name
    denied_program = tmp_path / ("host-only-" + name)
    shutil.copyfile(system_program, denied_program)
    create = native._CreateProcess

    def deny_system_program(*args):
        # Model a runner where the LPAC token cannot execute a system helper. Only
        # replace that dependency; the host file retains its ordinary ACL.
        command = args[1].value.replace(str(system_program), str(denied_program))
        return create(args[0], ctypes.create_unicode_buffer(command), *args[2:])

    monkeypatch.setattr(native, "_CreateProcess", deny_system_program)
    result = await session.run("echo independent 中文 🚀", session.workspace)
    assert result["returncode"] == 0, result
    assert result["stdout"].strip() == "independent 中文 🚀"


async def test_two_process_limit_supports_builtins_without_expanding_job_limit():
    with pytest.raises(ValueError, match="two command processes"):
        WindowsCommandSession(active_process_limit=1)
    async with WindowsCommandSession(active_process_limit=2) as session:
        result = await session.run("echo bounded 中文 🚀", session.workspace)
        assert result["returncode"] == 0, result
        assert result["stdout"].strip() == "bounded 中文 🚀"
        result = await session.run("python -I -S -c \"print('unexpected')\"", session.workspace)
        assert result["returncode"] != 0, result
        assert "unexpected" not in result["stdout"]


async def test_child_inherits_lpac_and_cannot_read_host_connect_or_break_away(
    session, tmp_path, monkeypatch
):
    sentinel = tmp_path / "host-canary.txt"
    sentinel.write_text("synthetic host canary", encoding="utf-8")
    monkeypatch.setenv("NOOA_COMMAND_SECRET", "must not inherit")
    code = f"""
import ctypes as c, json, os, socket, subprocess, sys
from ctypes import wintypes as w
a = c.WinDLL('advapi32', use_last_error=True)
a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
token = w.HANDLE()
assert a.OpenProcessToken(w.HANDLE(-1), 8, c.byref(token))
app, size = w.DWORD(), w.DWORD()
assert a.GetTokenInformation(token, 29, c.byref(app), 4, c.byref(size))
result = {{'appcontainer': app.value, 'secret_present': 'NOOA_COMMAND_SECRET' in os.environ}}
try:
    open({str(sentinel)!r}).read()
except OSError as exc:
    result['host_errno'] = exc.errno
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1).close()
except OSError as exc:
    result['network_winerror'] = getattr(exc, 'winerror', None)
try:
    subprocess.run([sys.executable, '-I', '-S', '-c', 'pass'], timeout=5, creationflags=0x08000000 | 0x01000000)
except OSError as exc:
    result['breakaway_winerror'] = exc.winerror
print(json.dumps(result))
"""
    (session.workspace / "probe.py").write_text(code, encoding="utf-8")
    result = await session.run("python -I -S probe.py", session.workspace)
    assert result["returncode"] == 0, result
    assert json.loads(result["stdout"]) == {
        "appcontainer": 1,
        "secret_present": False,
        "host_errno": 13,
        "network_winerror": 10013,
        "breakaway_winerror": 5,
    }
    assert sentinel.read_text() == "synthetic host canary"


async def test_exited_shell_does_not_leave_background_descendants_or_block_output(session):
    from nooa._win_job import _image_name

    source = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'], creationflags=0x08000000)\n"
        "print(child.pid, flush=True)\n"
    )
    (session.workspace / "background.py").write_text(source, encoding="utf-8")
    result = await asyncio.wait_for(
        session.run("python -I -S background.py", session.workspace), 15
    )
    assert result["returncode"] == 0, result
    assert not _image_name(int(result["stdout"].strip()))


async def test_timeout_and_output_limit_leave_session_reusable(session, monkeypatch):
    early = await session.run(
        'python -I -S -c "import time; time.sleep(60)"',
        session.workspace,
        timeout_s=0.001,
    )
    # A real deadline can expire during bootstrap, before any user output.
    assert early["timed_out"] is True and early["returncode"] != 0, early
    run = session._run
    stops = []

    def record_stop(*args):
        stops.append(args[-1])
        return run(*args)

    monkeypatch.setattr(session, "_run", record_stop)
    source = (
        "import time\n"
        "from pathlib import Path\n"
        "print('started', flush=True)\n"
        "Path('output-ready').write_text('ready', encoding='ascii')\n"
        "time.sleep(60)\n"
    )
    (session.workspace / "output.py").write_text(source, encoding="utf-8")
    task = asyncio.create_task(session.run("python -I -S output.py", session.workspace))

    async def wait_ready():
        while not (session.workspace / "output-ready").exists():
            if task.done():
                pytest.fail(f"command exited before producing output: {task.result()}")
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_ready(), 10)
        # Output is known to be in the pipe before requesting native termination;
        # no assumption about bootstrap duration or interpreter startup is needed.
        stops[0].set()
        result = await asyncio.wait_for(task, 10)
    finally:
        for stop in stops:
            stop.set()
        if not task.done():
            await asyncio.wait_for(task, 10)
    assert result["returncode"] != 0, result
    assert "started" in result["stdout"]
    limited = await session.run(
        "python -I -S -c \"print('x' * 100000)\"", session.workspace, max_output_bytes=1024
    )
    assert limited["output_truncated"] is True
    assert len(limited["stdout"].encode()) + len(limited["stderr"].encode()) <= 1024
    result = await session.run("echo reusable", session.workspace)
    assert result["returncode"] == 0 and result["stdout"].strip() == "reusable"


async def test_cancel_reaps_children_before_returning_and_rejects_concurrent_calls(session):
    from nooa._win_job import _image_name

    source = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'], creationflags=0x08000000)\n"
        "Path('ready.txt').write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    (session.workspace / "cancel.py").write_text(source, encoding="utf-8")
    task = asyncio.create_task(session.run("python -I -S cancel.py", session.workspace))

    async def wait_ready():
        while not (session.workspace / "ready.txt").exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_ready(), 10)
        pid = int((session.workspace / "ready.txt").read_text())
        with pytest.raises(SandboxUnavailable, match="concurrent"):
            await session.run("echo refused", session.workspace)
        with pytest.raises(RuntimeError, match="active"):
            await session.aclose()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert not _image_name(pid)
        result = await session.run("echo after cancellation", session.workspace)
        assert result["stdout"].strip() == "after cancellation"
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_invalid_cwd_and_command_are_rejected_before_launch(session, tmp_path):
    with pytest.raises(ValueError, match="private"):
        await session.run("echo refused", tmp_path)
    with pytest.raises(ValueError, match="NUL"):
        await session.run("echo\0invalid", session.workspace)
    with pytest.raises(ValueError, match="limit"):
        await session.run("echo " + "x" * 32767, session.workspace)
    for timeout in (0, float("inf"), True):
        with pytest.raises(ValueError, match="timeout"):
            await session.run("echo refused", session.workspace, timeout_s=timeout)
    junction = session.workspace / "outside-junction"
    subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(tmp_path)],
        check=True,
        capture_output=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        with pytest.raises(OSError):
            await session.run("echo denied> escaped.txt", junction)
        assert not (tmp_path / "escaped.txt").exists()
    finally:
        junction.rmdir()


async def test_failed_execution_cleanup_keeps_owner_until_close_retry(session, monkeypatch):
    from nooa.runtime.sandbox import _windows_command as commands

    close = commands._CommandExecution.close
    calls = 0

    def fail_once(execution):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("execution cleanup failed")
        close(execution)

    root = session.workspace.parent
    monkeypatch.setattr(commands._CommandExecution, "close", fail_once)
    with pytest.raises(OSError, match="execution cleanup failed"):
        await session.run("echo finished", session.workspace)
    assert len(session._executions) == 1
    with pytest.raises(SandboxUnavailable, match="not ready"):
        await session.run("echo refused", root / "workspace")
    await session.aclose()
    assert not root.exists() and not session._executions and calls == 2


async def test_close_removes_runtime_and_retries_failed_native_cleanup(monkeypatch):
    session = await WindowsCommandSession().__aenter__()
    private_root = session.workspace.parent
    assert session._runtime is not None
    close = session._runtime.close
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("profile cleanup failed")
        close()

    monkeypatch.setattr(session._runtime, "close", fail_once)
    with pytest.raises(OSError, match="cleanup failed"):
        await session.aclose()
    assert private_root.exists()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        await session.run("echo refused", private_root / "workspace")
    await session.aclose()
    await session.aclose()
    assert not private_root.exists() and calls == 2
