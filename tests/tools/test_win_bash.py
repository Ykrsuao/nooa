# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Windows support for BashSession: finding MSYS2 bash, its PATH, and the Job Object."""

import asyncio
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only", allow_module_level=True)

from nooa.tools import _win_bash  # noqa: E402
from nooa.tools._bash_session import BashSession  # noqa: E402


def _make_git_install(root):
    (root / "cmd").mkdir(parents=True)
    (root / "cmd" / "git.exe").write_bytes(b"")
    (root / "usr" / "bin").mkdir(parents=True)
    bash = root / "usr" / "bin" / "bash.exe"
    bash.write_bytes(b"")
    return bash


@pytest.fixture
def no_git(monkeypatch, tmp_path):
    """No git on PATH and no Git install in the standard locations."""
    monkeypatch.delenv(_win_bash.BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(_win_bash.shutil, "which", lambda _name: None)
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "empty"))


class TestFindBash:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        bash = tmp_path / "bash.exe"
        bash.write_bytes(b"")
        monkeypatch.setenv(_win_bash.BASH_ENV_VAR, str(bash))
        assert _win_bash.find_bash() == bash

    def test_env_var_must_name_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(_win_bash.BASH_ENV_VAR, str(tmp_path / "missing.exe"))
        with pytest.raises(FileNotFoundError, match=_win_bash.BASH_ENV_VAR):
            _win_bash.find_bash()

    def test_found_next_to_git_on_path(self, no_git, monkeypatch, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        git = str(tmp_path / "Git" / "cmd" / "git.exe")
        monkeypatch.setattr(_win_bash.shutil, "which", lambda name: git if name == "git" else None)
        assert _win_bash.find_bash() == bash.resolve()

    def test_found_in_program_files(self, no_git, monkeypatch, tmp_path):
        bash = _make_git_install(tmp_path / "pf" / "Git")
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
        assert _win_bash.find_bash() == bash

    def test_never_the_wsl_launcher(self, no_git, monkeypatch, tmp_path):
        # System32\bash.exe runs WSL; being on PATH must not make it a candidate.
        system32 = tmp_path / "System32"
        system32.mkdir()
        (system32 / "bash.exe").write_bytes(b"")
        monkeypatch.setenv("PATH", str(system32))
        with pytest.raises(FileNotFoundError, match="Git for Windows"):
            _win_bash.find_bash()


class TestBashEnv:
    def test_python_streams_default_to_utf8(self, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        env = _win_bash.bash_env(bash, {})
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_explicit_python_stream_encoding_is_preserved(self, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        env = _win_bash.bash_env(bash, {"PYTHONIOENCODING": "utf-8:backslashreplace"})
        assert env["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_msys_tools_come_first(self, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        (tmp_path / "Git" / "mingw64" / "bin").mkdir(parents=True)
        env = _win_bash.bash_env(bash, {"PATH": r"C:\Windows\System32"})
        dirs = env["PATH"].split(";")
        assert dirs[:3] == [
            str(tmp_path / "Git" / "mingw64" / "bin"),
            str(bash.parent),
            r"C:\Windows\System32",
        ]

    def test_python3_shim_when_only_python_exists(self, monkeypatch, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        py_dir = tmp_path / "py"
        py_dir.mkdir()
        (py_dir / "python.exe").write_bytes(b"")
        monkeypatch.setenv("PATHEXT", ".EXE")
        env = _win_bash.bash_env(bash, {"PATH": str(py_dir)})
        shim = Path(env["PATH"].split(";")[-1]) / "python3"
        assert shim.read_bytes() == b'#!/bin/sh\nexec python "$@"\n'

    def test_no_shim_when_python3_exists(self, monkeypatch, tmp_path):
        bash = _make_git_install(tmp_path / "Git")
        py_dir = tmp_path / "py"
        py_dir.mkdir()
        (py_dir / "python.exe").write_bytes(b"")
        (py_dir / "python3.exe").write_bytes(b"")
        monkeypatch.setenv("PATHEXT", ".EXE")
        env = _win_bash.bash_env(bash, {"PATH": str(py_dir)})
        assert env["PATH"].split(";")[-1] == str(py_dir)


def _alive(pid: int) -> bool:
    return _win_bash._image_name(pid) != ""


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class TestProcessJob:
    def test_kill_descendants_spares_the_root(self):
        # The root starts a child after joining the job, so the child joins too.
        code = (
            "import subprocess, sys, time;"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(c.pid, flush=True); time.sleep(60)"
        )
        # The venv executable is a redirector: killing its descendants would
        # also kill the actual root interpreter and invalidate this assertion.
        root = subprocess.Popen(
            [sys._base_executable, "-I", "-S", "-c", f"input(); {code}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job = _win_bash.ProcessJob()
        try:
            job.assign(root.pid)
            root.stdin.write("\n")
            root.stdin.flush()
            child = int(root.stdout.readline())
            assert child in job.pids()

            assert job.kill_descendants(root.pid)
            assert _wait_dead(child)
            assert root.poll() is None

            job.close()
            assert root.wait(timeout=5) is not None
        finally:
            job.close()
            if root.poll() is None:
                root.kill()
            root.communicate(timeout=5)

    def test_close_is_idempotent(self):
        job = _win_bash.ProcessJob()
        job.close()
        job.close()
        assert job.pids() == []


class TestSessionProcessTree:
    @pytest.mark.parametrize("failed_connection", [False, True])
    async def test_startup_hook_child_belongs_to_job_and_dies_on_close(
        self, monkeypatch, tmp_path, failed_connection
    ):
        child_file = tmp_path / "startup-child.pid"
        startup = tmp_path / "startup.sh"
        child_code = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        startup.write_text(
            f"{shlex.quote(Path(sys._base_executable).as_posix())} -I -S -c "
            f"{shlex.quote(child_code)} &\n"
            f"while [ ! -s {shlex.quote(child_file.as_posix())} ]; do sleep 0.01; done\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("BASH_ENV", startup.as_posix())
        assign = _win_bash.ProcessJob.assign

        def delayed_assign(job, pid):
            # Model scheduling between CreateProcess and job assignment. Bash
            # must not run BASH_ENV during this gap, even on a slow parent.
            time.sleep(0.5)
            assign(job, pid)

        monkeypatch.setattr(_win_bash.ProcessJob, "assign", delayed_assign)
        session = BashSession(cwd=tmp_path)
        cleanup = _win_bash.ProcessJob()
        child = None
        members = []

        async def fail_connection(*args):
            nonlocal child, members
            async with asyncio.timeout(5):
                while not child_file.exists() or not child_file.read_text(encoding="utf-8"):
                    await asyncio.sleep(0.01)
            child = int(child_file.read_text(encoding="utf-8"))
            assign(cleanup, child)
            members = session._job.pids()
            raise OSError("injected connection failure")

        try:
            if failed_connection:
                monkeypatch.setattr(session, "_accept_control", fail_connection)
                with pytest.raises(OSError, match="injected connection failure"):
                    await session.start()
            else:
                await session.start()
                child = int(child_file.read_text(encoding="utf-8"))
                assign(cleanup, child)
                assert session._job is not None
                members = session._job.pids()
            await session.close()
            assert child in members, "BASH_ENV child escaped the session job during startup"
            assert _wait_dead(child), "BASH_ENV child survived session.close()"
        finally:
            await session.close()
            cleanup.close()
            if child is not None:
                assert _wait_dead(child)

    async def test_close_kills_background_jobs(self, tmp_path):
        s = BashSession(cwd=tmp_path)
        await s.start()
        await s.run("sleep 60 &")
        assert s._job is not None
        pids = [pid for pid in s._job.pids() if _win_bash._image_name(pid) == "sleep.exe"]
        assert pids, "background sleep should be in the job"
        await s.close()
        assert all(_wait_dead(pid) for pid in pids)

    async def test_timeout_interrupts_external_command(self, tmp_path):
        s = BashSession(cwd=tmp_path)
        try:
            _out, _err, code = await s.run("sleep 60", timeout=1.0)
            assert code == 124
            out, _err, code = await s.run("echo still_alive")
            assert code == 0
            assert "still_alive" in out
        finally:
            await s.close()


class TestSessionStartup:
    @pytest.mark.parametrize("stage", ["create_job", "assign", "resume"])
    async def test_failed_job_launch_reaps_suspended_bash_before_startup_hook(
        self, monkeypatch, tmp_path, stage
    ):
        marker = tmp_path / "hook-ran"
        startup = tmp_path / "startup.sh"
        startup.write_text(f"echo ran > {shlex.quote(marker.as_posix())}\n", encoding="utf-8")
        monkeypatch.setenv("BASH_ENV", startup.as_posix())
        session = BashSession(cwd=tmp_path)
        processes = []
        spawn = asyncio.create_subprocess_exec

        async def record_process(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process

        def fail(*args):
            raise OSError("injected launch failure")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
        try:
            with monkeypatch.context() as patch:
                if stage == "create_job":
                    patch.setattr(_win_bash, "ProcessJob", fail)
                elif stage == "assign":
                    patch.setattr(_win_bash.ProcessJob, "assign", fail)
                else:
                    patch.setattr(_win_bash, "resume_suspended_process", fail)
                with pytest.raises(OSError, match="injected launch failure"):
                    await session.start()
            assert session._process is None
            assert session._job is None
            assert not session._started
            assert processes[0].returncode is not None
            assert processes[0].stdin.is_closing()
            assert not marker.exists(), "BASH_ENV ran before job ownership was established"

            assert await session.run("echo recovered") == ("recovered", "", 0)
            assert marker.read_text(encoding="utf-8").strip() == "ran"
        finally:
            await session.close()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.communicate()

    async def test_repeated_startup_cancellation_finishes_cleanup(self, monkeypatch, tmp_path):
        session = BashSession(cwd=tmp_path)
        connecting = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()
        close = session._close_impl

        async def wait_for_connection(*args):
            connecting.set()
            await asyncio.Future()

        async def delayed_close():
            closing.set()
            await release.wait()
            await close()

        monkeypatch.setattr(session, "_accept_control", wait_for_connection)
        monkeypatch.setattr(session, "_close_impl", delayed_close)
        task = asyncio.create_task(session.start())
        try:
            await asyncio.wait_for(connecting.wait(), timeout=5)
            process = session._process
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert process.returncode is not None
            assert process.stdin.is_closing()
            assert session._process is None
            assert session._job is None
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.close()

    @pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError, OSError])
    async def test_failed_connection_reaps_process_and_allows_retry(
        self, monkeypatch, tmp_path, error
    ):
        session = BashSession(cwd=tmp_path)
        processes = []
        spawn = asyncio.create_subprocess_exec

        async def record_process(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process

        async def fail_connection(*args):
            raise error()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(session, "_accept_control", fail_connection)
                with pytest.raises(error):
                    await session.start()
            assert session._process is None
            assert session._job is None
            assert not session._started
            assert processes[0].returncode is not None
            assert processes[0].stdin.is_closing()

            out, err, code = await session.run("echo recovered")
            assert (out, err, code) == ("recovered", "", 0)
        finally:
            await session.close()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.communicate()


async def test_unicode_workspace_and_python_output(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    workspace = tmp_path / "\u4e2d\u6587 workspace"
    workspace.mkdir()
    async with BashSession(cwd=workspace) as session:
        out, err, code = await session.run(
            'python3 -c "import sys; print(chr(0x4e2d) + chr(0x6587)); '
            'print(chr(0x9519) + chr(0x8bef), file=sys.stderr)"'
        )
        assert (out, err, code) == ("\u4e2d\u6587", "\u9519\u8bef", 0)
        assert session.cwd == workspace


class TestControlConnection:
    async def test_oversized_untrusted_handshake_does_not_block_bash(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        task = asyncio.create_task(BashSession._accept_control(listener, "session-token"))
        clients = []
        accepted = None
        try:
            bad_reader, bad_writer = await asyncio.open_connection(*listener.getsockname())
            clients.append(bad_writer)
            bad_writer.write(b"x" * (2**20 + 1) + b"\n")
            await bad_writer.drain()
            try:
                assert await asyncio.wait_for(bad_reader.read(), timeout=5) == b""
            except ConnectionResetError:
                pass

            _, good_writer = await asyncio.open_connection(*listener.getsockname())
            clients.append(good_writer)
            good_writer.write(b"session-token\n")
            await good_writer.drain()
            _, accepted = await asyncio.wait_for(task, timeout=5)
            assert not accepted.is_closing()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()
            if accepted is not None:
                accepted.close()
                await accepted.wait_closed()
            for writer in clients:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    pass

    async def test_cancel_closes_unauthenticated_connection(self, monkeypatch):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        open_connection = asyncio.open_connection
        accepted = []
        connected = asyncio.Event()

        async def record_connection(*args, **kwargs):
            reader, writer = await open_connection(*args, **kwargs)
            accepted.append(writer)
            connected.set()
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", record_connection)
        task = asyncio.create_task(BashSession._accept_control(listener, "session-token"))
        _, client = await open_connection(*listener.getsockname())
        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert accepted[0].is_closing()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()
            client.close()
            await client.wait_closed()
            for writer in accepted:
                writer.close()
                await writer.wait_closed()
