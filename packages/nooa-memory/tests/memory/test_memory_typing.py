# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contracts at optional memory dependency and nullable evidence boundaries."""

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest
from nooa_memory.config import EmbeddingConfig, ForgetPolicy, VectorConfig
from nooa_memory.embeddings import LiteLLMEmbedder
from nooa_memory.forgetting import ForgettingEngine
from nooa_memory.generative import ReflectionClient, llm_episode_writer
from nooa_memory.manager import MemoryManager
from nooa_memory.observability import per_memory_usage
from nooa_memory.schema import AccessRecord, Memory
from nooa_memory.store import MemoryStore
from nooa_memory.tracing_bridge import current_trace_ref, install_tracing_bridge
from nooa_memory.vector_backends import ChromaVectorIndex, SqliteVecVectorIndex, make_vector_index

from nooa.llm_types import CacheBoundary, LLMResponse
from nooa.unifiedllm import FakeLLMClient, UnifiedLLM


@pytest.mark.parametrize("configured", [False, True])
def test_embedding_kwargs_preserve_transport_and_optional_values(monkeypatch, configured):
    import litellm

    calls = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return {"data": [{"embedding": [3.0, 4.0]} for _ in kwargs["input"]]}

    monkeypatch.setattr(litellm, "embedding", embedding)
    config = EmbeddingConfig(
        backend="litellm",
        model="test-model",
        endpoint="https://embedding.test/v1" if configured else None,
        api_key="test-key" if configured else None,
        dimensions=2 if configured else None,
        timeout=0.25,
        num_retries=0,
        batch_size=2,
    )
    embedder = LiteLLMEmbedder(config)
    assert embedder.embed_batch([]) == []
    assert not calls
    vectors = embedder.embed_batch(["a", "b", "c"])
    assert len(vectors) == 3
    assert embedder.dim == 2
    assert all(np.allclose(v, [0.6, 0.8]) for v in vectors)
    expected = {"model": "test-model", "timeout": 0.25, "num_retries": 0}
    if configured:
        expected.update(api_base=config.endpoint, api_key=config.api_key, dimensions=2)
    assert calls == [dict(expected, input=["a", "b"]), dict(expected, input=["c"])]


@dataclass
class Reply:
    content: str | None


class ScriptedClient:
    def call(self, messages: list[dict[str, Any] | LLMResponse | CacheBoundary]) -> Reply:
        return Reply('{"noteworthy": true, "episode": "saved"}')


def _accept_client(client: ReflectionClient) -> ReflectionClient:
    return client


def test_reflection_client_accepts_runtime_and_structural_clients():
    runtime = FakeLLMClient()
    assert _accept_client(runtime) is runtime
    assert _accept_client(ScriptedClient()) is not None
    assert llm_episode_writer(ScriptedClient)("recent events") == "saved"


def _runtime_client_contract(client: UnifiedLLM) -> ReflectionClient:
    return client


@pytest.mark.parametrize("recorded", [False, True])
def test_usage_averages_only_available_evidence(recorded):
    store = MemoryStore(":memory:")
    try:
        memory = Memory(content="test", access_log=[])
        memory.access_log = [AccessRecord(ts=1.0, channel="created")]
        if recorded:
            memory.access_log += [
                AccessRecord(ts=2.0, channel="recalled", rank=0, score=0.0),
                AccessRecord(ts=3.0, channel="recalled", rank=2, score=0.5),
            ]
        usage = per_memory_usage(memory, forgetting=ForgettingEngine(store, ForgetPolicy()))
        assert usage["mean_rank"] == (1.0 if recorded else None)
        assert usage["mean_score"] == (0.25 if recorded else None)
    finally:
        store.close()


@pytest.mark.parametrize("dependency", ["sqlite_vec", "chromadb", "chromadb.config"])
def test_optional_vector_dependencies_fail_only_when_selected(monkeypatch, dependency):
    monkeypatch.setitem(sys.modules, dependency, None)
    assert len(make_vector_index(VectorConfig())) == 0
    if dependency == "sqlite_vec":
        conn = sqlite3.connect(":memory:")
        try:
            with pytest.raises(ImportError, match="sqlite-vec"):
                SqliteVecVectorIndex(conn, 2)
        finally:
            conn.close()
    else:
        with pytest.raises(ImportError, match="chromadb"):
            ChromaVectorIndex(VectorConfig(backend="chroma_embedded"))


def test_sqlite_vec_loads_optional_module_on_selection(monkeypatch):
    module = ModuleType("sqlite_vec")
    load = Mock()
    monkeypatch.setattr(module, "load", load, raising=False)
    monkeypatch.setattr(module, "serialize_float32", Mock(), raising=False)
    monkeypatch.setitem(sys.modules, "sqlite_vec", module)
    conn = Mock(spec=sqlite3.Connection)
    SqliteVecVectorIndex(conn, 2)
    load.assert_called_once_with(conn)
    assert [call.args for call in conn.enable_load_extension.call_args_list] == [(True,), (False,)]
    assert "embedding float[2]" in conn.execute.call_args.args[0]


@pytest.mark.parametrize("mode", ["http", "persistent", "ephemeral"])
def test_chroma_loads_optional_client_and_settings(monkeypatch, mode):
    module = ModuleType("chromadb")
    config_module = ModuleType("chromadb.config")
    settings = Mock()
    factory = Mock()
    monkeypatch.setattr(config_module, "Settings", settings, raising=False)
    for name in ("HttpClient", "PersistentClient", "EphemeralClient"):
        monkeypatch.setattr(module, name, factory, raising=False)
    monkeypatch.setitem(sys.modules, "chromadb", module)
    monkeypatch.setitem(sys.modules, "chromadb.config", config_module)
    config = VectorConfig(
        backend="chroma_http" if mode == "http" else "chroma_embedded",
        collection="test",
        host="vector.test",
        port=8123,
    )
    ChromaVectorIndex(config, path="memory.sqlite" if mode == "persistent" else ":memory:")
    settings.assert_called_once_with(anonymized_telemetry=False)
    expected = {"settings": settings.return_value}
    if mode == "http":
        expected.update(host="vector.test", port=8123)
    elif mode == "persistent":
        expected.update(path="memory.chroma")
    factory.assert_called_once_with(**expected)
    collection = factory.return_value.get_or_create_collection.call_args.kwargs
    assert collection["metadata"] == {"hnsw:space": "cosine"}
    assert (
        collection["name"].startswith("test-")
        if mode == "ephemeral"
        else collection["name"] == "test"
    )


def test_tracing_bridge_noops_without_optional_tracer(monkeypatch):
    from nooa_memory import tracing_bridge

    manager = Mock(spec=MemoryManager, store=Mock(path=":memory:"), owner="test", agent=Mock())
    handlers: list[Callable[[object], None]] = []

    def subscribe(name, handler):
        handlers.append(handler)
        return lambda: None

    manager.agent.event_manager.on.side_effect = subscribe
    monkeypatch.setattr(tracing_bridge, "_ot_trace", Mock())
    assert len(install_tracing_bridge(manager)) == 5
    monkeypatch.setattr(tracing_bridge, "_ot_trace", None)
    assert current_trace_ref() is None
    assert install_tracing_bridge(manager) == []
    for handler in handlers:
        handler(object())
