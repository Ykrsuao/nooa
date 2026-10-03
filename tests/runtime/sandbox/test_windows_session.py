# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Public Windows policy and lifecycle tests, with real LPAC Agent controls."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.events import PythonOutput
from nooa.runtime.restrictions import DEFAULT_BLOCKED_MODULES, RestrictionsConfig
from nooa.runtime.sandbox import _windows_session as managed
from nooa.runtime.sandbox._windows_context import _names, _render_windows_policy
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.runtime.sandbox.windows import (
    DirectoryGrant,
    FileGrant,
    HttpsEndpoint,
    WindowsSandboxPolicy,
    WindowsSandboxSession,
)
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = pytest.mark.timeout(180)
native = pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC")

_APP_SOURCE = Path(__file__).with_name("lpac_test_app.py")
_spec = importlib.util.spec_from_file_location("managed_lpac_app", _APP_SOURCE)
assert _spec is not None and _spec.loader is not None
app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = app
_spec.loader.exec_module(app)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_access", "write"),
        ("workspace_access", None),
        ("recovery", "restart"),
        ("tools", "read"),
        ("tools", ("_private",)),
        ("tools", ("read_file",)),
        ("tools", ("read", "read")),
        ("tool_policies", {"missing": lambda args: True}),
        ("inputs", {"A": b"a", "a": b"b"}),
        ("inputs", {"../escape": b"a"}),
        ("inputs", {"CON": b"a"}),
        ("inputs", {"a": "text"}),
        ("files", {"a": FileGrant("a", writable=1)}),
        ("directories", {"a": FileGrant("a")}),
        ("https", {"a": HttpsEndpoint("http://example.com", "93.184.216.34")}),
        ("https", {"a": HttpsEndpoint("https://example.com", "127.0.0.1")}),
        ("https", {"a": HttpsEndpoint("https://user:pass@example.com", "93.184.216.34")}),
        ("memory_limit_bytes", True),
        ("memory_limit_bytes", -1),
        ("memory_limit_bytes", 2**64),
        ("cpu_time_limit_s", 1.5),
        ("cpu_time_limit_s", 2**63),
        ("max_file_bytes", 4 * 1024 * 1024 + 1),
        ("max_response_bytes", 0),
        ("max_directory_entries", 4097),
    ],
)
def test_policy_refuses_invalid_grants_and_units(field, value):
    with pytest.raises((ValueError, TypeError)):
        WindowsSandboxPolicy(**{field: value})


@pytest.mark.parametrize(
    "name",
    [
        "cell_timeout_s",
        "timeout_grace_s",
        "startup_timeout_s",
        "broker_timeout_s",
        "frame_timeout_s",
        "https_timeout_s",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "3"])
def test_policy_refuses_invalid_deadlines(name, value):
    with pytest.raises(ValueError):
        WindowsSandboxPolicy(**{name: value})


def test_policy_snapshots_inputs_and_collections(tmp_path):
    inputs = {"a": b"data"}
    files = {"file": FileGrant(tmp_path / "a")}
    tools = ["allowed"]
    policy = WindowsSandboxPolicy(inputs=inputs, files=files, tools=tools)
    inputs["a"] = b"changed"
    files.clear()
    tools.append("forbidden")
    assert policy.inputs["a"] == b"data" and "file" in policy.files
    assert policy.tools == ("allowed",)
    with pytest.raises(TypeError):
        policy.inputs["b"] = b"other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.cell_timeout_s = 0
    assert WindowsSandboxPolicy(cell_timeout_s=None, broker_timeout_s=0).cell_timeout_s is None


def test_policy_refuses_async_predicates_and_unknown_fields():
    async def predicate(args):
        return True

    with pytest.raises(TypeError, match="synchronous"):
        WindowsSandboxPolicy(tools=("allowed",), tool_policies={"allowed": predicate})
    with pytest.raises(TypeError):
        WindowsSandboxPolicy(unknown_permission=True)


def test_context_default_policy_does_not_claim_linux_or_live_agent_semantics():
    text = _render_windows_policy(WindowsSandboxPolicy())
    for expected in (
        "Windows LPAC",
        "read-only",
        "no direct host path grants",
        "registryRead",
        "direct internet sockets are denied",
        "no endpoints granted",
        "No other Agent methods or live self fields",
        "Cell deadline: 10s",
        "startup deadline: 60s",
        "IPC frame deadline: 5s",
        "parent-tool deadline: 30s",
        "no configured Job Object memory limit",
        "no configured Job Object CPU limit",
        "replaced with empty globals",
        "no cumulative session or disk quotas",
        "not asynchronous descriptor I/O",
    ):
        assert expected in text
    for absent in ("Landlock", "RLIMIT", "picklable", "self.<attr>", "self.write_file"):
        assert absent not in text


