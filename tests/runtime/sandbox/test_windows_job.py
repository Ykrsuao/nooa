# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native kernel resource limits, not filesystem/network sandbox containment."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("Windows Job Objects", allow_module_level=True)

from nooa import _win_job  # noqa: E402
from nooa._win_job import ProcessJob  # noqa: E402

MIB = 1024 * 1024
# The venv executable is a redirector that launches another process on Windows.
PYTHON = sys._base_executable


@pytest.fixture
def child_env(tmp_path):
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP", "COMSPEC"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        PYTHON_DOTENV_DISABLED="1",
        NEMO_OO_USER_DIR=str(tmp_path / "user-config"),
        NEMO_OO_PROJECT_DIR=str(tmp_path / "project-config"),
    )
    return env


def _run(code, limits, child_env, tmp_path, *, timeout=15):
    job = ProcessJob(**limits)
    # This is trusted bootstrap code. No tested workload runs before assignment.
    process = subprocess.Popen(
        [PYTHON, "-I", "-S", "-c", "input()\n" + code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        cwd=tmp_path,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.pid != os.getpid()
        job.assign(process.pid)
        stdout, stderr = process.communicate("\n", timeout=timeout)
        return process.returncode, stdout, stderr
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("limited", [False, True])
def test_memory_budget_is_enforced_by_the_kernel(limited, child_env, tmp_path):
    code = (
        "try:\n"
        "    value = bytearray(96 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('allocation-denied')\n"
        "else:\n"
        "    print('allocated', len(value))\n"
    )
    limits = {"memory_limit_bytes": 64 * MIB} if limited else {}
    status, stdout, stderr = _run(code, limits, child_env, tmp_path)
    assert status == 0, stderr
    assert ("allocation-denied" if limited else "allocated 100663296") in stdout


@pytest.mark.parametrize("limited", [False, True])
def test_memory_budget_covers_the_whole_process_tree(limited, child_env, tmp_path):
    child = (
        "try:\n"
        "    value = bytearray(40 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('allocation-denied')\n"
        "else:\n"
        "    print('allocated')\n"
    )
    code = (
        "import subprocess, sys\n"
        "value = bytearray(40 * 1024 * 1024)\n"
        f"child = subprocess.run([sys.executable, '-I', '-S', '-c', {child!r}], "
        "capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "assert child.returncode == 0, child.stderr\n"
        "print(child.stdout.strip())\n"
    )
    status, stdout, stderr = _run(
        code, {"memory_limit_bytes": 64 * MIB} if limited else {}, child_env, tmp_path
    )
    assert status == 0, stderr
    assert stdout.strip() == ("allocation-denied" if limited else "allocated")


@pytest.mark.parametrize("limited", [False, True])
def test_process_count_limit_prevents_children(limited, child_env, tmp_path):
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    child = subprocess.run([sys.executable, '-I', '-S', '-c', \"print('child-ran')\"], "
        "capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "except OSError:\n"
        "    print('creation-denied')\n"
        "else:\n"
        "    print(child.stdout.strip() if child.returncode == 0 else 'creation-denied')\n"
    )
    limits = {"active_process_limit": 1} if limited else {}
    status, stdout, stderr = _run(code, limits, child_env, tmp_path)
    assert status == 0, stderr
    assert stdout.strip() == ("creation-denied" if limited else "child-ran")


def test_descendants_cannot_request_breakaway_from_the_job(child_env, tmp_path):
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    child = subprocess.run([sys.executable, '-I', '-S', '-c', \"print('escaped')\"], "
        "capture_output=True, text=True, timeout=5, "
        "creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB)\n"
        "except OSError:\n"
        "    print('breakaway-denied')\n"
        "else:\n"
        "    print(child.stdout.strip())\n"
    )
    status, stdout, stderr = _run(code, {}, child_env, tmp_path)
    assert status == 0, stderr
    assert stdout.strip() == "breakaway-denied"


def test_cpu_limit_terminates_runaway_worker(child_env, tmp_path):
    status, stdout, _ = _run(
        "print('workload-started', flush=True)\nwhile True:\n    pass\n",
        {"cpu_time_limit_s": 1},
        child_env,
        tmp_path,
    )
    assert "workload-started" in stdout
    assert status != 0


def test_close_kills_limited_worker_and_descendant(child_env, tmp_path):
    from nooa._win_job import _image_name

    child_code = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        "input()\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-S', '-c', {child_code!r}], "
        "creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    job = ProcessJob(memory_limit_bytes=128 * MIB, cpu_time_limit_s=10, active_process_limit=2)
    root = subprocess.Popen(
        [PYTHON, "-I", "-S", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        cwd=tmp_path,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        job.assign(root.pid)
        assert root.stdin is not None and root.stdout is not None
        root.stdin.write("\n")
        root.stdin.flush()
        child = int(root.stdout.readline())
        assert child in job.pids()
        job.close()
        job.close()
        assert root.wait(timeout=5) is not None
        deadline = time.monotonic() + 5
        while _image_name(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _image_name(child)
    finally:
        job.close()
        if root.poll() is None:
            root.kill()
        root.communicate(timeout=5)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory_limit_bytes", -1),
        ("memory_limit_bytes", 1 << (8 * ctypes.sizeof(ctypes.c_size_t))),
        ("cpu_time_limit_s", -1),
        ("cpu_time_limit_s", (1 << 63) // 10_000_000 + 1),
        ("active_process_limit", -1),
        ("active_process_limit", 1 << 32),
        ("memory_limit_bytes", 1.5),
        ("cpu_time_limit_s", True),
    ],
)
def test_invalid_limits_fail_before_creating_a_job(name, value, monkeypatch):
    def unexpected_create(*args):
        pytest.fail("Invalid limits must not allocate a kernel handle")

    monkeypatch.setattr(_win_job, "_CreateJobObjectW", unexpected_create)
    with pytest.raises(ValueError, match=name):
        ProcessJob(**{name: value})


def test_assign_after_close_fails_explicitly():
    job = ProcessJob()
    job.close()
    with pytest.raises(RuntimeError, match="closed"):
        job.assign(0)


def test_creation_handle_is_borrowed_and_refuses_closed_jobs():
    job = ProcessJob()
    try:
        assert job._creation_handle() == job._handle
    finally:
        job.close()
    with pytest.raises(RuntimeError, match="closed job"):
        job._creation_handle()


def test_owner_exit_closes_job_even_without_python_cleanup(child_env, tmp_path):
    pid_file = tmp_path / "owned-process.json"
    code = (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from nooa._win_job import ProcessJob\n"
        "job = ProcessJob(memory_limit_bytes=128 * 1024 * 1024, active_process_limit=1)\n"
        "child = subprocess.Popen([sys._base_executable, '-I', '-S', '-c', 'import time; time.sleep(60)'], "
        "creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "try:\n"
        "    job.assign(child.pid)\n"
        f"    Path({str(pid_file)!r}).write_text(json.dumps(child.pid), encoding='utf-8')\n"
        "except BaseException:\n"
        "    child.kill()\n"
        "    child.wait()\n"
        "    raise\n"
        "os._exit(0)\n"
    )
    from nooa._win_job import _image_name

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=child_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    child = json.loads(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _image_name(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _image_name(child)


def test_shell_uses_the_shared_job_implementation():
    from nooa.tools import _win_bash

    assert _win_bash.ProcessJob is ProcessJob


def test_failed_limit_installation_closes_the_job_handle(monkeypatch):
    created = []
    closed = []
    create = _win_job._CreateJobObjectW
    close = _win_job._CloseHandle

    def record_create(*args):
        handle = create(*args)
        created.append(handle)
        return handle

    def record_close(handle):
        closed.append(handle)
        return close(handle)

    def fail_limits(*args):
        ctypes.set_last_error(5)
        return False

    monkeypatch.setattr(_win_job, "_CreateJobObjectW", record_create)
    monkeypatch.setattr(_win_job, "_CloseHandle", record_close)
    monkeypatch.setattr(_win_job, "_SetInformationJobObject", fail_limits)
    with pytest.raises(OSError):
        ProcessJob(memory_limit_bytes=64 * MIB)
    assert len(created) == 1
    assert closed == created


def test_job_handle_is_not_inheritable():
    get_info = _win_job._kernel32.GetHandleInformation
    get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    get_info.restype = ctypes.c_int
    job = ProcessJob()
    try:
        flags = ctypes.c_ulong()
        assert get_info(job._handle, ctypes.byref(flags))
        assert flags.value & 1 == 0  # HANDLE_FLAG_INHERIT
    finally:
        job.close()


def test_failed_assignment_closes_the_temporary_process_handle(monkeypatch, child_env, tmp_path):
    opened = []
    closed = []
    open_process = _win_job._OpenProcess
    close = _win_job._CloseHandle

    def record_open(*args):
        handle = open_process(*args)
        opened.append(handle)
        return handle

    def record_close(handle):
        closed.append(handle)
        return close(handle)

    def fail_assign(*args):
        ctypes.set_last_error(5)
        return False

    job = ProcessJob()
    child = subprocess.Popen(
        [PYTHON, "-I", "-S", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
        env=child_env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        monkeypatch.setattr(_win_job, "_OpenProcess", record_open)
        monkeypatch.setattr(_win_job, "_CloseHandle", record_close)
        monkeypatch.setattr(_win_job, "_AssignProcessToJobObject", fail_assign)
        with pytest.raises(OSError):
            job.assign(child.pid)
        assert len(opened) == 1 and opened[0]
        assert closed == opened
        assert job.pids() == []
    finally:
        job.close()
        child.kill()
        child.wait(timeout=5)
