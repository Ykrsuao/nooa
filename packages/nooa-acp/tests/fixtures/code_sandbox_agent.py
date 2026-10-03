# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Code-mode ACP fixture: real native cells invoke the ordinary host shell tools."""

import asyncio
import json
import os
import shlex

from nooa_acp.server import serve

from nooa.llm_types import LLMResponse, ToolCall
from nooa.unifiedllm import FakeLLMClient

HOST_NETWORK_COMMAND = (
    shlex.join(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--noproxy",
            "*",
            "--max-time",
            "10",
            os.environ["NOOA_ACP_TEST_HOST_URL"],
        ]
    )
    + " > host-network.txt"
)

CODE = f"""
request = notification['user_messages'][-1]
if request == 'block':
    await self.shell.run('python -c "import time;time.sleep(60)"', timeout=60)
else:
    output_path = Path('code-result.txt')
    assert output_path.name == 'code-result.txt'
    note = Context('native type aliases verified', prefix=True)
    self.context['sandbox_namespace_probe'] = note
    assert self.context['sandbox_namespace_probe'] == note.value
    assert CodeActConfig(cell_timeout=2).cell_timeout == 2
    await self.shell.write_file(str(output_path), 'host edit persists')
    result = await self.shell.run('echo host-command > code-command.txt')
    assert result.returncode == 0, result.stderr
    assert (await self.shell.read('code-result.txt')).text == 'host edit persists'
    network = await self.shell.run({HOST_NETWORK_COMMAND!r})
    assert network.returncode == 0, network.stderr
    assert (await self.shell.read('host-network.txt')).text == 'host-network-verified'
    return_result(Done(message='Code sandbox verified over ACP.', explanation='checked host tools'))
"""


def llm_factory():
    return FakeLLMClient(
        [
            LLMResponse(
                parts=(
                    ToolCall(
                        id=f"code-{i}", name="execute_python", arguments=json.dumps({"code": CODE})
                    ),
                ),
                finish_reason="tool_calls",
            )
            for i in range(4)
        ],
        strict_exhaustion=True,
    )


asyncio.run(serve(llm_factory, sandbox="auto", sandbox_mode="code"))