@pytest.mark.parametrize("writable", [False, True])
def test_context_named_broker_permissions_and_sensitive_values(tmp_path, writable):
    secret = "not-for-the-model"
    policy = WindowsSandboxPolicy(
        workspace_access="read_write",
        inputs={"seed.txt": secret.encode()},
        files={"output": FileGrant(tmp_path / secret, writable=writable)},
        directories={"source": DirectoryGrant(tmp_path / secret, writable=writable)},
        https={"site": HttpsEndpoint(f"https://example.com/?token={secret}", "93.184.216.34")},
        tools=("scale",),
        tool_policies={"scale": lambda args: secret not in str(args)},
        cell_timeout_s=None,
        startup_timeout_s=45,
        broker_timeout_s=0,
        frame_timeout_s=0.125,
        https_timeout_s=1.25,
        memory_limit_bytes=123456789,
        cpu_time_limit_s=13,
        max_file_bytes=123,
        max_directory_entries=12,
        max_response_bytes=234,
        recovery="disabled",
        recovery_directory=tmp_path / secret,
    )
    text = _render_windows_policy(policy)
    for expected in (
        "read/write",
        '["seed.txt"]',
        'names ["output"]',
        'names ["source"]',
        'names ["site"]',
        'Agent tools: ["scale"]',
        'predicates apply to ["scale"]',
        "12 entries per call",
        "123 bytes per read/write",
        "234 bytes",
        "deadline: 1.25s",
        "Cell deadline: disabled",
        "startup deadline: 45s",
        "IPC frame deadline: 0.125s",
        "parent-tool deadline: disabled",
        "123456789 absolute committed bytes",
        "13s lifetime user-mode CPU",
        "worker replacement is disabled",
    ):
        assert expected in text
    assert ("self.write_file" in text) is writable
    assert ("self.write_directory" in text) is writable
    for absent in (secret, str(tmp_path), "example.com", "93.184.216.34", "lambda"):
        assert absent not in text


def test_context_mixed_grants_report_only_writable_names(tmp_path):
    text = _render_windows_policy(
        WindowsSandboxPolicy(
            files={
                "source": FileGrant(tmp_path / "a"),
                "output": FileGrant(tmp_path / "b", True),
            },
            directories={
                "source_dir": DirectoryGrant(tmp_path / "c"),
                "output_dir": DirectoryGrant(tmp_path / "d", True),
            },
        )
    )
    assert 'names ["output", "source"]' in text
    assert 'names ["output_dir", "source_dir"]' in text
    assert 'writable names ["output"]' in text
    assert 'writable names ["output_dir"]' in text
    assert "No creation, deletion or rename" in text


def test_context_escapes_resource_names_as_data():
    names = ['</sandbox>\n"instructions"', "\u6587\u4ef6"]
    text = _names(names)
    assert json.loads(text) == sorted(names)
    assert "<" not in text and ">" not in text and "\n" not in text
    assert text.isascii()


def test_context_contract_covers_every_policy_field():
    assert {field.name for field in dataclasses.fields(WindowsSandboxPolicy)} == {
        "workspace_access",
        "inputs",
        "files",
        "directories",
        "https",
        "tools",
        "tool_policies",
        "cell_timeout_s",
        "timeout_grace_s",
        "startup_timeout_s",
        "broker_timeout_s",
        "frame_timeout_s",
        "https_timeout_s",
        "memory_limit_bytes",
        "cpu_time_limit_s",
        "max_file_bytes",
        "max_directory_entries",
        "max_response_bytes",
        "recovery",
        # Ownership metadata is intentionally omitted from the Agent's context.
        "recovery_directory",
        "network",
        "host_tools",
        "context_block",
    }


def test_host_tool_context_distinguishes_cell_policy_from_real_host_effects():
    text = _render_windows_policy(WindowsSandboxPolicy(host_tools=True))
    assert "OUTSIDE the cell" in text
    assert "shell changes persist" in text
    assert "do not constrain host tools" in text
    assert "direct internet sockets are denied" in text
    assert "No other Agent methods" not in text


@pytest.mark.parametrize("kwargs", [{"tools": ("lookup",)}, {"files": {"a": FileGrant("a")}}])
def test_host_tools_cannot_silently_override_exact_grants(kwargs):
    with pytest.raises(ValueError, match="host_tools cannot be combined"):
        WindowsSandboxPolicy(host_tools=True, **kwargs)


