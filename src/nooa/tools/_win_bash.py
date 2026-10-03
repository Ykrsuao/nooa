# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Windows support for BashSession: locate MSYS2 bash and own its process tree.

On POSIX, BashSession runs ``/bin/bash``, passes it a control fd, and kills
work through a process group. None of that exists on Windows, so:

- bash is Git for Windows' MSYS2 bash (``<Git>\\usr\\bin\\bash.exe``), or any
  MSYS2 bash named by ``NOOA_BASH``. ``System32\\bash.exe`` is never used: it
  is the WSL launcher, which runs commands in a Linux VM with a different
  filesystem.
- the control channel is a loopback TCP connection that bash opens itself
  through ``/dev/tcp`` (see ``BashSession.start``).
- bash and everything it starts live in a Job Object. Killing the job's other
  members interrupts a command; closing the job kills the whole tree, which
  also happens automatically if Python dies.

Import this module only on Windows.
"""

import sys

assert sys.platform == "win32"

import asyncio  # noqa: E402
import ctypes  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from ctypes import wintypes  # noqa: E402
from pathlib import Path  # noqa: E402

from nooa._win_job import ProcessJob as ProcessJob  # noqa: E402
from nooa._win_job import _image_name as _image_name  # noqa: E402

BASH_ENV_VAR = "NOOA_BASH"
CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_ERROR_NO_MORE_FILES = 18


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _fn(name: str, restype, *argtypes):
    fn = getattr(_kernel32, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_CreateToolhelp32Snapshot = _fn(
    "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
)
_Thread32First = _fn(
    "Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)
)
_Thread32Next = _fn("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
_OpenThread = _fn("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_ResumeThread = _fn("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
_CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)


def resume_suspended_process(pid: int) -> None:
    """Resume a just-created, job-owned process without running an unowned child.

    asyncio's Popen closes CreateProcess's primary-thread handle. A process
    created with CREATE_SUSPENDED has not run its entry point, so recover that
    single thread through the documented Toolhelp API. Unexpected thread state
    fails the launch; callers must terminate and reap the process on any error.
    """
    snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    threads: list[int] = []
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = _Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                threads.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            found = _Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _CloseHandle(snapshot)
    if len(threads) != 1:
        raise RuntimeError(f"Expected one suspended Bash thread, found {len(threads)}")
    thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, threads[0])
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        previous_count = _ResumeThread(thread)
        if previous_count == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous_count != 1:
            raise RuntimeError(f"Unexpected Bash thread suspend count: {previous_count}")
    finally:
        _CloseHandle(thread)


def close_stale_pipe(transport: asyncio.BaseTransport) -> None:
    """Dispose a Proactor pipe/socket after its owning event loop has closed.

    CPython 3.12/3.13's close() schedules connection_lost before closing the OS
    handle. A dead loop cannot run that callback. Finish it synchronously; its
    finally block releases the handle even if protocol notification encounters
    the dead loop. Keep this CPython-specific fallback out of live-loop cleanup.
    """
    try:
        transport.close()
    except RuntimeError:
        pass
    disconnect = getattr(transport, "_call_connection_lost", None)
    if disconnect is not None:
        try:
            disconnect(None)
        except (RuntimeError, OSError):
            pass


def find_bash() -> Path:
    """Return the MSYS2 bash to run, or raise FileNotFoundError with install advice."""
    override = os.environ.get(BASH_ENV_VAR)
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"{BASH_ENV_VAR}={override!r} is not a file")
        return path
    for root in _git_roots():
        bash = root / "usr" / "bin" / "bash.exe"
        if bash.is_file():
            return bash
    raise FileNotFoundError(
        "Shell tools on Windows need the bash that ships with Git for Windows "
        "(https://git-scm.com/download/win). Install it, or set "
        f"{BASH_ENV_VAR} to the path of an MSYS2 bash.exe."
    )


def _git_roots() -> Iterator[Path]:
    git = shutil.which("git")
    if git:
        exe = Path(git).resolve()
        # <root>\cmd\git.exe, <root>\bin\git.exe, or <root>\mingw64\bin\git.exe
        yield exe.parent.parent
        yield exe.parent.parent.parent
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            yield Path(base) / "Git"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Programs" / "Git"


def bash_env(bash: Path, env: dict[str, str]) -> dict[str, str]:
    """Put the MSYS2 tool directories first on PATH, as Git Bash's own launcher does.

    ``--noprofile`` skips /etc/profile, so without this ``base64``, ``mktemp``
    and the rest of coreutils would not be found, and Windows' ``find.exe`` and
    ``sort.exe`` would shadow the GNU tools.

    Windows Python installs ``python`` but no ``python3``, which commands and
    ``#!/usr/bin/env python3`` shebangs expect, so when only ``python`` exists
    a ``python3`` shim that forwards to it goes last on PATH.
    """
    usr_bin = bash.parent
    root = usr_bin.parent.parent
    dirs = [str(d) for d in (root / "mingw64" / "bin", usr_bin) if d.is_dir()]
    path = os.pathsep.join([*dirs, env.get("PATH", "")])
    if shutil.which("python3", path=path) is None and shutil.which("python", path=path):
        path = os.pathsep.join([path, str(_python3_shim_dir())])
    env["PATH"] = path
    # Piped Python streams otherwise use the Windows ANSI code page, whereas
    # BashSession sends and reads UTF-8. Do not override an explicit choice.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


_PYTHON3_SHIM = b'#!/bin/sh\nexec python "$@"\n'


def _python3_shim_dir() -> Path:
    shim_dir = Path(tempfile.gettempdir()) / "nooa-bash-shims"
    shim = shim_dir / "python3"
    if not shim.is_file() or shim.read_bytes() != _PYTHON3_SHIM:
        shim_dir.mkdir(exist_ok=True)
        # Concurrent sessions may race here; each writes the same bytes.
        tmp = shim_dir / f"python3.{os.getpid()}.tmp"
        tmp.write_bytes(_PYTHON3_SHIM)
        os.replace(tmp, shim)
    return shim_dir
