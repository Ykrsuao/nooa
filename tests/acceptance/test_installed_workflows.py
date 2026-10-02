# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline CLI, viewer, coding, memory, and bench workflows from installed wheels."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AgentMessageChunk, McpServerStdio, ToolCallProgress, ToolCallStart
from acp.transports import default_environment

FIXTURES = Path(__file__).parent / "fixtures"
UNICODE_TEXT = "\u4e2d\u6587\u9a8c\u6536"
FILE_NAME = "\u4ee3\u7801 file.py"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "\u4e2d\u6587 workspace"
    path.mkdir()
    return path


@pytest.fixture
def child_env(workspace: Path) -> dict[str, str]:
    # Never inherit credentials, PYTHONPATH, or the user's NOOA configuration.
    env = default_environment()
    env.update(
        NEMO_OO_USER_DIR=str(workspace / "user-config"),
        NEMO_OO_PROJECT_DIR=str(workspace / ".nooa"),
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        OTLP_ENDPOINT="http://127.0.0.1:9/v1/traces",
    )
    for name in ("NOOA_BASH", "PYTHONPYCACHEPREFIX"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def test_imports_are_from_installed_wheels() -> None:
    installed = os.environ.get("NOOA_SMOKE_INSTALL_ROOT")
    if installed is None:
        pytest.skip("installation provenance is checked by scripts/smoke_install.py")
    root = Path(installed).resolve()
    for name in (
        "nooa",
        "nooa_cli",
        "nooa_acp",
        "nooa_memory",
        "nooa_bench",
        "nooa.runtime.sandbox.windows",
        "nooa.runtime.sandbox._windows_capabilities",
    ):
        module = importlib.import_module(name)
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(root), module.__file__


@pytest.mark.parametrize(
    ("entrypoint", "args", "expected"),
    [
        ("nooa", ["--help"], "start-dev"),
        ("nooa", ["acp", "--help"], "ACP"),
        ("nooa-acp", ["--help"], "ACP"),
        ("nemo-harbor", ["--help"], "--working-dir"),
    ],
)
def test_console_entrypoints(entrypoint, args, expected, workspace, child_env):
    executable = Path(sys.executable).parent / (entrypoint + (".exe" if os.name == "nt" else ""))
    result = subprocess.run(
        [str(executable), *args],
        cwd=workspace,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout


def test_bench_help_does_not_import_agent_runtime(workspace, child_env):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from nooa_bench.runner import main\n"
            "try:\n"
            "    main(args=['--help'])\n"
            "except SystemExit as exc:\n"
            "    assert exc.code == 0\n"
            "assert 'nooa' not in sys.modules\n"
            "assert 'litellm' not in sys.modules\n",
        ],
        cwd=workspace,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--working-dir" in result.stdout


def test_doctor_smoke_is_offline_and_does_not_create_user_config(
    workspace,
    child_env,
    unused_tcp_port,
):
    before = set(workspace.iterdir())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nooa_cli",
            "doctor",
            "--json",
            "--smoke",
            "--workspace",
            str(workspace),
            "--port",
            str(unused_tcp_port),
        ],
        cwd=workspace,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["ok"] is True
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["smoke"]["status"] == "ok"
    assert checks["bash"]["status"] == "ok"
    if sys.platform == "win32":
        assert checks["sandbox"]["status"] == "warning"
        assert checks["windows_sandbox"]["status"] == "ok"
        assert "WindowsSandboxSession" in checks["windows_sandbox"]["message"]
        assert "containment is not verified" in checks["windows_sandbox"]["message"]
    assert set(workspace.iterdir()) == before


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        url = values.get("src" if tag == "script" else "href")
        if tag in {"script", "link"} and url and url.startswith("/assets/"):
            self.urls.append(url)