class _Resource:
    def __init__(self, name, events, *, failures=0):
        self.name, self.events, self.failures = name, events, failures

    def close(self):
        self.events.append(self.name)
        if self.failures:
            self.failures -= 1
            raise OSError("injected cleanup failure")

    async def aclose(self):
        self.close()


@pytest.fixture
def fake_provision(monkeypatch):
    # Patch only the owner's provisioning boundary, never an Agent/metaclass method.
    monkeypatch.setattr(managed.sys, "platform", "win32")
    events = []

    def provision(owner):
        owner._runtime = _Resource("runtime", events)
        owner._files = _Resource("files", events)
        owner._directories = _Resource("directories", events)

    monkeypatch.setattr(WindowsSandboxSession, "_provision", provision)
    return events


@pytest.mark.parametrize(
    "config",
    [
        CodeActConfig(execution_backend="sandbox"),
        CodeActConfig(cell_timeout=0.5),
        CodeActConfig(sandbox=SandboxConfig(network=True)),
        CodeActConfig().model_copy(update={"windows_sandbox": {"memory_limit_bytes": 1024}}),
        CodeActConfig().model_copy(
            update={"sandbox": SandboxConfig().model_copy(update={"memory_limit_bytes": 1024})}
        ),
    ],
    ids=["backend", "cell-deadline", "public-policy", "unknown-config", "unknown-policy"],
)
async def test_generation_config_is_refused_before_provision(monkeypatch, config):
    from unittest.mock import Mock

    provision = Mock(side_effect=AssertionError("invalid config provisioned a runtime"))
    monkeypatch.setattr(WindowsSandboxSession, "_provision", provision)
    with pytest.raises(ValueError):
        async with WindowsSandboxSession(WindowsSandboxPolicy(), config=config):
            pytest.fail("invalid config was admitted")
    provision.assert_not_called()


async def test_session_binds_generation_options_before_provision(monkeypatch):
    monkeypatch.setattr(managed.sys, "platform", "win32")
    monkeypatch.setattr(WindowsSandboxSession, "_provision", lambda owner: None)
    config = CodeActConfig(max_retries=6, max_iterations=4, prefill=None)
    policy = WindowsSandboxPolicy(cell_timeout_s=0.75, broker_timeout_s=2)

    async with WindowsSandboxSession(policy, config=config) as owner:
        backend = owner.strategy()
        assert backend.config.max_retries == 6
        assert backend.config.max_iterations == 4
        assert backend.config.prefill is None
        assert backend.config.cell_timeout == 0.75
        assert backend.config.execution_backend == "sandbox"
        assert backend.config.sandbox.context_block is False
        assert config.execution_backend == "inprocess" and config.cell_timeout is None
        assert config.model_fields_set == {"max_retries", "max_iterations", "prefill"}

        override = owner.strategy(config=CodeActConfig(max_retries=2))
        assert override.config.max_retries == 2
        assert override.config.cell_timeout == 0.75
        with pytest.raises(ValueError):
            owner.strategy(config=CodeActConfig().model_copy(update={"memory_limit_bytes": 1024}))


async def test_lifecycle_cleanup_order_and_one_shot(fake_provision):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    async with owner:
        owner._executors.append(_Resource("executor", fake_provision))
    assert fake_provision == ["executor", "directories", "files", "runtime"]
    assert owner._state == "closed" and owner._runtime is None
    await owner.aclose()
    with pytest.raises(RuntimeError, match="reopened"):
        await owner.__aenter__()


@pytest.mark.parametrize("resource", ["executor", "directories", "files", "runtime"])
async def test_failed_cleanup_retains_ownership_and_can_retry(fake_provision, resource):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    await owner.__aenter__()
    executor = _Resource("executor", fake_provision)
    owner._executors.append(executor)
    target = executor if resource == "executor" else getattr(owner, "_" + resource)
    target.failures = 1
    with pytest.raises(OSError, match="injected"):
        await owner.aclose()
    assert owner._runtime is not None
    if resource != "runtime":
        assert "runtime" not in fake_provision
    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner._begin_call()
    await owner.aclose()
    assert owner._state == "closed"
    assert fake_provision.count(resource) == 2


async def test_active_calls_refuse_concurrency_and_close(fake_provision):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    async with owner:
        owner._begin_call()
        with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
            owner._begin_call()
        with pytest.raises(RuntimeError, match="await active"):
            await owner.aclose()
        assert not fake_provision
        owner._active = False
        with pytest.raises(SandboxUnavailable, match="not ready"):
            owner._begin_call()


