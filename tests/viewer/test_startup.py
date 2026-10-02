# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The trace viewer starts without loading the playground's model runtime."""

import os
import subprocess
import sys


def test_viewer_import_does_not_load_litellm(tmp_path):
    env = dict(os.environ)
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        NOOA_TRACE_DB=str(tmp_path / "traces.db"),
        NEMO_OO_USER_DIR=str(tmp_path / "user"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.modules['litellm'] = None\n"
            "from nooa.viewer.main import app\n"
            "assert app is not None\n",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
