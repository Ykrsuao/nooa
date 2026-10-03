# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fresh Linux command processes restricted to disposable workspace snapshots.

The command starts only after Landlock/seccomp/rlimits are installed in a fresh
interpreter. The parent does not fork an Agent or use preexec_fn. Shell children
inherit restrictions and cannot change process groups, enabling group cleanup
before output pipes are drained. This is not a container or a PID namespace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import platform
import shutil
import signal
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from nooa.runtime.sandbox import guards
from nooa.runtime.sandbox.config import FileRule, SandboxConfig, resolve_spec
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.runtime.sandbox.executor import check_enforceable

_BOOTSTRAP = (
    "import json,sys;sys.path.insert(0,sys.argv[1]);"
    "from nooa.runtime.sandbox._linux_commands import _child_exec;"
    "_child_exec(json.loads(sys.argv[2]))"
)


def _command_filter(*, block_network: bool = True) -> bytes:
    """Deny sockets, group escapes and operations on unrelated host processes.

    The preceding architecture filter rejects foreign ABIs and x32. Private
    socketpairs remain usable for asyncio, but cannot be redirected to a host
    socket when networking is disabled. io_uring cannot bypass this filtering.
    """
    if platform.machine() == "x86_64":
        denied = (62, 101, 109, 112, 129, 200, 234, 272, 297, 308, 310, 311, 312)
        network_syscalls = (41, 42, 43, 46, 49, 50, 288, 307)
        sendto_syscall = 44
        fcntl_syscall, ioctl_syscall, clone_syscall, prlimit_syscall = 72, 16, 56, 302
    elif platform.machine() in ("aarch64", "arm64"):
        denied = (97, 117, 129, 130, 131, 138, 154, 157, 240, 268, 270, 271, 272)
        network_syscalls = (198, 200, 201, 202, 203, 211, 242, 269)
        sendto_syscall = 206
        fcntl_syscall, ioctl_syscall, clone_syscall, prlimit_syscall = 25, 29, 220, 261
    else:
        raise SandboxUnavailable("Linux command sandbox requires x86_64 or aarch64")
    # pidfd signaling/descriptor access and io_uring have shared syscall numbers.
    denied += (424, 425, 426, 427, 434, 438)
    if block_network:
        denied += network_syscalls
    instructions = [struct.pack("HBBI", 0x20, 0, 0, 0)]  # Load seccomp_data.nr.
    for number in denied:
        instructions.extend(
            (struct.pack("HBBI", 0x15, 0, 1, number), struct.pack("HBBI", 0x06, 0, 0, 0x5000D))
        )
    allow = struct.pack("HBBI", 0x06, 0, 0, 0x7FFF0000)
    deny = struct.pack("HBBI", 0x06, 0, 0, 0x5000D)

    def branch(number: int, body: list[bytes]) -> None:
        # Every selected body terminates with RET. Unselected branches retain
        # the syscall number in A and skip directly to the next comparison.
        instructions.append(struct.pack("HBBI", 0x15, 0, len(body), number))
        instructions.extend(body)

    # File-owner notification APIs can signal a host PID without kill().
    owner = [struct.pack("HBBI", 0x20, 0, 0, 24)]  # args[1]: fcntl command
    for operation in (8, 10, 15):  # F_SETOWN, F_SETSIG, F_SETOWN_EX
        owner.extend((struct.pack("HBBI", 0x15, 0, 1, operation), deny))
    owner.extend(
        (
            struct.pack("HBBI", 0x15, 0, 3, 4),  # F_SETFL
            struct.pack("HBBI", 0x20, 0, 0, 32),  # args[2]: flags
            struct.pack("HBBI", 0x45, 0, 1, 0x2000),  # O_ASYNC
            deny,
            allow,
        )
    )
    branch(fcntl_syscall, owner)
    ownership_ioctls = [struct.pack("HBBI", 0x20, 0, 0, 24)]
    for operation in (0x8901, 0x8902, 0x5452):  # FIOSETOWN, SIOCSPGRP, FIOASYNC
        ownership_ioctls.extend((struct.pack("HBBI", 0x15, 0, 1, operation), deny))
    ownership_ioctls.append(allow)
    branch(ioctl_syscall, ownership_ioctls)
    # Namespace clones can implicitly escape the parent's process group. Keep
    # ordinary fork/thread clone paths, with ENOSYS for clone3's opaque struct so
    # libc falls back to the inspectable classic clone syscall.
    branch(435, [struct.pack("HBBI", 0x06, 0, 0, 0x50026)])
    branch(
        clone_syscall,
        [
            struct.pack("HBBI", 0x20, 0, 0, 16),
            struct.pack("HBBI", 0x45, 0, 1, 0x7E020000),
            deny,
            allow,
        ],
    )
    # prlimit64 can change another same-user process's resource limits. pid=0
    # keeps ordinary resource.setrlimit() and explicit current-process queries.
    branch(
        prlimit_syscall,
        [
            struct.pack("HBBI", 0x20, 0, 0, 16),
            struct.pack("HBBI", 0x15, 1, 0, 0),
            deny,
            allow,
        ],
    )
    if block_network:
        # send()/asyncio wakeups use sendto with a NULL destination. A datagram
        # socketpair must not send to an explicit host address. Check both words
        # of the 64-bit args[4] pointer without dereferencing untrusted memory.
        instructions.extend(
            (
                struct.pack("HBBI", 0x15, 0, 5, sendto_syscall),
                struct.pack("HBBI", 0x20, 0, 0, 48),
                struct.pack("HBBI", 0x15, 0, 2, 0),
                struct.pack("HBBI", 0x20, 0, 0, 52),
                struct.pack("HBBI", 0x15, 1, 0, 0),
                struct.pack("HBBI", 0x06, 0, 0, 0x5000D),
            )
        )
    instructions.append(struct.pack("HBBI", 0x06, 0, 0, 0x7FFF0000))
    return b"".join(instructions)


