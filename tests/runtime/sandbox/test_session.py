# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native dispatch, fail-closed policy and managed Linux lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from nooa import Agent
from nooa.config import CodeActConfig
from nooa.runtime.restrictions import DEFAULT_BLOCKED_MODULES, RestrictionsConfig
from nooa.runtime.sandbox import SandboxConfig, SandboxSession, SandboxUnavailable
from nooa.runtime.sandbox import _linux_session as linux
from nooa.runtime.sandbox import session as facade
from nooa.runtime.sandbox.errors import CellSerializationError
from nooa.runtime.sandbox.guards import probe_capabilities
from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall


@pytest.mark.parametrize("platform", ["darwin", "freebsd13"])
def test_unsupported_platform_has_no_fallback(monkeypatch, platform):
    monkeypatch.setattr(facade.sys, "platform", platform)
    with pytest.raises(SandboxUnavailable, match="Linux and Windows only"):
        SandboxSession(SandboxConfig())


@pytest.mark.parametrize("platform,backend", [("win32", "linux"), ("linux", "windows")])
def test_explicit_platform_mismatch(monkeypatch, platform, backend):
    monkeypatch.setattr(facade.sys, "platform", platform)
    with pytest.raises(SandboxUnavailable, match="cannot run"):
        SandboxSession(SandboxConfig(), backend=backend)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_auto_dispatch_preserves_native_policy(monkeypatch, platform):
    monkeypatch.setattr(facade.sys, "platform", platform)
    policy = SandboxConfig() if platform == "linux" else WindowsSandboxPolicy()
    session = SandboxSession(policy)
    assert session.policy == policy
    assert session.backend == ("linux" if platform == "linux" else "windows")
    assert isinstance(
        session._session,
        linux._LinuxSandboxSession if platform == "linux" else WindowsSandboxSession,
    )


def test_linux_does_not_accept_windows_native_grants(monkeypatch):
    monkeypatch.setattr(facade.sys, "platform", "linux")
    with pytest.raises(TypeError, match="requires"):
        SandboxSession(WindowsSandboxPolicy())


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="backend"):
        SandboxSession(SandboxConfig(), backend="spawn")


@pytest.fixture
def linux_host(monkeypatch):
    monkeypatch.setattr(facade.sys, "platform", "linux")
    monkeypatch.setattr(linux.mp, "get_all_start_methods", lambda: ["fork"])
    monkeypatch.setattr(linux, "check_enforceable", lambda policy: [])


def test_linux_rejects_dropped_guards(linux_host):
    with pytest.raises(ValueError, match="require=True"):
        SandboxSession(SandboxConfig(require=False))


@pytest.mark.parametrize(
    "kwargs", [{"application_modules": {}}, {"application_requirements": ["nooa"]}]
)
def test_linux_rejects_windows_staging(linux_host, kwargs):
    with pytest.raises(ValueError, match="Windows"):
        SandboxSession(SandboxConfig(), **kwargs)


@pytest.mark.parametrize(
    "config",
    [
        CodeActConfig(execution_backend="sandbox"),
        CodeActConfig(sandbox=SandboxConfig(network=True)),
    ],
)
def test_linux_rejects_conflicting_policy_in_generation_config(linux_host, config):
    with pytest.raises(ValueError, match="session policy"):
        SandboxSession(SandboxConfig(), config=config)


def test_linux_revalidates_copied_policy(linux_host):
    with pytest.raises(ValueError):
        SandboxSession(SandboxConfig().model_copy(update={"start_method": "spawn"}))


async def test_unavailable_guard_blocks_session_entry(linux_host, monkeypatch):
    monkeypatch.setattr(linux, "check_enforceable", lambda policy: ["Landlock unavailable"])
    session = SandboxSession(SandboxConfig())
    with pytest.raises(SandboxUnavailable, match="Landlock unavailable"):
        await session.__aenter__()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        session.strategy()
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        await session.__aenter__()
    await session.aclose()


