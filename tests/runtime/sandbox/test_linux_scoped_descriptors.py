# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit-tool Linux sessions discard inherited host descriptor capabilities."""

from __future__ import annotations

import os
import socket
import sys

import pytest

from nooa import Agent
from nooa.runtime.restrictions import RestrictionsConfig
from nooa.runtime.sandbox._linux_session import _ManagedLinuxExecutor
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.unifiedllm import FakeLLMClient

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(sys.platform != "linux", reason="Linux fork")]


class _Tools(Agent, llm=FakeLLMClient()):
    def echo(self, value: int) -> int:
        return value

    def forbidden(self) -> None:
        raise AssertionError("not granted")


def _executor(workspace, *, tools=("echo",), cell_timeout: float = 5):
    return _ManagedLinuxExecutor(
        _Tools(),
        SandboxConfig(workspace=str(workspace), timeout_grace_s=0.1),
        tools=tools,
        cell_timeout=cell_timeout,
    )


async def test_scoped_worker_cannot_read_or_write_inherited_host_file(tmp_path):
    import fcntl

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "host.txt"
    outside.write_bytes(b"private host bytes")
    with outside.open("r+b", buffering=0) as file:
        # Linux-only APIs are absent from Windows type stubs.
        inherited = getattr(fcntl, "fcntl")(file.fileno(), getattr(fcntl, "F_DUPFD_CLOEXEC"), 512)  # noqa: B009
        executor = _executor(workspace)
        try:
            result = await executor.run_cell(
                "import os\n"
                "denied = []\n"
                f"for action in [lambda: os.read({inherited}, 100), lambda: os.write({inherited}, b'escape')]:\n"
                "    try:\n"
                "        action()\n"
                "    except OSError as exc:\n"
                "        denied.append(exc.errno)\n"
                "denied\n"
            )
            assert result.success, result.error
            assert result.returned_value == [9, 9]
            assert outside.read_bytes() == b"private host bytes"
        finally:
            await executor.aclose()
            os.close(inherited)


async def test_scoped_worker_cannot_send_on_inherited_connected_socket(tmp_path):
    import fcntl

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname()) as outgoing:
            incoming, _ = listener.accept()
            with incoming:
                incoming.settimeout(0.2)
                outgoing.sendall(b"host control")
                assert incoming.recv(32) == b"host control"
                inherited = getattr(fcntl, "fcntl")(  # noqa: B009 - Linux-only runtime API
                    outgoing.fileno(),
                    getattr(fcntl, "F_DUPFD_CLOEXEC"),  # noqa: B009 - Linux-only runtime API
                    512,
                )
                executor = _executor(tmp_path)
                try:
                    result = await executor.run_cell(
                        "import os\n"
                        "denied = None\n"
                        "try:\n"
                        f"    os.write({inherited}, b'escaped')\n"
                        "except OSError as exc:\n"
                        "    denied = exc.errno\n"
                        "denied\n"
                    )
                    assert result.success, result.error
                    assert result.returned_value == 9
                    with pytest.raises(TimeoutError):
                        incoming.recv(32)
                finally:
                    await executor.aclose()
                    os.close(inherited)


async def test_scoped_protocol_tools_and_recovery_survive_descriptor_cleanup(tmp_path):
    executor = _executor(tmp_path, cell_timeout=0.2)
    try:
        result = await executor.run_cell("self.echo(8)")
        assert result.success and result.returned_value == 8, result.error
        result = await executor.run_cell("self.forbidden()")
        assert not result.success and "not granted" in str(result.error)
        result = await executor.run_cell(
            "from nooa.runtime.sandbox.worker import _PROXY_STATE\n_PROXY_STATE[self][1] is None\n"
        )
        assert result.success and result.returned_value is True, result.error
        result = await executor.run_cell("while True: pass")
        assert not result.success
        result = await executor.run_cell("self.echo(9)")
        assert result.success and result.returned_value == 9, result.error
    finally:
        await executor.aclose()


async def test_scoped_worker_cannot_signal_parent_or_spawn_process_but_threads_work(tmp_path):
    executor = _ManagedLinuxExecutor(
        _Tools(),
        SandboxConfig(workspace=str(tmp_path)),
        tools=("echo",),
        cell_timeout=5,
        restrictions=RestrictionsConfig(blocked_modules=frozenset(), blocked_calls={}),
    )
    try:
        result = await executor.run_cell(
            "import os, subprocess, socket\n"
            "denied = {}\n"
            "actions = {\n"
            f"    'signal': lambda: os.kill({os.getpid()}, 0),\n"
            "    'fork': os.fork,\n"
            "    'subprocess': lambda: subprocess.run(['/bin/true']),\n"
            "    'unix_socket': lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),\n"
            "}\n"
            "for name, action in actions.items():\n"
            "    try:\n"
            "        value = action()\n"
            "        if name == 'fork':\n"
            "            if value == 0:\n"
            "                os._exit(0)\n"
            "            os.waitpid(value, 0)\n"
            "    except OSError as exc:\n"
            "        denied[name] = exc.errno\n"
            "denied['thread_result'] = await asyncio.to_thread(lambda: 7)\n"
            "denied\n"
        )
        assert result.success, result.error
        assert result.returned_value == {
            "signal": 13,
            "fork": 13,
            "subprocess": 13,
            "unix_socket": 13,
            "thread_result": 7,
        }
    finally:
        await executor.aclose()


async def test_scoped_raw_stdio_cannot_steal_or_forge_host_transport(tmp_path):
    source_r, source_w = os.pipe()
    sink_r, sink_w = os.pipe()
    saved_in, saved_out = os.dup(0), os.dup(1)
    executor = _executor(tmp_path)
    result = None
    try:
        os.write(source_w, b"private host protocol")
        os.dup2(source_r, 0)
        os.dup2(sink_w, 1)
        result = await executor.run_cell(
            "import os\n"
            "data = os.read(0, 100)\n"
            "os.write(1, b'forged host protocol')\n"
            "print('captured cell output')\n"
            "data\n"
        )
    finally:
        os.dup2(saved_in, 0)
        os.dup2(saved_out, 1)
        for fd in (saved_in, saved_out, source_r, source_w, sink_w):
            os.close(fd)
        await executor.aclose()
    try:
        assert result is not None and result.success, getattr(result, "error", None)
        assert result.returned_value == b""
        assert "captured cell output" in result.stdout
        assert b"forged host protocol" not in os.read(sink_r, 1024)
    finally:
        os.close(sink_r)


async def test_scoped_explicit_network_permission_still_allows_sockets(tmp_path):
    executor = _ManagedLinuxExecutor(
        _Tools(),
        SandboxConfig(workspace=str(tmp_path), network=True),
        tools=(),
        cell_timeout=5,
        restrictions=RestrictionsConfig(blocked_modules=frozenset(), blocked_calls={}),
    )
    try:
        result = await executor.run_cell(
            "import socket\n"
            "with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:\n"
            "    connection.bind(('127.0.0.1', 0))\n"
            "    port = connection.getsockname()[1]\n"
            "port > 0\n"
        )
        assert result.success and result.returned_value is True, result.error
    finally:
        await executor.aclose()
