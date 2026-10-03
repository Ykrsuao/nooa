# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal Windows spawn/IPC acceptance backend, NOT a security sandbox.

Not exported or selected by CodeAct/SandboxConfig. The explicit unsafe opt-in is
for trusted tests only; filesystem, network, environment and parent access are
unrestricted. Job budgets use absolute committed memory and lifetime user CPU,
not the public Linux sandbox's headroom/per-process rlimit semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import sys
import threading
import time
from functools import partial
from types import SimpleNamespace
from typing import Any, Literal

from nooa.events import ExecutionResult
from nooa.runtime.sandbox._spawn_bootstrap import make_bootstrap, spawn_worker_main
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import (
    CellSerializationError,
    CellTimeoutError,
    SandboxUnavailable,
    WorkerDiedError,
)
from nooa.runtime.sandbox.executor import SandboxedExecutor
from nooa.runtime.sandbox.serialization import dto_from_wire, dto_to_result, is_picklable


class _SpawnExecutor(SandboxedExecutor):
    """Trusted-code process runner used to develop native Windows support."""

    def __init__(
        self,
        agent: Any,
        *,
        unsafe_no_isolation: bool = False,
        module_globals: dict[str, Any] | None = None,
        framework_builtins: dict[str, Any] | None = None,
        restrictions: Any = None,
        cell_timeout: float | None = 10,
        startup_timeout_s: float = 30,
        timeout_grace_s: float = 0,
        broker_timeout_s: float = 300,
        recovery: Literal["restart_empty", "disabled"] = "restart_empty",
        memory_limit_bytes: int = 0,
        cpu_time_limit_s: int = 0,
        active_process_limit: int = 0,
        max_error: int | None = None,
        error_tail: int | None = None,
    ):
        import math

        if unsafe_no_isolation is not True:
            raise SandboxUnavailable("internal spawn worker requires unsafe_no_isolation=True")
        if sys.platform != "win32":
            raise SandboxUnavailable("internal spawn worker currently requires native Windows")
        if not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be finite and positive")
        if cell_timeout is not None and (not math.isfinite(cell_timeout) or cell_timeout <= 0):
            raise ValueError("cell_timeout must be None or finite and positive")
        if not math.isfinite(broker_timeout_s) or broker_timeout_s < 0:
            raise ValueError("broker_timeout_s must be finite and nonnegative")
        if type(timeout_grace_s) not in (int, float) or not math.isfinite(timeout_grace_s):
            raise ValueError("timeout_grace_s must be finite and nonnegative")
        if timeout_grace_s < 0:
            raise ValueError("timeout_grace_s must be finite and nonnegative")
        super().__init__(
            agent,
            SandboxConfig(
                filesystem=False,
                network=True,
                require=False,
                context_block=False,
                timeout_grace_s=timeout_grace_s,
                broker_timeout_s=broker_timeout_s,
                recovery=recovery,
            ),
            cell_timeout=cell_timeout,
            framework_builtins=framework_builtins,
            restrictions=restrictions,
            max_error=max_error,
            error_tail=error_tail,
        )
        from nooa.agentdoc import doc
        from nooa.agentdoc.introspect import methods, variables
        from nooa.agentdoc.visibility import filter_mro_module_globals

        self._bootstrap = make_bootstrap(
            filter_mro_module_globals(type(agent)) if module_globals is None else module_globals,
            self._framework_builtins,
            self._codec,
        )
        self._framework_root = SimpleNamespace(**self._framework_builtins)
        self._introspection_root = SimpleNamespace(
            **{fn.__name__: partial(self._introspect, fn) for fn in (doc, methods, variables)}
        )
        self._startup_timeout_s = startup_timeout_s
        self._job_limits = {
            "memory_limit_bytes": memory_limit_bytes,
            "cpu_time_limit_s": cpu_time_limit_s,
            "active_process_limit": active_process_limit,
        }
        self._job: Any = None
        self._io_task: asyncio.Task | None = None
        self._running_task: asyncio.Task | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._stop = threading.Event()

    def _prepare_backend(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._degraded = ["filesystem", "network", "parent-process isolation"]

    def _broker_roots(self) -> dict[str, Any]:
        return {
            "agent": self._agent,
            "framework": self._framework_root,
            "introspection": self._introspection_root,
        }

    def _attribute_is_value(self, target: Any) -> bool:
        # Picklability alone does not make a tool a snapshot: its methods must
        # still run on the live parent. Only declared data crosses as a value.
        if not is_picklable(target):
            return False
        try:
            self._codec.loads(self._codec.dumps(target))
        except Exception:
            return False
        return True

    def _introspect(self, fn: Any, path: list[str], *args: Any, **kwargs: Any) -> Any:
        if not isinstance(path, list) or not all(isinstance(p, str) for p in path):
            raise CellSerializationError("invalid introspection path")
        return fn(self._walk_path(path), *args, **kwargs)

    async def _dispatch_tool_call(self, msg: dict[str, Any]) -> dict[str, Any]:
        task = asyncio.current_task()
        assert task is not None
        self._tool_tasks.add(task)
        try:
            if self._stop.is_set():
                raise asyncio.CancelledError
            return await super()._dispatch_tool_call(msg)
        finally:
            self._tool_tasks.discard(task)

    def _start_worker(self) -> None:
        from nooa._win_job import ProcessJob

        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=spawn_worker_main, args=(child,), daemon=True, name="nooa-spawn-experiment"
        )
        self._conn, self._proc = parent, proc
        self._stop = threading.Event()
        try:
            self._job = ProcessJob(**self._job_limits)
            proc.start()
            # CPython's Windows spawn launcher bypasses the venv redirector.
            # No application payload is released until this actual PID is assigned.
            assert proc.pid is not None
            self._job.assign(proc.pid)
        except BaseException:
            self._terminate_worker()
            raise
        finally:
            child.close()

    async def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        await self._aterminate_worker()
        try:
            self._start_worker()
            conn, proc = self._conn, self._proc
            assert conn is not None and proc is not None
            self._io_task = asyncio.create_task(
                asyncio.to_thread(self._handshake, conn, proc.pid, self._stop)
            )
            await asyncio.wait_for(asyncio.shield(self._io_task), self._startup_timeout_s)
        except TimeoutError as exc:
            raise SandboxUnavailable("spawn worker startup deadline exceeded") from exc
        except asyncio.CancelledError:
            raise
        except SandboxUnavailable:
            raise
        except Exception as exc:
            raise SandboxUnavailable(f"spawn worker startup failed: {exc}") from exc

    def _handshake(self, conn: Any, pid: int | None, stop: threading.Event) -> None:
        end = time.monotonic() + self._startup_timeout_s

        def receive(expected: str) -> dict[str, Any]:
            while not stop.is_set():
                if time.monotonic() >= end:
                    raise SandboxUnavailable("spawn worker startup deadline exceeded")
                if not conn.poll(0.05):
                    continue
                msg = self._codec.loads(conn.recv_bytes(maxlength=65536))
                if not isinstance(msg, dict) or msg.get("type") != expected:
                    raise SandboxUnavailable(f"unexpected spawn startup message: {msg!r}")
                return msg
            raise SandboxUnavailable("spawn worker startup cancelled")

        if receive("hello").get("pid") != pid:
            raise SandboxUnavailable("spawn worker PID does not match the assigned job process")
        conn.send(
            {
                "op": "bootstrap",
                "payload": self._bootstrap,
                "restrictions": self._restrictions,
                "max_error": self._max_error,
                "error_tail": self._error_tail,
            }
        )
        receive("ready")

    async def run_cell(self, code: str, *, execution_count: int = 1) -> ExecutionResult:
        async with self._lock:
            if self._closed:
                raise WorkerDiedError("spawn executor is closed")
            if self._disabled:
                return self._synth_error(WorkerDiedError("spawn recovery='disabled'"))
            self._running_task = asyncio.current_task()
            try:
                await self._ensure_worker()
                conn, proc = self._conn, self._proc
                assert conn is not None and proc is not None
                req_id = self._next_id()
                loop = asyncio.get_running_loop()

                def exchange() -> dict[str, Any]:
                    conn.send(
                        {
                            "op": "run",
                            "id": req_id,
                            "code": code,
                            "execution_count": execution_count,
                        }
                    )
                    return self._recv_until_result(
                        req_id,
                        (
                            self._cell_timeout + self._config.timeout_grace_s
                            if self._cell_timeout is not None
                            else None
                        ),
                        loop,
                        conn=conn,
                        proc=proc,
                        stop=self._stop,
                        max_message_bytes=32 * 1024 * 1024,
                        strict_messages=True,
                    )

                self._io_task = asyncio.create_task(asyncio.to_thread(exchange))
                response = await asyncio.shield(self._io_task)
                dto = dto_from_wire(response.get("result"))
                return dto_to_result(dto, signal_factory=self._signal_factory, codec=self._codec)
            except asyncio.CancelledError:
                await self._finish_retirement()
                raise
            except SandboxUnavailable:
                await self._finish_retirement()
                raise
            except (
                WorkerDiedError,
                CellTimeoutError,
                CellSerializationError,
                OSError,
                EOFError,
            ) as exc:
                await self._finish_retirement()
                error = exc if isinstance(exc, CellTimeoutError) else WorkerDiedError(str(exc))
                return self._synth_error(error)
            except BaseException:
                await self._finish_retirement()
                raise
            finally:
                self._running_task = None

    async def _finish_retirement(self) -> None:
        """Finish cleanup despite repeated cancellation; don't abandon an IPC reader."""
        task = asyncio.create_task(self._aterminate_worker())
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                continue
        if self._config.recovery == "disabled":
            self._disabled = True

    def _terminate_worker(self) -> None:
        self._stop.set()
        if self._job is not None:
            self._job.close()
            self._job = None
        proc = self._proc
        if proc is not None:
            if proc.pid is not None:
                if proc.is_alive():
                    proc.terminate()
                proc.join(2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(2)
                if proc.is_alive():
                    raise WorkerDiedError("spawn worker could not be reaped")
            proc.close()
            self._proc = None

    async def _aterminate_worker(self) -> None:
        self._stop.set()
        # Killing first unblocks recv_bytes/send even for partial or oversized frames.
        await asyncio.to_thread(self._terminate_worker)
        if self._io_task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._io_task
            self._io_task = None
        tasks = list(self._tool_tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def aclose(self) -> None:
        self._closed = True
        running = self._running_task
        if running is not None and running is not asyncio.current_task():
            running.cancel()
        async with self._lock:
            await self._finish_retirement()

    def close_sync(self) -> None:
        if self._running_task is not None:
            raise RuntimeError("use await aclose() while a spawn cell is running")
        self._closed = True
        self._terminate_worker()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
