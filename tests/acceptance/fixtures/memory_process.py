# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise packaged skill discovery and a persistent offline memory store."""

import asyncio
import json
import sqlite3
import sys
from importlib.metadata import entry_points
from pathlib import Path

from nooa_memory import MemoryConfig
from nooa_memory.memory_skill import MemorySkill

from nooa import Agent
from nooa.unifiedllm import FakeLLMClient

ORIGINAL = "deploy verification \u4e2d\u6587\u8bb0\u5fc6: run python check.py"
UPDATED = "deploy verification \u4e2d\u6587\u8bb0\u5fc6: run python verified.py"


class MemoryAgent(Agent):
    pass


def main() -> None:
    db, phase = Path(sys.argv[1]), sys.argv[2]
    entry = next(ep for ep in entry_points(group="nooa.skills") if ep.name == "nemo.memory")
    assert entry.load() is MemorySkill
    llm = FakeLLMClient(strict_exhaustion=True)
    agent = MemoryAgent(llm=llm)
    skill = entry.load()(MemoryConfig(enabled=True, path=str(db)))
    skill.attach(agent)
    manager = skill._mgr
    assert manager is not None
    assert manager.config.vector.backend == "numpy"
    assert manager.config.embedding.backend == "hashing"
    try:
        if phase == "write":
            mid = skill.remember(ORIGINAL, type="skill", importance="HIGH")
            skill.remember("unrelated invoice retention schedule", importance="LOW")
            assert skill.stats().writes == 2
        else:
            hits = skill.search("deploy verification", k=1)
            assert len(hits) == 1
            mid = hits[0].id
        stored = manager.store.get(mid)
        assert stored is not None
        assert stored.content == (UPDATED if phase == "read" else ORIGINAL)
        embedding = manager.store.get_embedding(mid)
        assert embedding is not None and len(embedding) == manager.embedder.dim
        # Exercise the reloaded vector index directly, not only keyword search.
        nearest = manager.store.knn(manager.embedder.embed(stored.embedding_text()), k=1)
        assert nearest[0][0] == mid and nearest[0][1] > 0.99
        if phase == "update":
            assert skill.update_memory(mid, content=UPDATED, importance="CRITICAL")
        expected = ORIGINAL if phase == "write" else UPDATED
        recalled = skill.recall(expected, k=1)
        assert len(recalled) == 1 and recalled[0].id == mid
        assert recalled[0].content == expected
        report = {"id": mid, "content": expected, "count": manager.store.count()}
    finally:
        skill.detach()
        asyncio.run(llm.aclose())
    assert llm.call_count == 0
    assert not hasattr(agent, "_memory")
    try:
        manager.store.count()
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("Detached memory store is still open")
    # Check release before interpreter exit can hide a leaked SQLite handle.
    renamed = db.with_suffix(".closed")
    db.rename(renamed)
    renamed.rename(db)
    report["closed"] = True
    print(json.dumps(report))


if __name__ == "__main__":
    main()