class _PausedLLM(FakeLLMClient):
    def __init__(self):
        super().__init__(
            [
                LLMResponse(
                    raw_response=None,
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id=f"result-{value}", name="return_result", arguments='{"result": 7}'
                        )
                    ],
                )
                for value in range(3)
            ],
            strict_exhaustion=True,
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def acall(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().acall(*args, **kwargs)


@pytest.fixture
def admission_backend(monkeypatch, fake_provision):
    from nooa.runtime.sandbox import _lpac_codeact

    # Keep Agent, CodeAct, FakeLLM and managed teardown real; replace only native
    # allocation. These calls use return_result, so no worker needs to run code.
    monkeypatch.setattr(
        _lpac_codeact,
        "_LpacExecutor",
        lambda *args, **kwargs: _Resource("executor", fake_provision),
    )

    async def create(owner):
        await owner.__aenter__()
        owner._files = owner._directories = None
        return owner.strategy(config=CodeActConfig(prefill=None))

    return create


@pytest.mark.parametrize("same_agent", [True, False], ids=["same-agent", "shared-session"])
@pytest.mark.parametrize("cancel_first", [False, True], ids=["completed", "cancelled"])
async def test_agent_calls_reject_overlap_and_allow_reuse(
    admission_backend, same_agent, cancel_first
):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    backend = await admission_backend(owner)

    class Demo(Agent):
        @strategy(backend)
        async def compute(self) -> int:
            """Return seven."""
            ...

    llm = _PausedLLM()
    first_agent = Demo(llm=llm)
    second_agent = first_agent if same_agent else Demo(llm=llm)
    tasks = []
    try:
        tasks.append(asyncio.create_task(first_agent.compute()))
        await asyncio.wait_for(llm.started.wait(), 5)
        tasks.append(asyncio.create_task(second_agent.compute()))
        done, _ = await asyncio.wait([tasks[1]], timeout=0.2)
        assert tasks[1] in done, "overlapping call queued behind the Agent generation lock"
        with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
            await tasks[1]
        assert owner._active and not tasks[0].done()
        if cancel_first:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
        else:
            llm.release.set()
            assert await tasks[0] == 7
        assert not owner._active
        llm.release.set()
        assert await first_agent.compute() == 7
    finally:
        llm.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await first_agent.aclose()
        if second_agent is not first_agent:
            await second_agent.aclose()
        await llm.aclose()
        await owner.aclose()


@pytest.mark.parametrize("entry", ["agent", "nested-strategy", "direct-strategy"])
async def test_managed_nested_calls_cannot_reuse_admission(admission_backend, monkeypatch, entry):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    backend = await admission_backend(owner)

    class Demo(Agent):
        @strategy(backend)
        async def compute(self) -> int:
            """Return seven."""
            ...

    llm = _PausedLLM()
    llm.release.set()
    agent = Demo(llm=llm)
    original_acall = llm.acall
    reentered = False

    async def reenter(*args, **kwargs):
        nonlocal reentered
        if not reentered:
            reentered = True
            with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
                if entry == "agent":
                    await agent.compute()
                elif entry == "nested-strategy":
                    await agent.runtime.execute_nested(backend, agent.runtime.current_call)
                else:
                    await backend.execute(agent.runtime, agent.runtime.current_call)
            assert owner._active, "rejected nested call released the outer admission"
        return await original_acall(*args, **kwargs)

    monkeypatch.setattr(llm, "acall", reenter)
    try:
        assert await agent.compute() == 7
        assert reentered and not owner._active
        assert await agent.compute() == 7
    finally:
        await agent.aclose()
        await llm.aclose()
        await owner.aclose()


async def test_call_override_releases_admission_when_cancelled_waiting_for_lock(admission_backend):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    backend = await admission_backend(owner)

    class Demo(Agent):
        async def compute(self) -> int:
            """Return seven."""
            ...

    llm = _PausedLLM()
    llm.release.set()
    agent = Demo(llm=llm)
    task = None
    try:
        agent.runtime._ensure_generation_lock_on_current_loop()
        async with agent.runtime._generation_lock:
            task = asyncio.create_task(agent.compute(_strategy=backend))
            async with asyncio.timeout(5):
                while not owner._active:
                    await asyncio.sleep(0)
            assert not llm.started.is_set()
            with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
                await asyncio.wait_for(agent.compute(_strategy=backend), 1)
            assert owner._active
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not owner._active
        assert await agent.compute(_strategy=backend) == 7
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await agent.aclose()
        await llm.aclose()
        await owner.aclose()


async def test_generation_setup_error_releases_managed_admission(admission_backend):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    backend = await admission_backend(owner)

    class Demo(Agent):
        @strategy(backend)
        async def compute(self) -> int:
            """Return seven."""
            ...

    def fail_resolution(_):
        raise ValueError("injected LLM resolution failure")

    llm = _PausedLLM()
    llm.release.set()
    agent = Demo(llm=llm)
    try:
        with pytest.raises(RuntimeError, match="injected LLM resolution failure"):
            await agent.compute(llm=fail_resolution)
        assert not owner._active and not llm.started.is_set()
        assert await agent.compute() == 7
    finally:
        await agent.aclose()
        await llm.aclose()
        await owner.aclose()


async def test_ordinary_agent_generation_still_queues():
    from nooa.strategies import CodeActStrategy

    class Demo(Agent):
        @strategy(CodeActStrategy(CodeActConfig(prefill=None)))
        async def compute(self) -> int:
            """Return seven."""
            ...

    llm = _PausedLLM()
    agent = Demo(llm=llm)
    tasks = []
    try:
        tasks.append(asyncio.create_task(agent.compute()))
        await asyncio.wait_for(llm.started.wait(), 5)
        tasks.append(asyncio.create_task(agent.compute()))
        done, _ = await asyncio.wait([tasks[1]], timeout=0.1)
        assert not done
        llm.release.set()
        assert await asyncio.gather(*tasks) == [7, 7]
        assert llm.call_count == 2
    finally:
        llm.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await agent.aclose()
        await llm.aclose()


async def test_reflexion_holds_admission_between_sequential_base_calls(admission_backend):
    from nooa.config.strategy_config import ReflexionConfig
    from nooa.strategies.reflexion import ReflectionOutput, ReflexionStrategy

    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    backend = await admission_backend(owner)
    composite = ReflexionStrategy(base=backend, config=ReflexionConfig(max_iterations=2))

    class Demo(Agent):
        @strategy(composite)
        async def compute(self) -> int:
            """Return seven."""
            ...

    started, release = asyncio.Event(), asyncio.Event()

    class ReflectingLLM(FakeLLMClient):
        async def acall(self, *args, **kwargs):
            if kwargs.get("output_model") is ReflectionOutput and not started.is_set():
                started.set()
                await release.wait()
            return await super().acall(*args, **kwargs)

    responses = []
    for index, satisfactory in enumerate([False, True, True]):
        responses.extend(
            [
                LLMResponse(
                    raw_response=None,
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(id=f"r-{index}", name="return_result", arguments='{"result": 7}')
                    ],
                ),
                LLMResponse(
                    raw_response=None,
                    content="",
                    parsed=ReflectionOutput(is_satisfactory=satisfactory),
                ),
            ]
        )
    llm = ReflectingLLM(responses, strict_exhaustion=True)
    agent = Demo(llm=llm)
    task = asyncio.create_task(agent.compute())
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert owner._active and not backend._execution_started
        with pytest.raises(SandboxUnavailable, match="concurrent or nested"):
            await asyncio.wait_for(agent.compute(), 1)
        assert owner._active
        release.set()
        assert await task == 7
        assert not owner._active
        assert await agent.compute() == 7
        assert llm.remaining_responses == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.aclose()
        await llm.aclose()
        await owner.aclose()


