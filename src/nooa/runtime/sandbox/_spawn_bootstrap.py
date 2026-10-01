# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trusted, data-only bootstrap for the internal Windows spawn experiment.

This is not a containment layer. Imports can execute application module code;
callers must use import-safe modules and the normal multiprocessing main guard.
Live agents, CurrentCall objects and callback closures never enter this payload.
"""

from __future__ import annotations

import importlib
import inspect
import os
import threading
import types
import typing
from functools import reduce
from multiprocessing.connection import _ConnectionBase as Connection
from operator import or_
from typing import Any

from nooa.runtime.sandbox import wire
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.runtime.sandbox.worker import (
    _PROXY_STATE,
    ChildBroker,
    ParentAgentProxy,
    _BrokerCallable,
    _is_async_callable,
    _NestedProxy,
    build_namespace,
    serve_cells,
)


def import_ref(value: Any) -> tuple[str, str]:
    module = value.__name__ if isinstance(value, types.ModuleType) else value.__module__
    name = "" if isinstance(value, types.ModuleType) else value.__qualname__
    if module in ("__main__", "__mp_main__") or "<locals>" in name:
        raise ValueError("use a type/function from an import-safe module, not __main__ or locals")
    ref = (module, name)
    if resolve_ref(ref) is not value:
        raise ValueError(f"{module}.{name} does not resolve to the original object")
    return ref


def resolve_ref(ref: tuple[str, str]) -> Any:
    obj = importlib.import_module(ref[0])
    for name in ref[1].split(".") if ref[1] else ():
        obj = getattr(obj, name)
    return obj


def describe(value: Any, codec: wire.Codec, label: str) -> dict[str, Any]:
    try:
        if value is type(None):
            return {"kind": "none_type"}
        origin = typing.get_origin(value)
        if origin is not None:
            if origin is types.UnionType or origin is typing.Union:
                ref = None
            elif origin is typing.Literal:
                ref = ("typing", "Literal")
            elif origin is typing.Annotated:
                ref = ("typing", "Annotated")
            else:
                ref = import_ref(origin)
            return {
                "kind": "generic",
                "origin": ref,
                "args": [describe(arg, codec, label) for arg in typing.get_args(value)],
            }
        if isinstance(value, (types.ModuleType, type)) or inspect.isroutine(value):
            return {"kind": "import", "ref": import_ref(value)}
        data = codec.dumps(value)
        codec.loads(data)  # Validate the declared-type contract before starting a process.
        return {"kind": "value", "data": data}
    except Exception as exc:
        raise SandboxUnavailable(f"spawn bootstrap cannot transfer {label!r}: {exc}") from exc


def restore(description: dict[str, Any], codec: wire.Codec) -> Any:
    if description["kind"] == "import":
        return resolve_ref(description["ref"])
    if description["kind"] == "none_type":
        return type(None)
    if description["kind"] == "generic":
        args = tuple(restore(arg, codec) for arg in description["args"])
        ref = description["origin"]
        return reduce(or_, args) if ref is None else resolve_ref(ref)[args]
    return codec.loads(description["data"])


def make_bootstrap(
    module_globals: dict[str, Any],
    framework_builtins: dict[str, Any],
    codec: wire.Codec,
) -> dict[str, Any]:
    type_refs = {}
    for key, cls in codec.types.items():
        try:
            type_refs[key] = import_ref(cls)
        except Exception as exc:
            raise SandboxUnavailable(
                f"spawn bootstrap cannot import data type {key!r}: {exc}"
            ) from exc
    module = {name: describe(value, codec, name) for name, value in module_globals.items()}
    values, callbacks = {}, {}
    call_type = None
    for name, value in framework_builtins.items():
        if name == "_call":
            call_type = describe(value.return_type, codec, "_call.return_type")
        elif callable(value) and not isinstance(value, type):
            callbacks[name] = _is_async_callable(value)
        else:
            values[name] = describe(value, codec, name)
    return {
        "types": type_refs,
        "module": module,
        "values": values,
        "callbacks": callbacks,
        "call_type": call_type,
    }


def _introspection(fn: Any, broker: ChildBroker, proxy: ParentAgentProxy) -> Any:
    def wrapper(obj: Any = proxy, *args: Any, **kwargs: Any) -> Any:
        if obj is proxy:
            path = []
        elif isinstance(obj, _BrokerCallable):
            path = obj._path
        elif isinstance(obj, _NestedProxy):
            path = _PROXY_STATE[obj][1]
        else:
            return fn(obj, *args, **kwargs)
        return broker.call([fn.__name__], (path, *args), kwargs)

    return wrapper


def spawn_worker_main(conn: Connection) -> None:  # pragma: no cover - real child
    """Wait for job assignment before receiving application bootstrap or cells."""
    try:
        wire.send(conn, {"type": "hello", "pid": os.getpid()})
        init = conn.recv()
        if init.get("op") != "bootstrap":
            return
        payload = init["payload"]
        codec = wire.Codec({key: resolve_ref(ref) for key, ref in payload["types"].items()})
        lock = threading.RLock()
        agent_broker = ChildBroker(conn, lock)
        builtins_broker = ChildBroker(conn, lock, root="framework")
        introspect_broker = ChildBroker(conn, lock, root="introspection")
        proxy = ParentAgentProxy(agent_broker, None)
        module = {name: restore(value, codec) for name, value in payload["module"].items()}
        values = {name: restore(value, codec) for name, value in payload["values"].items()}
        values.update(
            {
                name: _BrokerCallable(builtins_broker, [name], is_async)
                for name, is_async in payload["callbacks"].items()
            }
        )
        if payload["call_type"] is not None:
            values["_call"] = types.SimpleNamespace(
                return_type=restore(payload["call_type"], codec)
            )
        from nooa.agentdoc import doc
        from nooa.agentdoc.introspect import methods, variables

        helpers = {
            fn.__name__: _introspection(fn, introspect_broker, proxy)
            for fn in (doc, methods, variables)
        }
        helpers["help"] = helpers["doc"]
        namespace = build_namespace(
            None,
            {**helpers, **values},
            proxy,
            init.get("restrictions"),
            module_globals=module,
        )
        wire.send(conn, {"type": "ready"})
        serve_cells(conn, namespace, init)
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            wire.send(conn, {"type": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        conn.close()