def _configuration(workspace: Path, timeout_s: float) -> SandboxConfig:
    # Do not import DEFAULT_SYSTEM_READ_PATHS: /proc, /etc, /opt and /sys are
    # broader than the command runtime needs and may disclose host information.
    paths = {"/usr", "/bin", "/lib", "/lib64", str(Path(sys.base_prefix).resolve())}
    allowed = [FileRule(path=path) for path in sorted(paths) if Path(path).exists()]
    for path in ("/dev/null", "/dev/urandom"):
        if Path(path).exists():
            allowed.append(
                FileRule(path=path, access="read_write" if path.endswith("null") else "read")
            )
    return SandboxConfig(
        workspace=str(workspace),
        allow=tuple(allowed),
        system_paths=False,
        network=False,
        max_memory_mb=512,
        max_cpu_seconds=max(1, math.ceil(timeout_s)),
        require=True,
    )


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "PATH": f"{Path(sys.base_prefix).resolve() / 'bin'}:/usr/bin:/bin",
        "HOME": str(workspace),
        "TMPDIR": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _child_exec(payload: dict[str, Any]) -> None:
    """Trusted bootstrap entry point, never a broker exposed to generated code."""
    if sys.platform != "linux":
        raise SandboxUnavailable("Linux command bootstrap requires native Linux")
    import resource

    workspace = Path(payload["workspace"])
    config = _configuration(workspace, payload["timeout_s"])
    guards.install_guards(resolve_spec(config))
    guards._seccomp_install(_command_filter())
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = 128 if hard == resource.RLIM_INFINITY else min(128, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, limit))
    os.chdir(payload["cwd"])
    os.execve(
        "/bin/bash",
        ["bash", "--noprofile", "--norc", "-c", payload["command"]],
        _environment(workspace),
    )


