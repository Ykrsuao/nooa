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
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

from nooa._win_job import ProcessJob as ProcessJob  # noqa: E402
from nooa._win_job import _image_name as _image_name  # noqa: E402

BASH_ENV_VAR = "NOOA_BASH"


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
