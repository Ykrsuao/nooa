# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal LPAC persistent-worker integration; not a public CodeAct backend.

The caller owns a private runtime with stage_framework() already completed.
Close this executor before closing that runtime. Host tools are explicit
capabilities: granting a callback grants its effects, not just its name.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType, SimpleNamespace
from typing import Any, Literal, cast

from nooa.runtime.sandbox import wire
from nooa.runtime.sandbox._appcontainer import _AppContainerPython, _long_path
from nooa.runtime.sandbox._lpac_process import LpacProcess
from nooa.runtime.sandbox._spawn import _SpawnExecutor
from nooa.runtime.sandbox.errors import CellSerializationError, SandboxUnavailable

_ToolPolicy = Callable[[Mapping[str, Any]], bool]


class _LpacExecutor(_SpawnExecutor):
    """Run with exact callback grants and optional parent-side argument policies.

    Policies receive signature-bound arguments (including defaults) and must
    return literal True. They are trusted, fast synchronous host code, not
    isolated validators or automatic path/URL sanitizers. Declared transport
    constructors run during decoding, before policy checks. Ungated callbacks
    retain their full explicitly granted effects.

    Optional job limits use native Windows units: total committed bytes
    (including startup) and lifetime user-mode CPU seconds, not Linux headroom
    or per-cell budgets. A replacement worker starts a new job with the same
    limits; these are not session-wide quotas. Zero disables an optional limit.
    """

    def __init__(
        self,
        runtime: _AppContainerPython,
        *,
        tools: Mapping[str, Callable[..., Any]] | None = None,
        tool_policies: Mapping[str, _ToolPolicy] | None = None,
        framework_builtins: dict[str, Any] | None = None,
        module_globals: dict[str, Any] | None = None,
        data_types: Iterable[type] = (),
        tool_introspection: bool = False,
        restrictions: Any = None,
        max_error: int | None = None,
        error_tail: int | None = None,
        cell_timeout: float | None = 10,
        startup_timeout_s: float = 30,
        broker_timeout_s: float = 30,
        recovery: Literal["restart_empty", "disabled"] = "restart_empty",
        frame_timeout_s: float = 5,
        memory_limit_bytes: int = 0,
        cpu_time_limit_s: int = 0,
    ):
        import math

        if not math.isfinite(frame_timeout_s) or frame_timeout_s <= 0:
            raise ValueError("frame_timeout_s must be finite and positive")
        tools = dict(tools or {})
        callbacks = dict(framework_builtins or {})
        for name, callback in tools.items():
            if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
                raise ValueError("LPAC tool names must be public identifiers")
            if not callable(callback):
                raise TypeError("LPAC tools must be explicitly granted callables")
        self._tool_policies = dict(tool_policies or {})
        if self._tool_policies.keys() - tools.keys():
            raise ValueError("LPAC policies must name explicitly granted tools")
        self._tool_signatures = {}
        for name, policy in self._tool_policies.items():
            if not callable(policy):
                raise TypeError("LPAC tool policies must be synchronous predicates")
            target = policy if inspect.isroutine(policy) else policy.__call__
            if (
                inspect.iscoroutinefunction(target)
                or inspect.isasyncgenfunction(target)
                or inspect.isgeneratorfunction(target)
            ):
                raise TypeError("LPAC tool policies must be synchronous predicates")
            self._tool_signatures[name] = inspect.signature(tools[name])
        self._runtime = runtime
        self._packages = _long_path(runtime.runtime / "packages")
        if runtime._closed or not runtime._framework_staged:
            raise SandboxUnavailable("LPAC runtime must have a staged framework")
        self._allowed = {
            "agent": frozenset(tools),
            "framework": frozenset(
                name
                for name, value in callbacks.items()
                if callable(value) and not isinstance(value, type)
            ),
        }
        self._data_types = _declared_types(data_types)
        self._tool_docs = None
        if tool_introspection:
            from nooa.agentdoc import doc

            self._tool_docs = {name: doc(callback) for name, callback in tools.items()}
        self._frame_timeout_s = frame_timeout_s
        self._processes: list[LpacProcess] = []
        self._cleanup_lock = threading.Lock()
        # Reuse only the internal IPC/cell lifecycle. _prepare_backend and
        # _start_worker below replace its unrestricted multiprocessing launcher.
        super().__init__(
            SimpleNamespace(**tools),
            unsafe_no_isolation=True,
            module_globals=module_globals or {},
            framework_builtins=callbacks,
            restrictions=restrictions,
            max_error=max_error,
            error_tail=error_tail,
            cell_timeout=cell_timeout,
            startup_timeout_s=startup_timeout_s,
            broker_timeout_s=broker_timeout_s,
            recovery=recovery,
            memory_limit_bytes=memory_limit_bytes,
            cpu_time_limit_s=cpu_time_limit_s,
            active_process_limit=1,
        )

    def _prepare_backend(self):
        self._ctx = None
        self._degraded = []
        self._codec = wire.Codec(self._data_types)

    def _start_worker(self):
        from nooa._win_job import ProcessJob

        self._stop = threading.Event()
        self._job = ProcessJob(**self._job_limits)
        code = (
            "import sys, os\n"
            "sys.stderr.reconfigure(encoding='utf-8')\n"
            f"sys.path.insert(0, {str(self._packages)!r})\n"
            "os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'\n"
            "os.environ['PYTHON_DOTENV_DISABLED'] = '1'\n"
            "from nooa.runtime.sandbox._lpac_transport import PipeConnection\n"
            "from nooa.runtime.sandbox._spawn_bootstrap import spawn_worker_main\n"
            "spawn_worker_main(PipeConnection(sys.stdin.buffer.raw, sys.stdout.buffer.raw, "
            f"frame_timeout_s={self._frame_timeout_s!r}))\n"
        )
        try:
            process = LpacProcess(
                self._runtime,
                self._job,
                code,
                frame_timeout_s=min(
                    self._frame_timeout_s,
                    self._cell_timeout if self._cell_timeout is not None else self._frame_timeout_s,
                ),
            )
            self._proc = cast(Any, process)  # Native adapter implements the process lifecycle.
            self._conn = process.connection
            self._processes.append(process)
        except BaseException:
            self._job.close()
            self._job = None
            raise

    def _decode_tool_call(self, msg):
        # Check the exact operation BEFORE walking host attributes or decoding
        # tool arguments. Neither dunder traversal nor attr/setattr/iter on live
        # host objects is a capability implied by granting a function.
        root, kind, path = msg.get("root", "agent"), msg.get("kind"), msg.get("path")
        if root == "introspection" and self._tool_docs is not None:
            if kind != "call" or path not in (["doc"], ["methods"], ["variables"]):
                raise CellSerializationError("LPAC introspection operation is not granted")
            # Introspection accepts only primitive paths, not application objects
            # whose validators or constructors would execute in the parent.
            payload = msg.get("payload")
            if not isinstance(payload, bytes):
                raise CellSerializationError("invalid LPAC introspection payload")
            value = wire.Codec().loads(payload)
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not isinstance(value[0], tuple)
                or len(value[0]) != 1
                or value[1] != {}
                or not self._can_describe(value[0][0])
            ):
                raise CellSerializationError("LPAC introspection requires an exact granted path")
            return {"root": root, "kind": kind, "path": path, "args": value[0], "kwargs": {}}
        if (
            not isinstance(root, str)
            or kind not in ("attr", "call")
            or not isinstance(path, list)
            or len(path) != 1
            or not isinstance(path[0], str)
            or path[0] not in self._allowed.get(root, ())
        ):
            raise CellSerializationError("LPAC broker operation is not explicitly granted")
        return super()._decode_tool_call(msg)

    def _can_describe(self, path):
        return isinstance(path, list) and (
            not path
            or (len(path) == 1 and isinstance(path[0], str) and path[0] in self._allowed["agent"])
        )

    def _introspect(self, fn, path, *args, **kwargs):
        if self._tool_docs is None or not self._can_describe(path) or args or kwargs:
            raise CellSerializationError("LPAC introspection requires an exact granted path")
        if fn.__name__ == "variables":
            return ""
        if path:
            return self._tool_docs[path[0]]
        return "\n\n".join(self._tool_docs.values())

    async def _dispatch_tool_call(self, msg):
        if msg.get("root", "agent") == "agent" and msg.get("kind") == "call":
            name = msg["path"][0]
            if name in self._tool_policies:
                try:
                    bound = self._tool_signatures[name].bind(*msg["args"], **msg["kwargs"])
                    bound.apply_defaults()
                    decision = self._tool_policies[name](MappingProxyType(bound.arguments))
                    # A wrapped async predicate must never become a truthy permission grant.
                    if inspect.iscoroutine(decision):
                        decision.close()
                    if decision is not True:
                        raise PermissionError
                except Exception:
                    return {
                        "ok": False,
                        "error_type": "PermissionError",
                        "error": f"LPAC tool policy denied {name}",
                    }
        response = await super()._dispatch_tool_call(msg)
        try:
            for key in ("result", "signal_result"):
                if key in response:
                    response[key] = self._codec.loads(self._codec.dumps(response[key]))
        except Exception:
            return {
                "ok": False,
                "error_type": "CellSerializationError",
                "error": "LPAC tools must return supported data snapshots, not live objects",
            }
        return response

    async def _aterminate_worker(self):
        await super()._aterminate_worker()
        self._close_retired_streams()

    def _terminate_worker(self):
        # Cancellation may interrupt an await of a still-running to_thread call.
        # Serialize retries so native process handles are never closed concurrently.
        with self._cleanup_lock:
            super()._terminate_worker()

    def close_sync(self):
        super().close_sync()
        self._close_retired_streams()

    def _close_retired_streams(self):
        # Retain ownership across cancellation of a to_thread teardown. A later
        # retirement can still close stderr and pipe endpoints of the old process.
        for process in self._processes[:]:
            if process._closed:
                process.close_streams()
                self._processes.remove(process)


def _declared_types(types: Iterable[type]) -> dict[str, type]:
    """Trust only explicitly declared data types and their annotated field types."""
    pending = list(types)
    for cls in pending:
        if not isinstance(cls, type) or not wire._is_data_type(cls):
            raise TypeError("LPAC data_types must contain data model classes")
    found = {}
    while pending:
        cls = pending.pop()
        if wire._is_data_type(cls) and wire.type_key(cls) not in found:
            found[wire.type_key(cls)] = cls
            pending.extend(wire._annotated_types(cls))
    return found