async def _finish(awaitable):
    """Finish owned setup/cleanup despite repeated cancellation requests."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def _remove_workspace(workspace: Path) -> None:
    """Reclaim private trees even when a command removes directory permissions.

    All command processes have retired before this is called. Symlinks are
    removed by fd-safe rmtree; their targets are never chmodded or traversed.
    """
    if workspace.is_symlink():
        raise ValueError("refusing to clean up a substituted command workspace")
    os.chmod(workspace, 0o700, follow_symlinks=False)
    for directory, children, _ in os.walk(workspace, topdown=True, followlinks=False):
        for name in children:
            child = Path(directory) / name
            if not child.is_symlink():
                os.chmod(child, 0o700, follow_symlinks=False)
    shutil.rmtree(workspace)


class LinuxCommandSession:
    """Own a private command workspace and one running command at a time.

    Shell state is fresh for every invocation. Environment credentials, live
    source paths and host callbacks are absent. System programs/stdlib are
    readable; project dependencies must be provisioned separately. Network,
    process-group changes and signaling other processes are denied.
    """

    def __init__(self) -> None:
        self._state = "new"
        self._active = False
        self._closing = False
        self._process: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.workspace: Path

    async def __aenter__(self) -> LinuxCommandSession:
        if self._state != "new":
            raise RuntimeError("Linux command sessions cannot be reopened")
        if sys.platform != "linux":
            raise SandboxUnavailable("Linux command session requires native Linux")
        missing = check_enforceable(_configuration(Path(tempfile.gettempdir()), 30))
        if missing:
            raise SandboxUnavailable("Linux command sandbox unavailable: " + ", ".join(missing))
        _command_filter()  # Validate the architecture before provisioning.
        self._loop = asyncio.get_running_loop()
        self.workspace = Path(tempfile.mkdtemp(prefix="nooa-linux-command-")).resolve()
        self._state = "ready"
        return self

    def _check_ready(self) -> None:
        if self._state != "ready":
            raise SandboxUnavailable("Linux command session is not ready")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Linux command session belongs to another event loop")

    @staticmethod
    def _kill_group(process: asyncio.subprocess.Process) -> None:
        if sys.platform != "linux":
            raise SandboxUnavailable("Linux process groups require native Linux")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    async def _retire(self, process, readers) -> None:
        # A dead shell can still have descendants holding stdout/stderr. Kill
        # its original group before awaiting either the streams or Process.wait.
        self._kill_group(process)
        await asyncio.wait_for(asyncio.gather(*readers, process.wait()), timeout=10)
        self._process = None

    async def run(
        self,
        command: str,
        cwd: str | Path,
        timeout_s: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> dict[str, Any]:
        self._check_ready()
        if self._active:
            raise SandboxUnavailable("concurrent Linux commands are not supported")
        if self._process is not None:
            raise SandboxUnavailable("close the Linux command session after failed process cleanup")
        if type(command) is not str or not command or len(command) > 32768 or "\0" in command:
            raise ValueError("command must be nonempty text of at most 32768 characters")
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 300
        ):
            raise ValueError("timeout_s must be finite and between 0 and 300 seconds")
        if type(max_output_bytes) is not int or not 0 < max_output_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_output_bytes must be between 1 and 4 MiB")
        cwd = Path(cwd).absolute()
        if not cwd.is_relative_to(self.workspace) or cwd.resolve() != cwd or not cwd.is_dir():
            raise PermissionError(
                "command cwd must be a real directory inside the private workspace"
            )
        self._active = True
        readers: list[asyncio.Task[None]] = []
        output = [bytearray(), bytearray()]
        remaining = max_output_bytes
        truncated = False
        timed_out = False

        async def read(stream: asyncio.StreamReader, index: int) -> None:
            nonlocal remaining, truncated
            while chunk := await stream.read(65536):
                accepted = min(len(chunk), remaining)
                output[index].extend(chunk[:accepted])
                remaining -= accepted
                truncated |= accepted != len(chunk)

        async def spawn() -> asyncio.subprocess.Process:
            payload = json.dumps(
                {
                    "workspace": str(self.workspace),
                    "cwd": str(cwd),
                    "command": command,
                    "timeout_s": timeout_s,
                }
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                _BOOTSTRAP,
                str(Path(__file__).resolve().parents[3]),
                payload,
                cwd=str(self.workspace),
                env=_environment(self.workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
            self._process = process
            assert process.stdout is not None and process.stderr is not None
            readers.extend(
                (
                    asyncio.create_task(read(process.stdout, 0)),
                    asyncio.create_task(read(process.stderr, 1)),
                )
            )
            return process

        async def wait_exit(process: asyncio.subprocess.Process) -> None:
            # Process.wait can wait for inherited output pipes after the shell
            # exits. Observe returncode first so background descendants retire.
            while process.returncode is None:
                await asyncio.sleep(0.01)

        try:
            process = await _finish(spawn())
            try:
                await asyncio.wait_for(wait_exit(process), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
            returncode = process.returncode
        finally:
            try:
                if self._process is not None:
                    await _finish(self._retire(self._process, readers))
            finally:
                self._active = False
        return {
            "stdout": output[0].decode("utf-8", errors="replace"),
            "stderr": output[1].decode("utf-8", errors="replace"),
            "returncode": returncode if returncode is not None else -9,
            "timed_out": timed_out,
            "output_truncated": truncated,
        }

    async def aclose(self) -> None:
        if self._state == "closed":
            return
        if self._active:
            raise RuntimeError("await active Linux commands before closing the session")
        if self._closing:
            raise RuntimeError("Linux command session cleanup is already in progress")
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Linux command session belongs to another event loop")
        if self._state == "new":
            self._state = "closed"
            return
        self._state = "closing"
        self._closing = True

        async def cleanup():
            if self._process is not None:
                await self._retire(self._process, [])
            await asyncio.to_thread(_remove_workspace, self.workspace)
            self._state = "closed"

        try:
            await _finish(cleanup())
        finally:
            self._closing = False

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
