# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Code-mode native acceptance over ACP stdio, shared by Windows and Linux."""

import asyncio
import sys
from pathlib import Path

import pytest
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AgentMessageChunk, TextContentBlock, ToolCallStart

pytestmark = [
    pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native sandbox required"),
    pytest.mark.timeout(600),
]


class Client:
    def __init__(self):
        self.updates = []
        self.command_started = asyncio.Event()
        self._changed = asyncio.Condition()

    async def session_update(self, session_id, update, **kwargs):
        async with self._changed:
            self.updates.append(update)
            if isinstance(update, ToolCallStart) and update.kind == "execute":
                self.command_started.set()
            if getattr(update, "status", None) == "failed":
                print(f"Native ACP tool failure: {update!r}")
            self._changed.notify_all()

    async def verified(self, after, count):
        def complete():
            return (
                sum(
                    isinstance(update, AgentMessageChunk)
                    and isinstance(update.content, TextContentBlock)
                    and "Code sandbox verified over ACP." in update.content.text
                    for update in self.updates[after:]
                )
                >= count
            )

        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(complete), 30)


@pytest.fixture
async def host_http_url():
    async def respond(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            body = b"host-network-verified"
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with listener:
        yield f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/"


async def test_native_code_acp_host_shell_cancel_reuse_close_and_load(
    tmp_path, monkeypatch, host_http_url
):
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(home / "user-config"))
    monkeypatch.setenv("NOOA_ACP_TEST_HOST_URL", host_http_url)
    monkeypatch.delenv("NEMO_OO_SETTINGS", raising=False)
    skill = root / ".agents" / "skills" / "host_skill.py"
    skill.parent.mkdir(parents=True)
    marker = root / "skill-loaded"
    skill.write_text(
        "from pathlib import Path\nfrom nooa.skill import Skill\n"
        f"Path({str(marker)!r}).write_text('loaded on host')\n"
        "class HostSkill(Skill):\n    def hello(self):\n        return 'host capability'\n",
        encoding="utf-8",
    )
    fixture = Path(__file__).parent / "fixtures" / "code_sandbox_agent.py"
    client = Client()
    async with spawn_agent_process(
        client,  # pyright: ignore[reportArgumentType]
        sys.executable,
        "-X",
        f"pycache_prefix={tmp_path / 'cache'}",
        str(fixture),
        cwd=root,
        env={
            "HOME": str(home),
            "USERPROFILE": str(home),
            "NEMO_OO_USER_DIR": str(home / "user-config"),
            "NOOA_ACP_TEST_HOST_URL": host_http_url,
        },
        transport_kwargs={"stderr": None},
    ) as (connection, _process):
        initialized = await connection.initialize(PROTOCOL_VERSION)
        assert initialized.agent_capabilities is not None
        assert initialized.agent_capabilities.mcp_capabilities is not None
        assert initialized.agent_capabilities.mcp_capabilities.http
        created = await asyncio.wait_for(connection.new_session(str(root)), 180)
        assert marker.read_text() == "loaded on host"
        answer = await asyncio.wait_for(
            connection.prompt(created.session_id, [text_block("verify")]), 180
        )
        assert answer.stop_reason == "end_turn"
        assert (root / "code-result.txt").read_text() == "host edit persists"
        assert (root / "code-command.txt").read_text().strip() == "host-command"
        assert (root / "host-network.txt").read_text() == "host-network-verified"
        await client.verified(0, 1)

        client.command_started.clear()
        pending = asyncio.create_task(connection.prompt(created.session_id, [text_block("block")]))
        await asyncio.wait_for(client.command_started.wait(), 180)
        await connection.cancel(created.session_id)
        assert (await asyncio.wait_for(pending, 90)).stop_reason == "cancelled"
        answer = await asyncio.wait_for(
            connection.prompt(created.session_id, [text_block("after cancel")]), 180
        )
        assert answer.stop_reason == "end_turn"
        await client.verified(0, 2)
        await connection.close_session(created.session_id)

        boundary = len(client.updates)
        await asyncio.wait_for(
            connection.load_session(cwd=str(root), session_id=created.session_id), 180
        )
        await client.verified(boundary, 2)
        answer = await asyncio.wait_for(
            connection.prompt(created.session_id, [text_block("after restore")]), 180
        )
        assert answer.stop_reason == "end_turn"
        await client.verified(boundary, 3)
        assert (root / "code-command.txt").read_text().strip() == "host-command"
        assert (root / "host-network.txt").read_text() == "host-network-verified"
        await connection.close_session(created.session_id)
