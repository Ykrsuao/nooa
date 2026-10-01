# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Script only the model; use real ACP, coding tools, shell, and MCP."""

import asyncio
import json
import shlex
import sys

from nooa_acp.server import serve

from nooa.llm_types import LLMResponse, ToolCall
from nooa.unifiedllm import FakeLLMClient

TEXT = "\u4e2d\u6587\u9a8c\u6536"
FILE_NAME = "\u4ee3\u7801 file.py"
PYTHON = shlex.quote(sys.executable.replace("\\", "/"))


def llm_factory() -> FakeLLMClient:
    command = "printf started > command-started; sleep 60; printf leaked > command-finished"
    cells = [
        f"await self.shell.run({command!r}, timeout=90)",
        "\n".join(
            [
                f"await self.shell.write_file({FILE_NAME!r}, \"print('before')\\n\")",
                f"match = await self.shell.read({FILE_NAME!r})",
                "assert 'before' in match.text",
                f"await self.shell.replace({FILE_NAME!r}, 'before', {TEXT!r})",
                f"result = await self.shell.run({(PYTHON + ' ' + shlex.quote(FILE_NAME))!r})",
                "assert result.returncode == 0, result.stderr",
                f"assert result.stdout.strip() == {TEXT!r}, result.stdout",
                f"reply = await self.localcheck.echo(value={TEXT!r})",
                f"assert reply == {TEXT!r}, reply",
                "await self.shell.write_file('mcp-result.txt', reply)",
                "self.message(reply)",
                "return_result(Done(explanation='edited and verified after cancellation'))",
            ]
        ),
    ]
    return FakeLLMClient(
        [
            LLMResponse.model_validate(
                {
                    "content": "",
                    "tool_calls": [
                        ToolCall(
                            id=f"acceptance-{index}",
                            name="execute_python",
                            arguments=json.dumps({"code": code}),
                        )
                    ],
                    "finish_reason": "tool_calls",
                }
            )
            for index, code in enumerate(cells)
        ],
        strict_exhaustion=True,
    )


asyncio.run(serve(llm_factory))