async def test_cross_loop_use_is_refused(fake_provision):
    async with WindowsSandboxSession(WindowsSandboxPolicy()) as owner:

        async def other_loop():
            with pytest.raises(RuntimeError, match="another event loop"):
                await owner.aclose()

        await asyncio.to_thread(asyncio.run, other_loop())
        assert not fake_provision


async def test_cancelled_provision_waits_for_thread_then_cleans(monkeypatch, fake_provision):
    started, release = threading.Event(), threading.Event()
    owner = WindowsSandboxSession(WindowsSandboxPolicy())

    def provision(owner):
        started.set()
        assert release.wait(10)
        owner._runtime = _Resource("runtime", fake_provision)

    monkeypatch.setattr(WindowsSandboxSession, "_provision", provision)
    task = asyncio.create_task(owner.__aenter__())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(RuntimeError, match="in progress"):
            await owner.aclose()
        assert not task.done() and not fake_provision
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_provision == ["runtime"] and owner._state == "closed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_close_drains_despite_repeated_cancellation(fake_provision):
    started, release = asyncio.Event(), asyncio.Event()

    class SlowExecutor:
        async def aclose(self):
            started.set()
            await release.wait()
            fake_provision.append("executor")

    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    await owner.__aenter__()
    owner._executors.append(SlowExecutor())
    task = asyncio.create_task(owner.aclose())
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not fake_provision
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_provision == ["executor", "directories", "files", "runtime"]
        assert owner._state == "closed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_staging_failure_closes_retained_runtime(monkeypatch, fake_provision):
    def provision(owner):
        owner._runtime = _Resource("runtime", fake_provision)
        raise ValueError("staging refused")

    monkeypatch.setattr(WindowsSandboxSession, "_provision", provision)
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    with pytest.raises(ValueError, match="staging refused"):
        await owner.__aenter__()
    assert fake_provision == ["runtime"] and owner._state == "closed"


