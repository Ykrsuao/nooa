# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native workspace grants through stdlib, persistent cells and real Agent calls."""

from __future__ import annotations

import json
import sys

import pytest

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.events import PythonOutput, ResultStatus
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac import _LpacExecutor
from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
from nooa.runtime.sandbox._lpac_runtime import stage_framework
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC"),
    pytest.mark.timeout(180),
]


@pytest.fixture(scope="module", params=["read", "read_write"])
def workspace_runtime(request):
    with _AppContainerPython(
        workspace_access=request.param, inputs={"input.txt": b"snapshot"}
    ) as runtime:
        yield runtime
    assert not runtime.root.exists()
    assert not runtime._profile._created


@pytest.fixture(scope="module")
def workspace_framework(workspace_runtime):
    stage_framework(workspace_runtime)
    return workspace_runtime


def _json(runtime, code):
    result = runtime.run(code)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


@pytest.mark.parametrize("access", [None, False, True, 0, 1, "", "readonly", "READ", [], {}])
def test_invalid_workspace_access_fails_before_provisioning(access, monkeypatch, tmp_path):
    from nooa.runtime.sandbox import _appcontainer, _lpac_recovery, _win_appcontainer

    def unexpected(*args, **kwargs):
        pytest.fail("invalid workspace policy must not provision resources")

    monkeypatch.setattr(_appcontainer.tempfile, "mkdtemp", unexpected)
    monkeypatch.setattr(_lpac_recovery._RuntimeLease, "create", unexpected)
    monkeypatch.setattr(_win_appcontainer, "Profile", unexpected)
    for recovery_directory in (None, tmp_path / "ledger"):
        with pytest.raises(ValueError, match="workspace_access"):
            _AppContainerPython(workspace_access=access, recovery_directory=recovery_directory)


def test_workspace_mode_is_creation_time_only(workspace_runtime):
    with pytest.raises(AttributeError):
        workspace_runtime.workspace_access = "read_write"


_MUTATIONS = {
    "create": "Path('new.txt').write_bytes(b'changed')",
    "truncate": "target.write_bytes(b'changed')",
    "append": "with target.open('ab') as stream: stream.write(b'changed')",
    "mkdir": "Path('new-dir').mkdir()",
    "rename": "target.rename('renamed.txt')",
    "delete": "target.unlink()",
    "nested-create": "Path('nested/new.txt').write_bytes(b'changed')",
    "nested-write": "Path('nested/existing.txt').write_bytes(b'changed')",
    "alternate-stream": "Path('existing.txt:extra').write_bytes(b'changed')",
}


@pytest.mark.parametrize("operation", _MUTATIONS)
def test_workspace_mutations_follow_native_grant(workspace_runtime, operation):
    root = workspace_runtime.workspace / operation
    root.mkdir()
    (root / "existing.txt").write_bytes(b"original")
    (root / "nested").mkdir()
    (root / "nested/existing.txt").write_bytes(b"nested")
    result = _json(
        workspace_runtime,
        "import json, os\nfrom pathlib import Path\n"
        f"os.chdir({str(root)!r})\n"
        "target = Path('existing.txt')\n"
        "assert target.read_bytes() == b'original'\n"
        "assert Path('nested/existing.txt').read_bytes() == b'nested'\n"
        "assert sorted(p.name for p in Path('.').iterdir()) == ['existing.txt', 'nested']\n"
        "try:\n"
        f"    {_MUTATIONS[operation]}\n"
        "except PermissionError as exc:\n"
        "    result = exc.errno\n"
        "else:\n"
        "    result = 'allowed'\n"
        "print(json.dumps(result))",
    )
    assert result == ("allowed" if workspace_runtime.workspace_access == "read_write" else 13)
    if workspace_runtime.workspace_access == "read":
        assert (root / "existing.txt").read_bytes() == b"original"
        assert (root / "nested/existing.txt").read_bytes() == b"nested"
        assert sorted(p.name for p in root.iterdir()) == ["existing.txt", "nested"]
        assert not (root / "existing.txt:extra").exists()


def test_temporary_files_use_the_same_workspace_grant(workspace_runtime):
    # CPython on Windows retries ACL denials as filename collisions when
    # os.access() reports a writable directory. Bound those retries in this probe.
    result = _json(
        workspace_runtime,
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "for key in ['TMP', 'TEMP', 'LOCALAPPDATA']:\n"
        "    assert Path(os.environ[key]).is_relative_to(Path.cwd())\n"
        "tempfile.TMP_MAX = 1\n"
        "try:\n"
        "    with tempfile.TemporaryFile(dir=Path.cwd()) as stream:\n"
        "        stream.write(b'temporary')\n"
        "except (PermissionError, FileExistsError) as exc:\n"
        "    result = exc.errno\n"
        "else:\n"
        "    result = 'allowed'\n"
        "print(json.dumps(result))",
    )
    if workspace_runtime.workspace_access == "read_write":
        assert result == "allowed"
    else:
        assert result in (13, 17)  # EACCES, or exhausted permission-denied retries.


def test_redirected_temp_directories_follow_workspace_grant(workspace_runtime):
    result = _json(
        workspace_runtime,
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "directory = Path(os.environ['TMP'])\n"
        "assert directory.is_relative_to(Path.cwd())\n"
        "tempfile.TMP_MAX = 1\n"
        "try:\n"
        "    directory.mkdir(parents=True, exist_ok=True)\n"
        "    with tempfile.TemporaryFile(dir=directory) as stream:\n"
        "        stream.write(b'temporary')\n"
        "except PermissionError as exc:\n"
        "    result = exc.errno\n"
        "else:\n"
        "    result = 'allowed'\n"
        "print(json.dumps(result))",
    )
    assert result == ("allowed" if workspace_runtime.workspace_access == "read_write" else 13)


