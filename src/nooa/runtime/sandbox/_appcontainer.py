# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal LPAC/stdlib acceptance launcher, not the public CodeAct backend.

Runs a private copy of CPython under a low-privilege AppContainer with only the
system registryRead capability required for DLL initialization, not network capabilities.
No host agent, site-packages, environment secrets, or broker are exposed. Input
files are snapshots; the disposable workspace is optionally writable. Framework
staging and persistent-worker integration live in the separate internal LPAC modules.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath
from typing import Literal


def _long_path(path: Path) -> Path:
    """Use extended Win32 spelling for filesystem I/O, not path authorization."""
    path = path.absolute()
    text = str(path)
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _check_source(path: Path) -> None:
    info = path.lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"runtime source must not contain reparse points: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"unsupported runtime source: {path}")


def _copy_stdlib(source: Path, destination: Path) -> None:
    excluded = {
        "site-packages",
        "__pycache__",
        "test",
        "tests",
        "ensurepip",
        "idlelib",
        "tkinter",
        "turtledemo",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in excluded or name.startswith(".")}
        for name in set(names) - skipped:
            _check_source(Path(directory) / name)
        return skipped

    _check_source(source)
    shutil.copytree(
        _long_path(source), _long_path(destination), ignore=ignore, copy_function=shutil.copyfile
    )


def _input_name(name: str) -> str:
    is_reserved = getattr(os.path, "isreserved", None)
    if (
        not name
        or name in (".", "..")
        or name[-1] in " ."
        or any(char in '<>:"/\\|?*\0' or ord(char) < 32 for char in name)
        or (is_reserved(name) if is_reserved else PureWindowsPath(name).is_reserved())
    ):
        raise ValueError(
            "input names must be plain Windows filenames, without streams or traversal"
        )
    return name


def _command(runtime: Path, code: str) -> list[str]:
    argv = [str(runtime / "pythonw.exe"), "-I", "-S", "-B", "-c", code]
    if len(subprocess.list2cmdline(argv)) >= 32767 or "\0" in code:
        raise ValueError("code exceeds the Windows command line contract")
    return argv


def _environment(workspace: Path) -> dict[str, str]:
    # Never inherit user profile, API keys, Python flags, proxies or configuration.
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "TEMP": str(workspace),
        "TMP": str(workspace),
        # Required by AppContainer creation, but need not be the host's real path.
        "LOCALAPPDATA": str(workspace),
    }


