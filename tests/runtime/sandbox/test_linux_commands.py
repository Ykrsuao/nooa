# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native shell confinement, environment separation and process-tree cleanup."""

from __future__ import annotations

import asyncio
import errno
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nooa.runtime.sandbox._linux_commands import LinuxCommandSession
from nooa.runtime.sandbox.errors import SandboxUnavailable

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="native Linux command sandbox"),
    pytest.mark.timeout(60),
]


def _python(code: str) -> str:
    return "python3 -c " + shlex.quote(code)


async def test_shell_files_and_environment_are_private(tmp_path, monkeypatch):
    secret = tmp_path / "host-secret"
    secret.write_text("private-key")
    monkeypatch.setenv("NOOA_COMMAND_SECRET", "not-for-shell")
    session = LinuxCommandSession()
    async with session:
        cwd = session.workspace / "snapshot"
        cwd.mkdir()
        (cwd / "source").write_text("before")
        result = await session.run("cat source; printf after > generated; pwd", cwd)
        assert result["returncode"] == 0, result
        assert "before" in result["stdout"] and str(cwd) in result["stdout"]
        assert (cwd / "generated").read_text() == "after"
        result = await session.run(
            _python(
                "import os; print(os.environ.get('NOOA_COMMAND_SECRET')); print(os.environ['HOME'])"
            ),
            cwd,
        )
        assert result["returncode"] == 0, result
        assert result["stdout"] == f"None\n{session.workspace}\n"
        result = await session.run("cat " + shlex.quote(str(secret)), cwd)
        assert result["returncode"] != 0 and "private-key" not in result["stdout"]
        result = await session.run("printf changed > " + shlex.quote(str(secret)), cwd)
        assert result["returncode"] != 0
        assert secret.read_text() == "private-key"
        result = await session.run("cat /proc/1/environ", cwd)
        assert result["returncode"] != 0
        with pytest.raises(PermissionError):
            await session.run("echo nope", tmp_path)
        workspace = session.workspace
    assert not workspace.exists()
    await session.aclose()
    with pytest.raises(RuntimeError, match="reopened"):
        await session.__aenter__()


async def test_network_group_escape_and_host_process_signals_denied():
    async with LinuxCommandSession() as session:
        for code in (
            "import socket; socket.socket()",
            "import socket; socket.socket(socket.AF_UNIX)",
            "import os; os.setsid()",
            "import os; os.setpgid(0,0)",
            f"import os; os.kill({os.getpid()},0)",
        ):
            result = await session.run(_python(code), session.workspace)
            assert result["returncode"] != 0, (code, result)
            assert "PermissionError" in result["stderr"], (code, result)