async def test_strategy_lifetime_and_configuration(linux_host):
    policy = SandboxConfig(network=True)
    session = SandboxSession(policy, config=CodeActConfig(cell_timeout=7))
    with pytest.raises(SandboxUnavailable, match="not ready"):
        session.strategy()
    async with session:
        strategy = session.strategy()
        assert strategy.config.execution_backend == "sandbox"
        assert strategy.config.sandbox == policy
        assert strategy.config.cell_timeout == 7
        with pytest.raises(ValueError, match="Windows"):
            session.strategy(module_globals={})
        with pytest.raises(ValueError, match="Windows"):
            session.strategy(data_types=(int,))
    with pytest.raises(SandboxUnavailable, match="not ready"):
        with strategy.call_scope():
            pass
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        await session.__aenter__()
    await session.aclose()


async def test_sequential_calls_admit_but_nested_or_competing_calls_reject(linux_host):
    async with SandboxSession(SandboxConfig()) as session:
        strategy = session.strategy()
        other_strategy = session.strategy()
        for _ in range(2):
            with strategy.call_scope():
                with strategy.call_scope(nested=True):
                    pass
                with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
                    with other_strategy.call_scope():
                        pass

                async def compete():
                    with strategy.call_scope(nested=True):
                        pass

                with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
                    await asyncio.create_task(compete())


async def test_reconfigured_policy_never_reaches_execution(linux_host, monkeypatch):
    async with SandboxSession(SandboxConfig()) as session:
        strategy = session.strategy()
        strategy.config = strategy.config.model_copy(update={"execution_backend": "inprocess"})
        with pytest.raises(SandboxUnavailable, match="cannot be reconfigured"):
            await strategy.execute(None, None)
        assert not session._session._active


async def test_close_during_admitted_call_blocks_future_calls(linux_host):
    session = SandboxSession(SandboxConfig())
    await session.__aenter__()
    strategy = session.strategy()
    with strategy.call_scope():
        with pytest.raises(RuntimeError, match="await active"):
            await session.aclose()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        session.strategy()
    await session.aclose()


async def test_failed_executor_cleanup_retains_ownership_for_retry(linux_host):
    attempts = 0

    async def close():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("cleanup failed")

    session = SandboxSession(SandboxConfig())
    await session.__aenter__()
    strategy = session.strategy()
    executor = SimpleNamespace(aclose=close)
    session._session._executors.append(executor)
    call_session = SimpleNamespace(sandbox_executor=executor)
    with pytest.raises(OSError, match="cleanup failed"):
        await strategy._close_sandbox(call_session)
    assert session._session._executors == [executor]
    assert call_session.sandbox_executor is None
    with pytest.raises(SandboxUnavailable, match="not ready"):
        session.strategy()
    await session.aclose()
    assert attempts == 2 and session._session._executors == []


async def test_cancelled_close_drains_owned_executor(linux_host):
    started, release = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        await release.wait()

    session = SandboxSession(SandboxConfig())
    await session.__aenter__()
    session._session._executors.append(SimpleNamespace(aclose=close))
    closing = asyncio.create_task(session.aclose())
    await started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session._session._executors == []
    await session.aclose()


async def test_cross_loop_use_fails(linux_host):
    async with SandboxSession(SandboxConfig()) as session:

        async def use():
            session.strategy()

        with pytest.raises(RuntimeError, match="another event loop"):
            await asyncio.to_thread(asyncio.run, use())


@pytest.mark.parametrize("tools", ["method", ("_private",), ("shell.run",), ("a", "a"), (1,)])
def test_linux_tool_names_are_exact_public_and_unique(linux_host, tools):
    with pytest.raises((ValueError, TypeError), match="public"):
        SandboxSession(SandboxConfig(), tools=tools)


@pytest.mark.parametrize(
    "message",
    [
        {"kind": "attr", "path": ["forbidden"]},
        {"kind": "call", "path": ["allowed", "__globals__"]},
        {"kind": "call", "path": ["allowed"], "root": "other"},
        {"kind": "setattr", "path": ["allowed"]},
        {"kind": "iter", "path": ["allowed"]},
        {"kind": "attr", "path": []},
        {"kind": "call", "path": "allowed"},
    ],
)
async def test_forged_broker_request_is_denied_before_decode_or_getattr(message):
    class Trap:
        def __getattribute__(self, name):
            raise AssertionError("forbidden host attribute resolution")

    executor = object.__new__(linux._ManagedLinuxExecutor)
    executor._tools = ("allowed",)
    executor._agent = Trap()
    # Missing codec deliberately proves a malformed payload is not decoded.
    with pytest.raises(CellSerializationError, match="not granted"):
        executor._decode_tool_call({**message, "payload": b"malicious"})
    response = await executor._dispatch_tool_call(message)
    assert not response["ok"]
    assert "not granted" in response["error"]


