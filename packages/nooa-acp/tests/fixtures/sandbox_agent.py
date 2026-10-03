# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""A real sandbox ACP server with deterministic responses for wire acceptance."""

import asyncio
import json

from nooa_acp.server import serve

from nooa.llm_types import LLMResponse, ToolCall
from nooa.unifiedllm import FakeLLMClient

CODE = """
request = notification['user_messages'][-1]
if request == 'block':
    await self.run_command('python -c "import time;time.sleep(60)"', timeout_s=60)
else:
    await self.workspace_write('result.txt', 'persistent edit')
    assert (await self.workspace_read('result.txt')) == 'persistent edit'
    r = await self.run_command('echo private > command-only.txt')
    assert r['returncode'] == 0, r
    assert r['changes_discarded']
    return_result(Done(message='Sandbox verified over ACP.', explanation='checked'))
"""


def llm_factory():
    return FakeLLMClient(
        [
            LLMResponse(
                parts=(
                    ToolCall(
                        id=f"sandbox-{i}",
                        name="execute_python",
                        arguments=json.dumps({"code": CODE}),
                    ),
                ),
                finish_reason="tool_calls",
            )
            for i in range(4)
        ],
        strict_exhaustion=True,
    )


asyncio.run(serve(llm_factory, sandbox="auto"))