def test_workspace_grant_does_not_allow_acl_or_owner_changes(workspace_runtime):
    result = _json(
        workspace_runtime,
        "import ctypes as c, json, os\nfrom ctypes import wintypes as w\n"
        "k = c.WinDLL('kernel32', use_last_error=True)\n"
        "k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]\n"
        "k.CreateFileW.restype = w.HANDLE\n"
        "k.CloseHandle.argtypes = [w.HANDLE]\n"
        "results = []\n"
        "for access in [0x40000, 0x80000]:\n"
        "    handle = k.CreateFileW(os.getcwd(), access, 7, None, 3, 0x02000000, None)\n"
        "    if handle == w.HANDLE(-1).value:\n"
        "        results.append(c.get_last_error())\n"
        "    else:\n"
        "        results.append('allowed')\n"
        "        k.CloseHandle(handle)\n"
        "print(json.dumps(results))",
    )
    assert result == [5, 5]


@pytest.mark.parametrize("access", ["read", "read_write"])
def test_workspace_acl_failure_cleans_resources_without_staging(access, monkeypatch):
    from nooa.runtime.sandbox import _appcontainer, _win_appcontainer

    grant = _win_appcontainer.Profile.grant_owned_directory
    owned = []

    def fail_workspace(self, root, path, **kwargs):
        if path.name == "workspace":
            owned.append((self, root))
            raise OSError("workspace ACL failed")
        return grant(self, root, path, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("failed ACL must not proceed to staging")

    monkeypatch.setattr(_win_appcontainer.Profile, "grant_owned_directory", fail_workspace)
    monkeypatch.setattr(_appcontainer, "_copy_stdlib", unexpected)
    with pytest.raises(OSError, match="workspace ACL failed"):
        _AppContainerPython(workspace_access=access)
    assert len(owned) == 1
    profile, root = owned[0]
    assert not profile._created and not root.exists()


async def test_worker_replacement_preserves_workspace_permissions(workspace_framework, tmp_path):
    runtime = workspace_framework
    (runtime.workspace / "worker-input.txt").write_bytes(b"parent data")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    executor = _LpacExecutor(runtime, startup_timeout_s=60)
    try:
        first_pid = None
        for attempt in range(2):
            result = await executor.run_cell("import os\nos.getpid()")
            assert result.success, result.error
            if first_pid is not None:
                assert result.returned_value != first_pid
            first_pid = result.returned_value
            result = await executor.run_cell(
                "from pathlib import Path\nPath('worker-input.txt').read_bytes()"
            )
            assert result.success and result.returned_value == b"parent data"
            result = await executor.run_cell(
                f"Path('worker-output-{attempt}.txt').write_bytes(b'worker')"
            )
            if runtime.workspace_access == "read_write":
                assert result.success and result.returned_value == 6
            else:
                assert not result.success and "PermissionError" in str(result.error)
                assert not (runtime.workspace / f"worker-output-{attempt}.txt").exists()
            for code in (
                f"open({str(secret)!r}, 'rb').read()",
                f"open({str(runtime.inputs / 'input.txt')!r}, 'wb')",
                "__import__('socket').socket()",
            ):
                denied = await executor.run_cell(code)
                assert not denied.success and "PermissionError" in str(denied.error)
            if attempt == 0:
                died = await executor.run_cell("os._exit(7)")
                assert not died.success
    finally:
        await executor.aclose()
    assert executor._proc is None and executor._conn is None and not executor._processes


async def test_real_agent_obeys_workspace_grant(workspace_framework):
    runtime = workspace_framework
    backend = _LpacCodeActStrategy(runtime, config=CodeActConfig(cell_timeout=10))
    cells = [
        "from pathlib import Path\nPath('agent-output.txt').write_bytes(b'agent')",
        "return_result(42)",
    ]
    llm = FakeLLMClient(
        scripted_responses=[
            LLMResponse(
                raw_response=None,
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id=f"c{i}", name="execute_python", arguments=json.dumps({"code": code})
                    )
                ],
            )
            for i, code in enumerate(cells)
        ]
    )

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> int:
            """Exercise the private workspace."""
            ...

    agent = Demo(llm=llm)
    assert await agent.compute() == 42
    errors = [
        e
        for e in agent.event_manager.values()
        if isinstance(e, PythonOutput) and e.execution_status is ResultStatus.ERROR
    ]
    if runtime.workspace_access == "read_write":
        assert not errors
        assert (runtime.workspace / "agent-output.txt").read_bytes() == b"agent"
    else:
        assert len(errors) == 1 and "PermissionError" in errors[0].error
        assert not (runtime.workspace / "agent-output.txt").exists()


def test_readonly_workspace_with_recovery_ledger(tmp_path):
    store = tmp_path / "ledger"
    with _AppContainerPython(workspace_access="read", recovery_directory=store) as runtime:
        report = _AppContainerPython.recover_orphans(store)
        assert report.active == [runtime.root.parent.name] and not report.errors
        assert _json(runtime, "print('42')") == 42
    assert not runtime.root.parent.exists()
    assert not _AppContainerPython.recover_orphans(store).recovered