async def test_private_socketpair_cannot_reconnect_or_send_to_host_socket(tmp_path):
    address = str(tmp_path / "host.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sentinel:
        sentinel.bind(address)
        sentinel.setblocking(False)
        async with LinuxCommandSession() as session:
            for action in (
                f"a.connect({address!r})",
                f"a.sendto(b'leak', {address!r})",
                f"a.sendmsg([b'leak'], [], 0, {address!r})",
            ):
                result = await session.run(
                    _python(
                        "import socket; a,b=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM); "
                        + action
                    ),
                    session.workspace,
                )
                assert result["returncode"] != 0 and "PermissionError" in result["stderr"], result
                with pytest.raises(BlockingIOError):
                    sentinel.recv(100)
            # The existing connected pair's address-free send still works, as
            # does asyncio's private wakeup socketpair.
            result = await session.run(
                _python(
                    "import asyncio,socket; a,b=socket.socketpair(); a.send(b'ok'); print(b.recv(2).decode()); asyncio.run(asyncio.sleep(0))"
                ),
                session.workspace,
            )
            assert result["returncode"] == 0 and result["stdout"] == "ok\n", result


async def test_indirect_signals_foreign_rlimits_and_namespace_clones_are_denied():
    # Owner assignment alone does not send a signal. Never write to an armed
    # descriptor here, even if a regression makes an assignment unexpectedly pass.
    code = f"""
import ctypes, errno, fcntl, os, platform, resource, socket, struct, threading
parent = {os.getpid()}
def denied(operation):
    try:
        operation()
    except OSError as error:
        assert error.errno == errno.EACCES, error
    else:
        raise AssertionError('restricted operation succeeded')
r,w = os.pipe()
for command,value in [(8,parent), (10,29), (15,struct.pack('ii',1,parent)), (4,os.O_ASYNC)]:
    denied(lambda command=command,value=value: fcntl.fcntl(r,command,value))
a,b = socket.socketpair()
for command,value in [(0x8901,parent),(0x8902,parent),(0x5452,1)]:
    denied(lambda command=command,value=value: fcntl.ioctl(a,command,struct.pack('i',value)))
denied(lambda: resource.prlimit(parent,resource.RLIMIT_NOFILE))
resource.setrlimit(resource.RLIMIT_NOFILE,resource.getrlimit(resource.RLIMIT_NOFILE))
libc = ctypes.CDLL(None,use_errno=True)
clone = 56 if platform.machine() == 'x86_64' else 220
for namespace in [0x20000,0x2000000,0x4000000,0x8000000,0x10000000,0x20000000,0x40000000]:
    # CLONE_THREAD without SIGHAND/VM is invalid, preventing an actual clone
    # even if the filter regresses; the filter must reject first with EACCES.
    rc=libc.syscall(clone,ctypes.c_ulong(namespace|0x10000),0,0,0,0)
    assert rc == -1 and ctypes.get_errno() == errno.EACCES
assert libc.syscall(435,0,0) == -1 and ctypes.get_errno() == errno.ENOSYS
t=threading.Thread(target=lambda: None);t.start();t.join()
print('verified')
"""
    async with LinuxCommandSession() as session:
        result = await session.run(_python(code), session.workspace)
        assert result["returncode"] == 0 and result["stdout"] == "verified\n", result


async def test_output_is_bounded_and_nonzero_exit_visible():
    async with LinuxCommandSession() as session:
        result = await session.run(
            _python("import sys; print('x'*100000); print('bad',file=sys.stderr); sys.exit(7)"),
            session.workspace,
            max_output_bytes=128,
        )
        assert result["returncode"] == 7
        assert result["output_truncated"]
        assert len(result["stdout"].encode()) + len(result["stderr"].encode()) <= 128


def _gone(pid: int) -> bool:
    try:
        # The WSL init may not reap killed orphan grandchildren immediately.
        # A zombie cannot execute or retain output handles and is retired.
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return status.split(") ", 1)[1].startswith("Z")
    except (FileNotFoundError, ProcessLookupError):
        # procfs reports ESRCH if the process is reaped after open() but before
        # read(); both cases mean the process has already retired.
        return True


def test_gone_handles_process_reaped_after_stat_is_opened(monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    status_path = Path(f"/proc/{process.pid}/stat")
    original_open = Path.open

    def open_then_reap(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == status_path:
            process.kill()
            process.wait(timeout=5)
        return stream

    try:
        monkeypatch.setattr(Path, "open", open_then_reap)
        assert _gone(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_gone_does_not_report_live_process_as_retired():
    assert not _gone(os.getpid())


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_gone_propagates_unrelated_stat_errors(monkeypatch, error_number):
    def fail_read(path, *args, **kwargs):
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(OSError) as caught:
        _gone(os.getpid())
    assert caught.value.errno == error_number


async def test_timeout_kills_descendants_before_draining_output():
    async with LinuxCommandSession() as session:
        result = await session.run("sleep 60 & echo $!; wait", session.workspace, timeout_s=3)
        assert result["timed_out"] and result["returncode"] == -signal.SIGKILL, result
        pid = int(result["stdout"].strip())
        assert _gone(pid)
        result = await session.run("printf recovered", session.workspace)
        assert result["returncode"] == 0 and result["stdout"] == "recovered"


async def test_successful_shell_exit_also_retires_background_children():
    async with LinuxCommandSession() as session:
        result = await session.run("sleep 60 & echo $!", session.workspace, timeout_s=10)
        assert not result["timed_out"] and result["returncode"] == 0, result
        assert _gone(int(result["stdout"].strip()))


async def test_cleanup_never_follows_command_created_symlinks_and_restores_private_access(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o750)
    (outside / "keep").write_text("host content")
    permissions = outside.stat().st_mode
    async with LinuxCommandSession() as session:
        result = await session.run(
            "mkdir locked; touch locked/file; ln -s "
            + shlex.quote(str(outside))
            + " escape; chmod 000 locked",
            session.workspace,
        )
        assert result["returncode"] == 0, result
        workspace = session.workspace
    assert not workspace.exists()
    assert outside.stat().st_mode == permissions
    assert (outside / "keep").read_text() == "host content"


async def test_cancel_drains_and_rejects_concurrent_commands():
    async with LinuxCommandSession() as session:
        task = asyncio.create_task(
            session.run("sleep 60 & echo $! > child; wait", session.workspace)
        )
        for _ in range(100):
            if (session.workspace / "child").exists():
                break
            await asyncio.sleep(0.05)
        assert (session.workspace / "child").exists()
        pid = int((session.workspace / "child").read_text())
        with pytest.raises(SandboxUnavailable, match="concurrent"):
            await session.run("echo no", session.workspace)
        with pytest.raises(RuntimeError, match="active"):
            await session.aclose()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _gone(pid)
        assert session._process is None


async def test_close_cancellation_drains_and_excludes_other_lifecycle_operations(monkeypatch):
    from nooa.runtime.sandbox import _linux_commands as commands

    session = await LinuxCommandSession().__aenter__()
    started, release = threading.Event(), threading.Event()
    remove = commands.shutil.rmtree

    def delayed(path):
        started.set()
        assert release.wait(5)
        remove(path)

    monkeypatch.setattr(commands.shutil, "rmtree", delayed)
    close = asyncio.create_task(session.aclose())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(RuntimeError, match="already in progress"):
            await session.aclose()
        with pytest.raises(SandboxUnavailable, match="not ready"):
            await session.run("echo no", session.workspace)
        close.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert not session.workspace.exists()
        await session.aclose()
    finally:
        release.set()
        await session.aclose()