class _AppContainerPython:
    """Own one disposable LPAC profile and isolated standard-library runtime.

    Use as a context manager. Calls are serialized; separate instances get
    distinct profile SIDs. A clean close deletes only this instance's private
    tree/profile. An explicit recovery_directory enrolls new resources in a
    host-private ledger; recover_orphans() reclaims committed inactive entries.
    Abrupt host death kills its job. Without enrollment, leftover trees/profiles
    cannot be automatically recovered; uncommitted entries are also preserved.

    workspace_access controls this private tree, not a live host-directory grant.
    "read" permits listing/reading but no worker writes, including temporary files.
    "read_write" retains the disposable writable workspace. Both modes retain
    OS-granted resources and explicit parent-tool effects outside this tree.
    """

    def __init__(
        self,
        *,
        inputs: Mapping[str, bytes] | None = None,
        recovery_directory: Path | None = None,
        workspace_access: Literal["read", "read_write"] = "read_write",
        _retain: Callable[[_AppContainerPython], None] | None = None,
    ):
        if type(workspace_access) is not str or workspace_access not in ("read", "read_write"):
            raise ValueError("workspace_access must be 'read' or 'read_write'")
        if sys.platform != "win32":
            raise RuntimeError("LPAC launcher requires native Windows")
        from nooa.runtime.sandbox._win_appcontainer import Profile

        files = {_input_name(name): data for name, data in (inputs or {}).items()}
        if len({name.casefold() for name in files}) != len(files):
            raise ValueError("input snapshots contain case-insensitive filename aliases")
        if any(not isinstance(data, bytes) for data in files.values()):
            raise TypeError("input snapshots must be bytes")
        self._lock = threading.RLock()
        self._profile = None
        self._closed = False
        self._framework_staged = False
        self._lease = None
        self._workspace_access = workspace_access
        if recovery_directory is None:
            self.root = Path(tempfile.mkdtemp(prefix="nooa-lpac-")).resolve()
        else:
            from nooa.runtime.sandbox._lpac_recovery import _RuntimeLease

            self._lease = _RuntimeLease.create(recovery_directory)
            self.root = self._lease.root
        self._parent = self.root.parent
        self.runtime = self.root / "runtime"
        self.workspace = self.root / "workspace"
        self.inputs = self.root / "inputs"
        try:
            # A managed owner retains even a partially constructed runtime when
            # constructor rollback itself fails and cleanup needs another attempt.
            if _retain is not None:
                _retain(self)
            self._profile = Profile(name=self._lease.profile_name) if self._lease else Profile()
            if self._lease is not None:
                self._lease.record_profile(self._profile.name)
            self._profile.grant_owned_directory(self.root, self.root)
            self.runtime.mkdir()
            self.workspace.mkdir()
            self.inputs.mkdir()
            self._profile.grant_owned_directory(
                self.root, self.workspace, writable=workspace_access == "read_write"
            )
            base = Path(sys.base_prefix).resolve()
            _copy_stdlib(base / "Lib", self.runtime / "Lib")
            _copy_stdlib(base / "DLLs", self.runtime / "DLLs")
            binaries = [
                # Console Python initializes a console host, which conflicts
                # with the token-level child-process ban even with NO_WINDOW.
                base / "pythonw.exe",
                base / "python3.dll",
                base / f"python{sys.version_info.major}{sys.version_info.minor}.dll",
                *base.glob("vcruntime*.dll"),
            ]
            for binary in binaries:
                _check_source(binary)
                shutil.copyfile(binary, self.runtime / binary.name)
            for name, data in files.items():
                (self.inputs / name).write_bytes(data)
        except BaseException:
            self.close()
            raise

    @property
    def workspace_access(self) -> Literal["read", "read_write"]:
        """Creation-time workspace grant; changing modes requires a new runtime."""
        return self._workspace_access

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def recover_orphans(recovery_directory: Path):
        """Recover only committed, inactive entries in an explicit private directory."""
        from nooa.runtime.sandbox._lpac_recovery import recover_orphans

        return recover_orphans(recovery_directory)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._lease is not None:
                if self.root != self._lease.root:
                    raise ValueError("refusing to remove a substituted LPAC staging root")
                self._lease.cleanup(
                    self._profile.close if self._profile is not None else lambda: None
                )
                self._closed = True
                return
            if self._profile is not None:
                self._profile.close()
            if self.root.exists():
                # Never recursively remove a substituted/junction target.
                if (
                    self.root.parent != self._parent
                    or self.root.resolve() != self.root
                    or self.root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise ValueError("refusing to remove a substituted LPAC staging root")
                from nooa.runtime.sandbox._windows_cleanup import _remove_tree

                _remove_tree(self.root)
            self._closed = True

    def run(
        self,
        code: str,
        *,
        input_bytes: bytes = b"",
        timeout_s: float = 10,
        max_output_bytes: int = 1024 * 1024,
    ) -> subprocess.CompletedProcess:
        """Run isolated Python with bounded stdio. No shell or inherited environment."""
        import msvcrt

        from nooa._win_job import ProcessJob
        from nooa.runtime.sandbox._win_appcontainer import SuspendedProcess

        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        argv = _command(self.runtime, code)
        env = _environment(self.workspace)
        with self._lock, contextlib.ExitStack() as stack:
            if self._closed:
                raise RuntimeError("AppContainer launcher is closed")
            assert self._profile is not None
            streams = []
            for _ in range(3):
                read_fd, write_fd = os.pipe()
                streams.append(
                    (
                        stack.enter_context(os.fdopen(read_fd, "rb", buffering=0)),
                        stack.enter_context(os.fdopen(write_fd, "wb", buffering=0)),
                    )
                )
            (stdin_r, stdin_w), (stdout_r, stdout_w), (stderr_r, stderr_w) = streams
            child_streams = (stdin_r, stdout_w, stderr_w)
            handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in child_streams]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            job = ProcessJob(active_process_limit=1)
            process = None
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=3, thread_name_prefix="nooa-lpac-io"
            )
            overflow = threading.Event()
            budget_lock = threading.Lock()
            used = 0

            def read_output(stream):
                nonlocal used
                chunks = []
                while chunk := stream.read(65536):
                    with budget_lock:
                        used += len(chunk)
                        if used > max_output_bytes:
                            overflow.set()
                            return b""
                    chunks.append(chunk)
                return b"".join(chunks)

            def write_input():
                try:
                    view = memoryview(input_bytes)
                    while view:
                        written = stdin_w.write(view)
                        view = view[written:]
                except BrokenPipeError:
                    pass
                finally:
                    stdin_w.close()

            try:
                process = SuspendedProcess(
                    self._profile, argv, self.workspace, env, handles, job=job
                )
                for stream in child_streams:
                    stream.close()
                # Already job-owned, still suspended: no Python or user code has run.
                output = pool.submit(read_output, stdout_r)
                errors = pool.submit(read_output, stderr_r)
                sent = pool.submit(write_input)
                process.resume()
                deadline = time.monotonic() + timeout_s
                while (exitcode := process.wait(20)) is None:
                    if overflow.is_set():
                        raise RuntimeError("AppContainer output exceeded max_output_bytes")
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout_s)
                sent.result()
                stdout, stderr = output.result(), errors.result()
                if overflow.is_set():
                    raise RuntimeError("AppContainer output exceeded max_output_bytes")
                return subprocess.CompletedProcess(argv, exitcode, stdout, stderr)
            finally:
                job.close()
                if process is not None:
                    process.close()
                pool.shutdown(wait=True, cancel_futures=True)
