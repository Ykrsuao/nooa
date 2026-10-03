# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Session ownership and admission for the existing Linux fork executor."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import platform
import struct
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from nooa.config import CodeActConfig
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import CellSerializationError, SandboxUnavailable
from nooa.runtime.sandbox.executor import SandboxedExecutor, check_enforceable
from nooa.strategies.codeact import CodeActStrategy


def _worker_process_filter() -> bytes:
    """Permit pthread clone, but deny process creation/replacement and foreign ABIs."""
    machine = platform.machine()
    if machine == "x86_64":
        arch, clone, denied = 0xC000003E, 56, (57, 58, 59, 322)
    elif machine in ("aarch64", "arm64"):
        arch, clone, denied = 0xC00000B7, 220, (221, 281)
    else:
        raise SandboxUnavailable("managed Linux workers require x86_64 or aarch64")

    def ins(code, jt, jf, value):
        return struct.pack("HBBI", code, jt, jf, value)

    deny = ins(0x06, 0, 0, 0x5000D)  # SECCOMP_RET_ERRNO | EACCES
    allow = ins(0x06, 0, 0, 0x7FFF0000)
    instructions = [ins(0x20, 0, 0, 4), ins(0x15, 1, 0, arch), deny, ins(0x20, 0, 0, 0)]
    if machine == "x86_64":
        instructions.extend((ins(0x45, 0, 1, 0x40000000), deny))  # x32 syscall ABI
    # glibc tries clone3 before clone for pthreads; ENOSYS requests the fallback.
    instructions.extend((ins(0x15, 0, 1, 435), ins(0x06, 0, 0, 0x50026)))
    for number in denied:
        instructions.extend((ins(0x15, 0, 1, number), deny))
    instructions.extend(
        (
            ins(0x15, 0, 3, clone),
            ins(0x20, 0, 0, 16),  # clone flags, low word of seccomp_data.args[0]
            ins(0x45, 1, 0, 0x10000),  # CLONE_THREAD cannot create another process.
            deny,
            allow,
        )
    )
    return b"".join(instructions)