def test_viewer_start_assets_and_stop(workspace, child_env, unused_tcp_port):
    db = workspace / "\u8f68\u8ff9 data.db"
    log = workspace / "viewer.log"
    stop_file = workspace / "stop-viewer"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                str(FIXTURES / "viewer_process.py"),
                str(stop_file),
                "start-dev",
                "--host",
                "127.0.0.1",
                "--port",
                str(unused_tcp_port),
                "--db",
                str(db),
            ],
            cwd=workspace,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{unused_tcp_port}",
                trust_env=False,
                timeout=2,
            ) as client:
                deadline = time.monotonic() + 30
                while True:
                    assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")
                    try:
                        health = client.get("/api/eval/health")
                        if health.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    time.sleep(0.1)
                assert health.json()["status"] == "healthy"
                config = client.get("/api/config")
                config.raise_for_status()
                assert Path(config.json()["db_path"]).resolve() == db.resolve()
                page = client.get("/")
                page.raise_for_status()
                assert 'id="root"' in page.text
                assets = _Assets()
                assets.feed(page.text)
                assert any(url.endswith(".js") for url in assets.urls)
                assert any(url.endswith(".css") for url in assets.urls)
                for url in assets.urls:
                    asset = client.get(url)
                    asset.raise_for_status()
                    assert len(asset.content) > 100
                    assert "text/html" not in asset.headers["content-type"]
            # The test-only launcher translates a file signal to SIGINT on the main
            # thread, exercising uvicorn shutdown even on headless Windows.
            stop_file.touch()
            process.communicate(timeout=15)
            assert process.returncode in (0, -2, 130), log.read_text(
                encoding="utf-8", errors="replace"
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    assert "Shutdown complete" in log.read_text(encoding="utf-8", errors="replace")
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", unused_tcp_port)) != 0
    # Windows refuses this rename if the exited server retained an open DB handle.
    assert db.is_file()
    db.rename(workspace / "closed.db")


class _Client:
    def __init__(self) -> None:
        self.updates: list[object] = []

    async def session_update(self, session_id: str, update: object, **kwargs) -> None:
        self.updates.append(update)


async def test_coding_edit_mcp_cancel_and_recover(workspace, child_env):
    client = _Client()
    server = workspace / "\u5de5\u5177 server.py"
    shutil.copyfile(FIXTURES / "mcp_server.py", server)
    log = workspace / "agent.log"
    with log.open("wb") as errors:
        async with spawn_agent_process(
            client,  # type: ignore[arg-type]
            sys.executable,
            str(FIXTURES / "coding_agent.py"),
            cwd=workspace,
            env=child_env,
            transport_kwargs={"stderr": errors, "shutdown_timeout": 10},
        ) as (connection, process):
            await asyncio.wait_for(connection.initialize(PROTOCOL_VERSION), timeout=30)
            session = await asyncio.wait_for(
                connection.new_session(
                    str(workspace),
                    mcp_servers=[
                        McpServerStdio(
                            name="localcheck",
                            command=sys.executable,
                            args=[str(server)],
                            env=[],
                        )
                    ],
                ),
                timeout=30,
            )
            first = asyncio.create_task(
                connection.prompt(session.session_id, [text_block("start the long command")])
            )
            try:
                # ToolCallStart precedes shell startup. Wait for a marker from
                # the actual command so cancellation cannot pass vacuously.
                async with asyncio.timeout(30):
                    while not (workspace / "command-started").exists():
                        if first.done():
                            pytest.fail(f"Command never started: {await first}\n{client.updates}")
                        await asyncio.sleep(0.05)
                await connection.cancel(session.session_id)
                assert (await asyncio.wait_for(first, timeout=15)).stop_reason == "cancelled"
                response = await asyncio.wait_for(
                    connection.prompt(
                        session.session_id, [text_block("edit, verify, and call MCP")]
                    ),
                    timeout=30,
                )
                assert response.stop_reason == "end_turn", client.updates
                assert (workspace / FILE_NAME).read_text(
                    encoding="utf-8"
                ) == f"print('{UNICODE_TEXT}')\n"
                assert (workspace / "mcp-result.txt").read_text(encoding="utf-8") == UNICODE_TEXT
                assert any(
                    isinstance(update, AgentMessageChunk) and update.content.text == UNICODE_TEXT
                    for update in client.updates
                ), client.updates
                started = [update for update in client.updates if isinstance(update, ToolCallStart)]
                finished = [
                    update for update in client.updates if isinstance(update, ToolCallProgress)
                ]
                assert any(update.kind == "edit" for update in started)
                assert any(update.kind == "execute" for update in started)
                assert any(update.status == "failed" for update in finished)
                assert not (workspace / "command-finished").exists()
                await connection.close_session(session.session_id)
            finally:
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        assert process.returncode == 0, log.read_text(encoding="utf-8", errors="replace")


def test_memory_skill_persists_across_processes_and_releases_database(workspace, child_env):
    db = workspace / "\u8bb0\u5fc6 data" / "memory.sqlite"
    reports = []
    for phase in ("write", "update", "read"):
        result = subprocess.run(
            [sys.executable, str(FIXTURES / "memory_process.py"), str(db), phase],
            cwd=workspace,
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(json.loads(result.stdout))
    assert len({report["id"] for report in reports}) == 1
    assert [report["count"] for report in reports] == [2, 2, 2]
    assert reports[0]["content"] != reports[1]["content"]
    assert reports[1]["content"] == reports[2]["content"]
    assert all(report["closed"] for report in reports)
    assert not Path(child_env["NEMO_OO_USER_DIR"]).exists()


@pytest.mark.parametrize("agent_type", ["bench", "rlm"])
def test_bench_executes_and_exports_offline(agent_type, workspace, child_env):
    output = workspace / "\u8bc4\u6d4b output"
    result = subprocess.run(
        [
            sys.executable,
            str(FIXTURES / "bench_process.py"),
            str(output),
            "--instruction",
            "Edit and verify the Unicode file.",
            "--model",
            "acceptance-offline",
            "--agent-type",
            agent_type,
            "--working-dir",
            str(workspace),
        ],
        cwd=workspace,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / FILE_NAME).read_text(encoding="utf-8") == f"print('{UNICODE_TEXT}')\n"
    metadata = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert metadata["success"] is True
    assert metadata["agent_type"] == agent_type
    assert metadata["model"] == "acceptance-offline"
    assert metadata["response"] == (output / "answer.txt").read_text(encoding="utf-8")
    assert FILE_NAME in metadata["response"]
    events = json.loads((output / "trajectory.json").read_text(encoding="utf-8"))
    assert any(event["event_type"] == "PythonOutput" for event in events)
    assert any(UNICODE_TEXT in json.dumps(event, ensure_ascii=False) for event in events)
    behavior = json.loads((output / "behavior.json").read_text(encoding="utf-8"))
    assert behavior["signals"]["python_cells"] == 2
    assert behavior["rates"]["completion_rate"] == 1.0
    assert json.loads((output / "lifecycle.json").read_text(encoding="utf-8")) == {
        "calls": 2,
        "closes": 1,
        "shell_stopped": True,
    }
    traces = list((output / "traces").rglob("*.jsonl"))
    assert traces, list(output.rglob("*"))
    records = [
        json.loads(line)
        for trace in traces
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spans = [
        span
        for record in records
        for resource in record["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert spans
    log = (output / "nooa_bench.log").read_text(encoding="utf-8")
    assert "Agent completed successfully" in log
    assert str(output) in log
    assert "--- Logging error ---" not in result.stderr