async def test_constructor_rollback_failure_retains_retryable_runtime(monkeypatch):
    monkeypatch.setattr(managed.sys, "platform", "win32")
    events = []
    resource = _Resource("runtime", events, failures=1)

    def allocate(**kwargs):
        kwargs["_retain"](resource)
        raise OSError("constructor rollback failed")

    monkeypatch.setattr(managed, "_AppContainerPython", allocate)
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    with pytest.raises(OSError, match="cleanup failure"):
        await owner.__aenter__()
    assert owner._runtime is resource
    await owner.aclose()
    assert events == ["runtime", "runtime"]


async def test_non_windows_refuses_before_provision(monkeypatch):
    monkeypatch.setattr(managed.sys, "platform", "linux")
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    with pytest.raises(SandboxUnavailable, match="native Windows"):
        await owner.__aenter__()
    assert owner._runtime is None and owner._state == "new"


async def test_managed_teardown_failure_blocks_new_calls_and_retries(fake_provision):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    await owner.__aenter__()
    # This test exercises strategy cleanup itself, without allocating a worker.
    owner._files = owner._directories = None
    backend = owner.strategy()
    executor = _Resource("executor", fake_provision, failures=1)
    owner._executors.append(executor)
    session = SimpleNamespace(sandbox_executor=executor)
    with pytest.raises(OSError, match="cleanup failure"):
        await backend._close_sandbox(session)
    assert owner._executors == [executor] and session.sandbox_executor is None
    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner._begin_call()
    await owner.aclose()
    assert fake_provision == ["executor", "executor", "runtime"]


async def test_managed_context_requires_ready_session_and_uses_static_windows_block(fake_provision):
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    async with owner:
        owner._files = owner._directories = None
        backend = owner.strategy()
        text = backend.get_block_overrides()["sandbox"]
        assert text == await backend.sandbox_context(None)
        assert "Windows LPAC" in text
        assert "sandbox" in backend.get_static_block_keys()
        assert backend.config.sandbox.context_block is False
    with pytest.raises(SandboxUnavailable, match="not ready"):
        backend.get_block_overrides()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        await backend.sandbox_context(None)