async def test_granted_broker_method_works_but_fields_cannot_be_read():
    class Tools:
        value = "secret"

        async def allowed(self, number):
            return number * 2

    executor = object.__new__(linux._ManagedLinuxExecutor)
    executor._agent = Tools()
    executor._tools = ("allowed", "value")
    executor._max_error = 1000
    response = await executor._dispatch_tool_call({"kind": "attr", "path": ["allowed"]})
    assert response["ok"] and response["callable"] and response["is_async"]
    response = await executor._dispatch_tool_call(
        {"kind": "call", "path": ["allowed"], "args": (3,)}
    )
    assert response == {"ok": True, "result": 6, "was_async": True}
    response = await executor._dispatch_tool_call({"kind": "attr", "path": ["value"]})
    assert not response["ok"] and "callable method" in response["error"]


def _response(code: str) -> LLMResponse:
    return LLMResponse(
        raw_response=None,
        content="",
        tool_calls=[
            ToolCall(id="cell", name="execute_python", arguments=json.dumps({"code": code}))
        ],
        finish_reason="tool_calls",
    )


class _NativeAgent(Agent, llm=FakeLLMClient()):
    def double(self, value: int) -> int:
        return value * 2

    def forbidden(self) -> str:
        raise AssertionError("this parent method must not be reached")

    async def run(self) -> dict:
        """Perform the requested checks and return the results."""
        ...


@pytest.mark.sandbox
@pytest.mark.skipif(sys.platform != "linux", reason="native Linux fork")
async def test_native_linux_worker_enforces_files_network_and_fresh_namespace(tmp_path):
    caps = probe_capabilities()
    assert caps.filesystem and caps.network, "native acceptance requires Landlock and seccomp"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    forbidden = tmp_path / "secret"
    forbidden.write_text("outside")
    code = (
        "import os, socket\n"
        f"open({str(workspace / 'allowed')!r}, 'w').write('allowed')\n"
        "denied = False\n"
        "try:\n"
        f"    open({str(forbidden)!r}).read()\n"
        "except PermissionError:\n"
        "    denied = True\n"
        "network_denied = False\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "except PermissionError:\n"
        "    network_denied = True\n"
        "marker = 123\n"
        "return_result({'pid': os.getpid(), 'denied': denied, 'network_denied': network_denied})"
    )
    async with SandboxSession(
        SandboxConfig(workspace=str(workspace)),
        config=CodeActConfig(
            cell_timeout=15,
            restrictions=RestrictionsConfig(blocked_modules=DEFAULT_BLOCKED_MODULES - {"socket"}),
        ),
    ) as session:
        strategy = session.strategy()
        llm = FakeLLMClient(scripted_responses=[_response(code)])
        agent = _NativeAgent(llm=llm)
        result = await agent.run(_strategy=strategy)
        assert result["pid"] != os.getpid()
        assert result["denied"] and result["network_denied"]
        assert (workspace / "allowed").read_text() == "allowed"
        assert not session._session._executors
        agent = _NativeAgent(
            llm=FakeLLMClient(
                scripted_responses=[
                    _response(
                        "fresh = False\n"
                        "try:\n"
                        "    marker\n"
                        "except NameError:\n"
                        "    fresh = True\n"
                        "return_result({'fresh': fresh})"
                    )
                ]
            ),
        )
        assert await agent.run(_strategy=strategy) == {"fresh": True}
        assert not session._session._executors


@pytest.mark.sandbox
@pytest.mark.skipif(sys.platform != "linux", reason="native Linux fork")
async def test_native_linux_tool_allowlist_enforced_inside_worker(tmp_path):
    async with SandboxSession(SandboxConfig(workspace=str(tmp_path)), tools=("double",)) as session:
        agent = _NativeAgent(
            llm=FakeLLMClient(
                scripted_responses=[
                    _response(
                        "number = self.double(6)\n"
                        "denied = False\n"
                        "try:\n"
                        "    self.forbidden()\n"
                        "except Exception:\n"
                        "    denied = True\n"
                        "return_result({'number': number, 'denied': denied})"
                    )
                ]
            )
        )
        assert await agent.run(_strategy=session.strategy()) == {"number": 12, "denied": True}
        assert not session._session._executors
