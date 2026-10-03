# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native isolation through actual ACP stdio, including restore and cancellation."""

import asyncio
import sys
from pathlib import Path

import pytest
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AgentMessageChunk, TextContentBlock, ToolCallStart
from nooa_cli.sessions import SessionStore

pytestmark = [
    pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native sandbox required"),
    pytest.mark.timeout(600),
]


class Client:
    def __init__(self):
        self.updates = []
        self.command_started = asyncio.Event()
        self._updated = asyncio.Condition()

    async def session_update(self, session_id, update, **kwargs):
        async with self._updated:
            self.updates.append(update)
            if isinstance(update, ToolCallStart) and update.kind == "execute":
                self.command_started.set()
            self._updated.notify_all()

    async def wait_for_verified_messages(self, *, after: int, count: int) -> None:
        def received():
            return (
                sum(
                    isinstance(update, AgentMessageChunk)
                    and isinstance(update.content, TextContentBlock)
                    and "Sandbox verified" in update.content.text
                    for update in self.updates[after:]
                )
                >= count
            )

        async with self._updated:
            await asyncio.wait_for(self._updated.wait_for(received), 30)


def _replay_diagnostics(client: Client, after: int, user_dir: Path, session_id: str) -> str:
    databases = list((user_dir / "acp-sandbox-sessions").glob(f"*/{session_id}.db"))
    transcripts = {
        str(path): [
            (turn.role, turn.content) for turn in SessionStore(path.parent).load_turns(session_id)
        ]
        for path in databases
    }
    return (
        f"updates after load boundary: {client.updates[after:]!r}; persisted turns: {transcripts!r}"
    )


async def test_native_acp_new_prompt_cancel_reuse_close_and_load(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    user_dir = tmp_path / "trusted-user"
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(user_dir))
    monkeypatch.delenv("NEMO_OO_SETTINGS", raising=False)
    marker = tmp_path / "workspace-import-ran"
    skills = root / ".agents" / "skills"
    skills.mkdir(parents=True)
    (skills / "danger.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n",
        encoding="utf-8",
    )
    client = Client()
    fixture = Path(__file__).parent / "fixtures" / "sandbox_agent.py"
    async with spawn_agent_process(
        client,  # type: ignore[arg-type]  # Only session_update is used by this test server.
        sys.executable,
        "-X",
        f"pycache_prefix={tmp_path / 'cache'}",
        str(fixture),
        cwd=root,
    ) as (connection, _process):
        await connection.initialize(PROTOCOL_VERSION)
        session = await asyncio.wait_for(connection.new_session(str(root)), 180)
        assert not marker.exists()
        response = await asyncio.wait_for(
            connection.prompt(session.session_id, [text_block("verify")]), 180
        )
        assert response.stop_reason == "end_turn"
        assert (root / "result.txt").read_text() == "persistent edit"
        assert not (root / "command-only.txt").exists()
        assert not (root / ".nooa" / "sessions").exists()

        client.command_started.clear()
        blocked = asyncio.create_task(connection.prompt(session.session_id, [text_block("block")]))
        await asyncio.wait_for(client.command_started.wait(), 180)
        await connection.cancel(session.session_id)
        cancelled = await asyncio.wait_for(blocked, 90)
        assert cancelled.stop_reason == "cancelled"
        again = await asyncio.wait_for(
            connection.prompt(session.session_id, [text_block("verify after cancel")]), 180
        )
        assert again.stop_reason == "end_turn"
        # SDK notification handlers run in separate tasks. Drain the original
        # successful replies so a delayed original cannot satisfy replay below.
        await client.wait_for_verified_messages(after=0, count=2)
        await connection.close_session(session.session_id)
        sessions = await connection.list_sessions(cwd=str(root))
        assert session.session_id in [item.session_id for item in sessions.sessions]
        count = len(client.updates)
        await asyncio.wait_for(
            connection.load_session(session_id=session.session_id, cwd=str(root)), 180
        )
        assert not marker.exists()
        # Receiving the load response does not drain scheduled notification
        # callbacks. Require both persisted successful replies before proceeding.
        try:
            await client.wait_for_verified_messages(after=count, count=2)
        except TimeoutError:
            pytest.fail(_replay_diagnostics(client, count, user_dir, session.session_id))
        restored = await asyncio.wait_for(
            connection.prompt(session.session_id, [text_block("verify after restore")]), 180
        )
        assert restored.stop_reason == "end_turn"
        assert not (root / "command-only.txt").exists()
        await connection.close_session(session.session_id)