async def test_provisioning_maps_grants_limits_and_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(managed.sys, "platform", "win32")
    observed, events = {}, []

    class Broker(_Resource):
        async def read(self, *args):
            return b"data"

        async def write(self, *args):
            return 1

        async def list(self, *args):
            return []

        async def fetch(self, name):
            return {"body": b"ok"}

    def factory(name):
        def create(grants, **kwargs):
            observed[name] = (grants, kwargs)
            return Broker(name, events)

        return create

    def allocate(**kwargs):
        observed["runtime"] = kwargs
        runtime = _Resource("runtime", events)
        kwargs["_retain"](runtime)
        return runtime

    def stage(runtime, **kwargs):
        observed["stage"] = kwargs

    monkeypatch.setattr(managed, "_AppContainerPython", allocate)
    monkeypatch.setattr(managed, "_FileBroker", factory("files"))
    monkeypatch.setattr(managed, "_DirectoryBroker", factory("directories"))
    monkeypatch.setattr(managed, "_HttpsBroker", factory("https"))
    monkeypatch.setattr(managed, "stage_framework", stage)
    policy = WindowsSandboxPolicy(
        workspace_access="read_write",
        inputs={"a": b"a"},
        files={"input": FileGrant(tmp_path / "input")},
        directories={"output": DirectoryGrant(tmp_path / "output", writable=True)},
        https={"site": HttpsEndpoint("https://example.com/data", "93.184.216.34")},
        max_file_bytes=123,
        max_directory_entries=12,
        max_response_bytes=234,
        https_timeout_s=7,
        cell_timeout_s=None,
        timeout_grace_s=0.75,
        broker_timeout_s=0,
        frame_timeout_s=2,
        memory_limit_bytes=1024**3,
        cpu_time_limit_s=15,
        recovery="disabled",
        recovery_directory=tmp_path / "recovery",
    )
    modules = {"app": tmp_path / "app.py"}
    async with WindowsSandboxSession(
        policy, application_modules=modules, application_requirements=("PyYAML>=6",)
    ) as owner:
        backend = owner.strategy()
        assert set(backend._parent_tools) == {
            "read_file",
            "list_directory",
            "read_directory",
            "write_directory",
            "fetch_https",
        }
        assert await backend._parent_tools["fetch_https"]("site") == {"body": b"ok"}
        assert backend._broker_timeout_s == 0 and backend._frame_timeout_s == 2
        assert backend.config.cell_timeout is None
        assert backend._timeout_grace_s == 0.75
        assert backend._memory_limit_bytes == 1024**3 and backend._cpu_time_limit_s == 15
        assert backend._recovery == "disabled"
    assert observed["runtime"]["workspace_access"] == "read_write"
    assert observed["runtime"]["inputs"] == {"a": b"a"}
    assert observed["runtime"]["recovery_directory"] == tmp_path / "recovery"
    assert observed["files"][1] == {"max_file_bytes": 123}
    assert observed["directories"][1] == {"max_file_bytes": 123, "max_entries": 12}
    assert observed["https"][1] == {"max_response_bytes": 234, "timeout_s": 7}
    assert observed["stage"] == {
        "application_modules": modules,
        "application_requirements": ("PyYAML>=6",),
    }
    assert events == ["directories", "files", "runtime"]


def _responses(*cells):
    return FakeLLMClient(
        scripted_responses=[
            LLMResponse(
                raw_response=None,
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id=f"c{i}", name="execute_python", arguments=json.dumps({"code": code})
                    )
                ],
            )
            for i, code in enumerate(cells)
        ]
    )


async def _compute(agent, *args):
    try:
        return await agent.compute(*args)
    except Exception as exc:
        exc.add_note(
            repr(
                [
                    (e.stdout, e.error)
                    for e in agent.event_manager.values()
                    if isinstance(e, PythonOutput)
                ]
            )
        )
        raise


@native
async def test_managed_agent_brokers_denials_fresh_workers_and_cleanup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_bytes(b"source")
    output = tmp_path / "output.txt"
    output.write_bytes(b"before")
    policy = WindowsSandboxPolicy(
        inputs={"seed.txt": b"snapshot"},
        files={"output": FileGrant(output, writable=True)},
        directories={"source": DirectoryGrant(source)},
        tools=("scale",),
        tool_policies={"scale": lambda args: args["number"] < 10},
        memory_limit_bytes=1024**3,
        cpu_time_limit_s=30,
        broker_timeout_s=2,
        frame_timeout_s=3,
    )
    owner = WindowsSandboxSession(
        policy,
        config=CodeActConfig(
            max_retries=6,
            # Reach the OS denial rather than the optional Python import guard.
            restrictions=RestrictionsConfig(blocked_modules=DEFAULT_BLOCKED_MODULES - {"socket"}),
        ),
    )
    async with owner:
        root = owner._runtime.root
        backend = owner.strategy()

        class Demo(Agent, llm=FakeLLMClient()):
            def scale(self, number: int) -> int:
                return number * 2

            def forbidden(self):
                raise AssertionError("ungranted callback")

            @strategy(backend)
            async def compute(self) -> int:
                """Compute using the named resources."""
                ...

        agent = Demo(
            llm=_responses(
                f"open({str(output)!r}, 'rb')",
                "open('denied.txt', 'w')",
                "self.forbidden()",
                "self.scale(10)",
                "import socket\nsocket.socket()",
                "import os\n"
                "assert 'forbidden' not in doc(self)\n"
                "assert 'write_directory' not in doc(self)\n"
                "assert await self.read_directory('source', 'input.txt') == b'source'\n"
                "assert (await self.list_directory('source'))[0]['name'] == 'input.txt'\n"
                "assert await self.write_file('output', b'after') == 5\n"
                "assert await self.read_file('output') == b'after'\n"
                "with open('../inputs/seed.txt', 'rb') as f:\n    assert f.read() == b'snapshot'\n"
                "seed = self.scale(3)\nreturn_result(os.getpid())",
            )
        )
        pid = await _compute(agent)
        assert pid != os.getpid()
        messages = "\n".join(str(message["content"]) for message in agent.llm.last_messages)
        assert "<sandbox>" in messages and "Windows LPAC" in messages
        assert 'names ["output"]' in messages and 'names ["source"]' in messages
        assert "self.<attr>" not in messages and "Landlock" not in messages
        errors = [
            e for e in agent.event_manager.values() if isinstance(e, PythonOutput) and e.error
        ]
        assert len(errors) == 5
        assert any("10013" in str(event.error) for event in errors)
        assert not owner._executors and not owner._active
        assert (
            await _compute(
                Demo(
                    llm=_responses(
                        "try:\n    seed\nexcept NameError:\n    return_result(8)\nreturn_result(0)"
                    )
                )
            )
            == 8
        )
    assert not root.exists() and output.read_bytes() == b"after"
    with pytest.raises(SandboxUnavailable, match="not ready"):
        await Demo(llm=_responses("return_result(0)")).compute()
    output.unlink()  # No pinned broker handle remains.


