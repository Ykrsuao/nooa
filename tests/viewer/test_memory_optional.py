# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Viewer startup and responses without the optional memory package."""

import os
import subprocess
import sys


def test_memory_routes_without_memory_package(tmp_path):
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
            "sys.modules['nooa_memory'] = None\n"
            "from fastapi.testclient import TestClient\n"
            "from nooa.viewer.main import app\n"
            "with TestClient(app) as client:\n"
            "    assert client.get('/api/memory/dbs').status_code == 200\n"
            "    for route in ('records', 'record', 'stats', 'explain'):\n"
            "        response = client.get('/api/memory/' + route,\n"
            "            params={'db': 'missing.sqlite', 'id': 'test', 'q': 'test'})\n"
            "        assert response.status_code == 503, response.text\n"
            "        assert 'nooa-memory' in response.json()['detail']\n",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
