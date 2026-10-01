# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Script the model and redirect Harbor paths; keep the real runner and agents."""

import json
import shlex
import sys
from pathlib import Path

from nooa_bench import runner

from nooa import unifiedllm
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

TEXT = "\u4e2d\u6587\u9a8c\u6536"
FILE_NAME = "\u4ee3\u7801 file.py"
VERIFY = shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(FILE_NAME)
shell_process = None


class RecordingLLM(FakeLLMClient):
    close_count = 0

    async def aclose(self):
        self.close_count += 1
        await super().aclose()


def main() -> None:
    output = Path(sys.argv[1])
    runner.LOGS_DIR = output
    runner.TRACES_DIR = output / "traces"
    runner.ANSWER_FILE = output / "answer.txt"
    cells = [
        "\n".join(
            [
                f"await self.shell.write_file({FILE_NAME!r}, \"print('before')\\n\")",
                f"await self.shell.replace({FILE_NAME!r}, 'before', {TEXT!r})",
                f"checked = await self.shell.run({VERIFY!r})",
                "assert checked.returncode == 0, checked.stderr",
                f"assert checked.stdout == {TEXT!r}, checked.stdout",
                "import __main__",
                "__main__.shell_process = self.shell._session._process",
                "assert __main__.shell_process.returncode is None",
                "print(checked.stdout)",
            ]
        ),
        "return_result(TaskResult("
        "solution_description='Edited and verified the file', "
        f"evidence=checked.stdout, how_to_verify={VERIFY!r}))",
    ]
    llm = RecordingLLM(
        [
            LLMResponse(
                parts=(
                    ToolCall(
                        id=f"acceptance-{index}",
                        name="python_cell",
                        arguments=json.dumps({"code": code}),
                    ),
                ),
                finish_reason="tool_calls",
            )
            for index, code in enumerate(cells)
        ],
        strict_exhaustion=True,
    )
    unifiedllm.get_llm_client = lambda *args, **kwargs: llm
    try:
        runner.main(args=sys.argv[2:])
    except SystemExit as exit:
        assert exit.code == 0, f"Runner exited with {exit.code}"
    assert shell_process is not None
    assert shell_process.returncode is not None, "Runner retained its shell process"
    (output / "lifecycle.json").write_text(
        json.dumps({"calls": llm.call_count, "closes": llm.close_count, "shell_stopped": True}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