@native
async def test_managed_agent_cancellation_and_broker_deadline(tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()
    owner = WindowsSandboxSession(WindowsSandboxPolicy(tools=("slow",), broker_timeout_s=0.15))
    async with owner:
        root = owner._runtime.root
        backend = owner.strategy()

        class Demo(Agent, llm=FakeLLMClient()):
            async def slow(self):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()

            @strategy(backend)
            async def compute(self) -> int:
                """Wait for a parent callback."""
                ...

        agent = Demo(llm=_responses("await self.slow()", "return_result(7)"))
        assert await agent.compute() == 7
        assert stopped.is_set() and not owner._executors
        started.clear()
        stopped.clear()
        task = asyncio.create_task(Demo(llm=_responses("await self.slow()")).compute())
        try:
            await asyncio.wait_for(started.wait(), 90)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stopped.is_set() and not owner._executors and not owner._active
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert await Demo(llm=_responses("return_result(9)")).compute() == 9
        assert not owner._executors and not owner._active
    assert not root.exists()


@native
async def test_managed_strategy_refuses_public_policy_and_deadline_mutation():
    async with WindowsSandboxSession(WindowsSandboxPolicy()) as owner:
        with pytest.raises(ValueError, match="cell_timeout_s"):
            owner.strategy(config=CodeActConfig(cell_timeout=0.5))
        with pytest.raises(ValueError, match="public backend"):
            owner.strategy(config=CodeActConfig(execution_backend="sandbox"))
        backend = owner.strategy()
        backend.config = backend.config.model_copy(update={"cell_timeout": None})

        class Demo(Agent, llm=FakeLLMClient()):
            @strategy(backend)
            async def compute(self) -> int:
                """Refuse changes."""
                ...

        with pytest.raises(SandboxUnavailable, match="reconfigured"):
            await Demo().compute()
        assert not owner._executors and not owner._active


@native
async def test_managed_staged_application_recovery_and_writable_workspace(tmp_path):
    policy = WindowsSandboxPolicy(
        workspace_access="read_write",
        cell_timeout_s=1,
        recovery_directory=tmp_path / "ledger",
    )
    owner = WindowsSandboxSession(
        policy,
        application_modules={"managed_lpac_app": _APP_SOURCE},
        application_requirements=("PyYAML>=6",),
    )
    async with owner:
        root = owner._runtime.root
        entry = root.parent
        backend = owner.strategy(module_globals={"app": app}, data_types=(app.Request, app.Answer))

        class Demo(Agent, llm=FakeLLMClient()):
            @strategy(backend)
            async def compute(self, request: app.Request) -> app.Answer:
                """Compute from a staged application."""
                ...

        agent = Demo(
            llm=_responses(
                "import yaml\n"
                "assert yaml.safe_load('value: 2')['value'] == 2\n"
                "with open('retained.txt', 'w') as f:\n    f.write('workspace')\n"
                "seed = app.increment(request.item.amount)",
                "while True: pass",
                "import os\n"
                "try:\n    seed\nexcept NameError:\n    pass\nelse:\n    raise AssertionError('retained globals')\n"
                "with open('retained.txt') as f:\n    assert f.read() == 'workspace'\n"
                "return_result(app.Answer(value=app.increment(request.item.amount), worker=os.getpid()))",
            )
        )
        answer = await _compute(agent, app.Request(item=app.Item(4)))
        assert isinstance(answer, app.Answer) and answer.value == 5
        assert answer.worker != os.getpid() and not owner._executors
        assert entry.is_dir()
    assert not entry.exists()