def _live_worker_main(conn, init):
    """Keep the live proxy while preventing worker-created or replacement processes.

    Only the forked worker receives this extra filter. Brokered host methods
    retain their normal ability to start commands. The original fork memory and
    descriptors are unchanged; exact-tool sessions use their separate launcher.
    """
    from nooa.runtime.sandbox import guards, wire
    from nooa.runtime.sandbox.worker import worker_main

    try:
        # This must also work when network/filesystem guards are disabled: their
        # installers cannot be relied on to set no_new_privs for this filter.
        guards._install_no_new_privs()
        guards._seccomp_install(_worker_process_filter())
    except BaseException as exc:
        try:
            wire.send(conn, {"type": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        os._exit(3)
    worker_main(conn, init)


def _scoped_worker_main(conn, init):
    """Drop inherited descriptor capabilities before building a broker-only proxy."""
    import errno

    from nooa.runtime.sandbox import guards, wire
    from nooa.runtime.sandbox._linux_commands import _command_filter
    from nooa.runtime.sandbox.worker import (
        ChildBroker,
        ParentAgentProxy,
        build_namespace,
        serve_cells,
    )

    try:
        null = os.open("/dev/null", os.O_RDWR)
        try:
            for standard in (0, 1, 2):
                os.dup2(null, standard)
        finally:
            if null > 2:
                os.close(null)
        keep = {0, 1, 2, conn.fileno(), *mp.current_process().__dict__["_scoped_fds"]}
        # Snapshot procfs first so closing its temporary enumeration descriptor
        # cannot affect iteration. No guessed fd limit or inherited pipe survives.
        inherited = [int(name) for name in os.listdir("/proc/self/fd")]
        for fd in inherited:
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
        broker = ChildBroker(conn, threading.RLock())
        proxy = ParentAgentProxy(broker, None)
        namespace = build_namespace(
            init["agent"], init.get("framework_builtins") or {}, proxy, init.get("restrictions")
        )
        guards.install_guards(init["spec"])
        guards._seccomp_install(_worker_process_filter())
        guards._seccomp_install(_command_filter(block_network=init["spec"].block_network))
    except BaseException as exc:
        try:
            wire.send(conn, {"type": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        os._exit(3)
    serve_cells(conn, namespace, init)


def _scoped_process(conn, init):
    """Pass exact multiprocessing lifecycle descriptors through the fork boundary.

    multiprocessing's fork launcher does not expose its child completion pipe to
    the target. Retaining arbitrary pipes would also retain host command/IPC
    capabilities, while closing that pipe signals completion prematurely. This
    narrowly scoped launcher follows CPython 3.12's fork lifecycle and records
    both new lifecycle descriptors before its normal bootstrap.
    """
    from multiprocessing import popen_fork, util

    # Linux-only runtime APIs are absent from Windows type stubs.
    class ScopedPopen(getattr(popen_fork, "Popen")):  # noqa: B009
        def _launch(self, process_obj):
            code = 1
            parent_r, child_w = os.pipe()
            try:
                child_r, parent_w = os.pipe()
            except BaseException:
                os.close(parent_r)
                os.close(child_w)
                raise
            try:
                self.pid = getattr(os, "fork")()  # noqa: B009 - Linux-only runtime API
            except BaseException:
                for fd in (parent_r, child_w, child_r, parent_w):
                    os.close(fd)
                raise
            if self.pid == 0:
                try:
                    os.close(parent_r)
                    os.close(parent_w)
                    process_obj._scoped_fds = (child_r, child_w)
                    code = process_obj._bootstrap(parent_sentinel=child_r)
                finally:
                    os._exit(code)
            else:
                os.close(child_w)
                os.close(child_r)
                self.finalizer = util.Finalize(
                    self,
                    getattr(util, "close_fds"),  # noqa: B009 - Linux-only runtime API
                    (parent_r, parent_w),
                )
                self.sentinel = parent_r

    class ScopedProcess(getattr(mp.get_context("fork"), "Process")):  # noqa: B009
        @staticmethod
        def _Popen(process_obj):
            return ScopedPopen(process_obj)

    return ScopedProcess(
        target=_scoped_worker_main, args=(conn, init), daemon=True, name="nooa-scoped-worker"
    )


def _validate_generation_config(config: CodeActConfig) -> None:
    if not isinstance(config, CodeActConfig):
        raise TypeError("CodeActConfig is required for generation options")
    CodeActConfig.model_validate(dict(config), strict=True)
    if config.execution_backend != "inprocess" or config.sandbox != SandboxConfig():
        raise ValueError("supply sandbox permissions as the session policy, not generation config")


async def _drain(awaitable):
    """Finish owned cleanup even when the caller is repeatedly cancelled."""
    task = asyncio.create_task(awaitable)
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


class _LinuxSandboxSession:
    def __init__(
        self,
        policy: SandboxConfig,
        *,
        config: CodeActConfig | None = None,
        tools: Iterable[str] | None = None,
    ):
        self._policy = SandboxConfig.model_validate(dict(policy), strict=True)
        if not self._policy.require:
            raise ValueError("managed Linux sandbox sessions require require=True")
        self._config = CodeActConfig() if config is None else config
        _validate_generation_config(self._config)
        if isinstance(tools, str):
            raise TypeError("tools must be an iterable of public method names")
        self._tools = None if tools is None else tuple(tools)
        if self._tools is not None and (
            any(
                not isinstance(name, str) or not name.isidentifier() or name.startswith("_")
                for name in self._tools
            )
            or len(set(self._tools)) != len(self._tools)
        ):
            raise ValueError("tools must contain unique exact public method names")
        self._state = "new"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._closing = False
        self._executors: list[Any] = []

    @property
    def policy(self) -> SandboxConfig:
        return self._policy

    def _check_loop(self):
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Linux sandbox session belongs to another event loop")

    async def __aenter__(self):
        if self._state != "new":
            raise RuntimeError("Linux sandbox sessions cannot be reopened")
        self._loop = asyncio.get_running_loop()
        self._state = "opening"
        try:
            _validate_generation_config(self._config)
            if sys.platform != "linux" or "fork" not in mp.get_all_start_methods():
                raise SandboxUnavailable("managed Linux sandbox requires native Linux and fork")
            missing = check_enforceable(self.policy)
            if missing:
                raise SandboxUnavailable("Cannot enforce sandbox guardrails: " + "; ".join(missing))
        except BaseException:
            self._state = "closed"
            raise
        self._state = "ready"
        return self

    def _require_ready(self):
        self._check_loop()
        if self._state != "ready":
            raise SandboxUnavailable("Linux sandbox session is not ready")

    def strategy(self, *, config=None, module_globals=None, data_types=()):
        self._require_ready()
        if module_globals is not None or tuple(data_types):
            raise ValueError("explicit module_globals/data_types are Windows sandbox options")
        config = self._config if config is None else config
        _validate_generation_config(config)
        return _ManagedLinuxStrategy(
            self,
            config=config.model_copy(
                update={"execution_backend": "sandbox", "sandbox": self.policy}
            ),
        )

    def _begin_call(self):
        self._require_ready()
        if self._active:
            raise SandboxUnavailable("concurrent or nested managed Linux calls are not supported")
        self._active = True

    async def _close_executor(self, executor):
        await executor.aclose()
        self._executors.remove(executor)

    async def _cleanup(self):
        for executor in tuple(self._executors):
            await self._close_executor(executor)
        self._state = "closed"

    async def aclose(self):
        self._check_loop()
        if self._state == "closed":
            return
        if self._closing:
            raise RuntimeError("Linux sandbox lifecycle operation already in progress")
        self._state = "closing"
        if self._active:
            raise RuntimeError("await active Agent calls before closing the Linux sandbox")
        self._closing = True
        try:
            await _drain(self._cleanup())
        finally:
            self._closing = False


class _ManagedLinuxStrategy(CodeActStrategy):
    def __init__(self, owner: _LinuxSandboxSession, **kwargs):
        self._owner = owner
        self._admitted_task: asyncio.Task[Any] | None = None
        self._execution_started = False
        super().__init__(**kwargs)

    def get_block_overrides(self):
        self._owner._require_ready()
        return super().get_block_overrides()

    async def sandbox_context(self, runtime):
        from nooa.runtime.sandbox.context_block import render_sandbox_block

        self._owner._require_ready()
        text = render_sandbox_block(
            self.config.sandbox,
            cell_timeout=self.config.cell_timeout,
            host_tools=self._owner._tools is None,
        )
        return text + "\n- Processes: the worker cannot launch child processes."

    @contextmanager
    def call_scope(self, *, nested: bool = False) -> Iterator[None]:
        if nested and self._admitted_task is asyncio.current_task() and not self._execution_started:
            yield
            return
        self._owner._begin_call()
        self._admitted_task = asyncio.current_task()
        try:
            yield
        finally:
            self._admitted_task = None
            self._owner._active = False

    async def execute(self, runtime, call):
        with self.call_scope(nested=True):
            self._execution_started = True
            try:
                if (
                    self.config.execution_backend != "sandbox"
                    or self.config.sandbox != self._owner.policy
                ):
                    raise SandboxUnavailable("managed Linux sandbox policy cannot be reconfigured")
                return await super().execute(runtime, call)
            finally:
                self._execution_started = False

    def _create_sandbox_executor(self, runtime, call, builtins):
        executor = _ManagedLinuxExecutor(
            runtime.agent,
            self.config.sandbox,
            tools=self._owner._tools,
            cell_timeout=self.config.cell_timeout,
            framework_builtins={**builtins, "_call": call},
            restrictions=self.config.restrictions,
            max_error=runtime.truncation_config.capture.max_error,
            error_tail=runtime.truncation_config.capture.tail,
        )
        self._owner._executors.append(executor)
        return executor

    async def _close_sandbox(self, session):
        executor = session.sandbox_executor
        if executor is not None:
            try:
                await _drain(self._owner._close_executor(executor))
            except BaseException:
                if executor in self._owner._executors:
                    self._owner._state = "closing"
                raise
            finally:
                session.sandbox_executor = None


class _ManagedLinuxExecutor(SandboxedExecutor):
    """Optionally restrict the trusted host broker to exact public methods.

    This restricts broker dispatch, not inherited fork memory. None exposes live
    Agent tools using declared-data snapshots; an empty tuple denies all tools.
    """

    def __init__(self, *args, tools: tuple[str, ...] | None, **kwargs):
        self._tools = tools
        super().__init__(*args, **kwargs)

    def _attribute_is_value(self, target: Any) -> bool:
        # Match the Windows live proxy: picklable helper objects must still run
        # their methods on the host. Only supported data crosses as a snapshot.
        if not super()._attribute_is_value(target):
            return False
        try:
            self._codec.loads(self._codec.dumps(target))
        except Exception:
            return False
        return True

    def _start_worker(self):
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        init = {
            "agent": self._agent,
            "framework_builtins": self._framework_builtins,
            "restrictions": self._restrictions,
            "spec": self._spec,
            "max_error": self._max_error,
            "error_tail": self._error_tail,
        }
        process = (
            self._ctx.Process(
                target=_live_worker_main,
                args=(child_conn, init),
                daemon=True,
                name="nooa-sandbox-worker",
            )
            if self._tools is None
            else _scoped_process(child_conn, init)
        )
        try:
            process.start()
        except BaseException:
            parent_conn.close()
            child_conn.close()
            raise
        child_conn.close()
        self._conn = parent_conn
        self._proc = process

    def _authorize(self, msg):
        if self._tools is None:
            return
        path = msg.get("path")
        if (
            msg.get("root", "agent") != "agent"
            or msg.get("kind") not in ("attr", "call")
            or not isinstance(path, list)
            or len(path) != 1
            or not isinstance(path[0], str)
            or path[0] not in self._tools
        ):
            raise CellSerializationError(
                "parent tool access is not granted by this sandbox session"
            )

    def _decode_tool_call(self, msg):
        # Reject forbidden operations before deserializing arguments or resolving
        # host attributes, even if the worker forges the broker protocol directly.
        self._authorize(msg)
        return super()._decode_tool_call(msg)

    async def _dispatch_tool_call(self, msg):
        try:
            self._authorize(msg)
        except CellSerializationError as exc:
            return {"ok": False, "error_type": "CellSerializationError", "error": str(exc)}
        return await super()._dispatch_tool_call(msg)

    def _walk_path(self, path, root="agent"):
        if self._tools is not None:
            self._authorize({"path": path, "root": root, "kind": "attr"})
        target = super()._walk_path(path, root)
        if self._tools is not None and (not callable(target) or isinstance(target, type)):
            raise CellSerializationError("a granted parent tool must be a callable method")
        return target
