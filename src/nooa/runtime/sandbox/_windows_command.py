# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private LPAC command execution in disposable snapshots, without host shell access."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nooa.runtime.sandbox._appcontainer import _AppContainerPython, _check_source, _environment
from nooa.runtime.sandbox.errors import SandboxUnavailable

if TYPE_CHECKING:
    from nooa._win_job import ProcessJob
    from nooa.runtime.sandbox._win_appcontainer import SuspendedProcess


_COMMAND_BOOTSTRAP = """
import ctypes, subprocess, sys

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
for name in ('SetConsoleCP', 'SetConsoleOutputCP'):
    operation = getattr(kernel, name)
    operation.argtypes = [ctypes.c_uint]
    operation.restype = ctypes.c_int
    if not operation(65001):
        error = ctypes.WinError(ctypes.get_last_error())
        print(f'Windows command console setup failed ({name}): {error}', file=sys.stderr)
        sys.exit(1)

try:
    result = subprocess.run(
        sys.argv[2], executable=sys.argv[1],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, close_fds=True,
    )
except OSError as error:
    print(f'Windows command shell launch failed: {error}', file=sys.stderr)
    sys.exit(1)
sys.exit(result.returncode)
"""


async def _await_owned(task: asyncio.Task, stop: threading.Event | None = None):
    """Cancellation requests stop work, then wait until owned resources retire."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if stop is not None:
                stop.set()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _decode_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # The shell and configured tools use UTF-8. Programs that explicitly
        # retain the machine OEM code page still get a best-effort fallback.
        return data.decode("oem", "replace")


class _CommandExecution:
    def __init__(self):
        self.stack = contextlib.ExitStack()
        self.job: ProcessJob | None = None
        self.process: SuspendedProcess | None = None
        self.pool: concurrent.futures.ThreadPoolExecutor | None = None
        self.child_streams = []
        self._process_handles: list[int] = []

    def stop_processes(self):
        import ctypes

        from nooa._win_job import _OpenProcess
        from nooa.runtime.sandbox._win_appcontainer import CloseHandle, _Wait

        if self.job is not None:
            try:
                for pid in self.job.pids():
                    handle = _OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
                    if handle:
                        self._process_handles.append(handle)
                    elif ctypes.get_last_error() != 87:  # A process can already have exited.
                        raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.job.close()
                self.job = None
        for handle in tuple(self._process_handles):
            result = _Wait(handle, 5000)
            if result == 258:
                raise TimeoutError("Windows command descendant did not terminate")
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            CloseHandle(handle)
            self._process_handles.remove(handle)

    def close(self):
        # Descendants can retain stdio after cmd exits. Kill the whole Job before
        # waiting on readers; retain every field if cleanup fails for a retry.
        self.stop_processes()
        if self.process is not None:
            self.process.close()
            self.process = None
        for stream in self.child_streams:
            stream.close()
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None
        self.stack.close()


class WindowsCommandSession:
    """Own an isolated command runtime and its writable private snapshot directory.

    Callers copy validated input into a fresh child of ``workspace`` and inspect
    changes after run() returns. This class never grants access to live host
    project directories. Each command gets a new LPAC process tree and Job.
    Two process slots are required for the UTF-8 console setup process and the
    command shell; external programs require additional slots.
    The wall-clock command timeout includes shell and program startup.
    """

    def __init__(
        self,
        *,
        memory_limit_bytes: int = 512 * 1024 * 1024,
        cpu_time_limit_s: int = 60,
        active_process_limit: int = 16,
        recovery_directory: Path | None = None,
    ):
        for name, value in (
            ("memory_limit_bytes", memory_limit_bytes),
            ("cpu_time_limit_s", cpu_time_limit_s),
            ("active_process_limit", active_process_limit),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if active_process_limit < 2:
            raise ValueError("active_process_limit must allow at least two command processes")
        self._limits = {
            "memory_limit_bytes": memory_limit_bytes,
            "cpu_time_limit_s": cpu_time_limit_s,
            "active_process_limit": active_process_limit,
        }
        self._recovery_directory = recovery_directory
        self._runtime: _AppContainerPython | None = None
        self._executions: list[_CommandExecution] = []
        self._state = "new"
        self._active = False
        self._closing = False
        self._loop = None

    @property
    def workspace(self) -> Path:
        self._require_ready()
        assert self._runtime is not None
        return self._runtime.workspace

    def _check_loop(self):
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Windows command session belongs to another event loop")

    def _require_ready(self):
        self._check_loop()
        if self._state != "ready":
            raise SandboxUnavailable("Windows command session is not ready")

    def _retain_runtime(self, runtime):
        self._runtime = runtime

    def _provision(self):
        runtime = _AppContainerPython(
            workspace_access="read_write",
            recovery_directory=self._recovery_directory,
            _retain=self._retain_runtime,
        )
        source = Path(sys.base_prefix).resolve() / "python.exe"
        _check_source(source)
        shutil.copyfile(source, runtime.runtime / "python.exe")
        # System binaries can have different LPAC execute ACLs across Windows
        # installations. Copy only this trusted shell into the existing private
        # read-only runtime; never grant access to host system directories.
        source = Path(_environment(runtime.workspace)["SystemRoot"]) / "System32" / "cmd.exe"
        _check_source(source)
        shutil.copyfile(source, runtime.runtime / "cmd.exe")

    async def __aenter__(self):
        if self._state != "new":
            raise RuntimeError("Windows command sessions cannot be reopened")
        if sys.platform != "win32":
            raise SandboxUnavailable("Windows command session requires native Windows")
        self._loop = asyncio.get_running_loop()
        self._state = "opening"
        try:
            await _await_owned(asyncio.create_task(asyncio.to_thread(self._provision)))
        except BaseException:
            self._state = "closing"
            await self.aclose()
            raise
        self._state = "ready"
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def run(
        self,
        command: str,
        cwd: Path,
        *,
        timeout_s: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> dict[str, Any]:
        self._require_ready()
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("command must be nonempty text without NUL")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError("timeout_s must be finite and positive")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        cwd = Path(cwd)
        if not cwd.is_absolute() or ".." in cwd.parts or not cwd.is_relative_to(self.workspace):
            raise ValueError("command cwd must be inside the private command workspace")
        if self._active:
            raise SandboxUnavailable("concurrent Windows commands are not supported")
        stop = threading.Event()
        self._active = True
        try:
            task = asyncio.create_task(
                asyncio.to_thread(self._run, command, cwd, timeout_s, max_output_bytes, stop)
            )
            return await _await_owned(task, stop)
        finally:
            self._active = False

    def _run(self, command, cwd, timeout_s, max_output_bytes, stop):
        import msvcrt

        from nooa._win_job import ProcessJob
        from nooa.runtime.sandbox._lpac_directories import _PinnedDirectory
        from nooa.runtime.sandbox._win_appcontainer import SuspendedProcess

        runtime = self._runtime
        assert runtime is not None and runtime._profile is not None
        env = _environment(runtime.workspace)
        system32 = Path(env["SystemRoot"]) / "System32"
        env.update(
            PATH=f"{runtime.runtime};{system32}",
            PATHEXT=".COM;.EXE;.BAT;.CMD",
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
        )
        shell = str(runtime.runtime / "cmd.exe")
        # cmd parses its /c tail using different quoting from CommandLineToArgvW.
        shell_line = '"' + shell + '" /d /s /c "' + command + '"'
        # Set the private console's code page before cmd starts and caches it.
        # The fixed bootstrap uses Win32 directly: no chcp executable, NUL
        # redirection or second cmd parse is needed to initialize the console.
        argv = [
            str(runtime.runtime / "python.exe"),
            "-I",
            "-X",
            "utf8",
            "-S",
            "-B",
            "-c",
            _COMMAND_BOOTSTRAP,
            shell,
            shell_line,
        ]
        command_line = subprocess.list2cmdline(argv)
        if len(command_line) >= 32767:
            raise ValueError("command exceeds the Windows command line limit")
        execution = _CommandExecution()
        self._executions.append(execution)
        try:
            pinned = execution.stack.enter_context(_PinnedDirectory(runtime.workspace))
            for part in cwd.relative_to(runtime.workspace).parts:
                pinned = execution.stack.enter_context(
                    _PinnedDirectory(pinned.path / part, parent=pinned)
                )
            streams = []
            for _ in range(3):
                read_fd, write_fd = os.pipe()
                streams.append(
                    (
                        execution.stack.enter_context(os.fdopen(read_fd, "rb", buffering=0)),
                        execution.stack.enter_context(os.fdopen(write_fd, "wb", buffering=0)),
                    )
                )
            (stdin_r, stdin_w), (stdout_r, stdout_w), (stderr_r, stderr_w) = streams
            execution.child_streams = [stdin_r, stdout_w, stderr_w]
            handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in execution.child_streams]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            execution.job = ProcessJob(**self._limits)
            execution.process = SuspendedProcess(
                runtime._profile,
                argv,
                cwd,
                env,
                handles,
                job=execution.job,
                allow_child_processes=True,
                command_line=command_line,
                hidden_console=True,
            )
            for stream in execution.child_streams:
                stream.close()
            stdin_w.close()
            execution.pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="nooa-lpac-command-io"
            )
            overflow = threading.Event()
            output_lock = threading.Lock()
            used = 0

            def read_output(stream):
                nonlocal used
                chunks = []
                while chunk := stream.read(65536):
                    with output_lock:
                        remaining = max_output_bytes - used
                        used += min(remaining, len(chunk))
                        if len(chunk) > remaining:
                            overflow.set()
                    if remaining:
                        chunks.append(chunk[:remaining])
                return b"".join(chunks)

            output = execution.pool.submit(read_output, stdout_r)
            errors = execution.pool.submit(read_output, stderr_r)
            execution.process.resume()
            deadline = time.monotonic() + timeout_s
            timed_out = False
            while (exitcode := execution.process.wait(20)) is None:
                timed_out = time.monotonic() >= deadline
                if timed_out or stop.is_set() or overflow.is_set():
                    break
            execution.stop_processes()
            execution.process.close()
            # close() reaps root and descendants before stdio reader completion.
            stdout, stderr = output.result(), errors.result()
            return {
                "stdout": _decode_output(stdout),
                "stderr": _decode_output(stderr),
                "returncode": exitcode if exitcode is not None else 1,
                "timed_out": timed_out,
                "output_truncated": overflow.is_set(),
            }
        finally:
            try:
                execution.close()
            except BaseException:
                self._state = "closing"
                raise
            else:
                self._executions.remove(execution)

    def _cleanup(self):
        for execution in tuple(self._executions):
            execution.close()
            self._executions.remove(execution)
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        self._state = "closed"

    async def aclose(self):
        self._check_loop()
        if self._state == "closed":
            return
        if self._active or self._state == "opening" or self._closing:
            raise RuntimeError("await active Windows command operations before closing")
        self._state = "closing"
        self._closing = True
        try:
            await _await_owned(asyncio.create_task(asyncio.to_thread(self._cleanup)))
        finally:
            self._closing = False
