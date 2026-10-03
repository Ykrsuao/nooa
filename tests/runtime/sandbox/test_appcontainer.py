# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native LPAC negative tests. Not the fork-only sandbox marker or public backend."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from nooa.runtime.sandbox._appcontainer import _AppContainerPython

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC")


@pytest.fixture(scope="module")
def lpac():
    with _AppContainerPython(inputs={"sample.txt": b"allowed snapshot"}) as instance:
        yield instance


def _json(lpac, source, **kwargs):
    result = lpac.run(source, **kwargs)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_private_python_boots_and_imports_standard_library(lpac):
    result = _json(
        lpac,
        "import json, sys, os, ctypes, socket, sqlite3\n"
        "print(json.dumps({'exe': sys.executable, 'prefix': sys.prefix, "
        "'path': sys.path, 'cwd': os.getcwd(), 'version': list(sys.version_info[:2])}))",
    )
    assert result["exe"] == str(lpac.runtime / "pythonw.exe")
    assert result["prefix"] == str(lpac.runtime)
    assert result["cwd"] == str(lpac.workspace)
    assert result["version"] == list(sys.version_info[:2])
    assert all(path.startswith(str(lpac.runtime)) for path in result["path"])


def test_stdio_is_usable_without_inheriting_environment(lpac, monkeypatch):
    monkeypatch.setenv("NOOA_LPAC_TEST_SECRET", "do-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "do-not-inherit")
    result = _json(
        lpac,
        "import json, os, sys\n"
        "print(json.dumps({'input': sys.stdin.buffer.read().decode('utf-8'), 'env': dict(os.environ)}))",
        input_bytes=b"parent message",
    )
    assert result["input"] == "parent message"
    assert "NOOA_LPAC_TEST_SECRET" not in result["env"]
    assert "PYTHONPATH" not in result["env"]
    assert "USERPROFILE" not in result["env"]
    assert "PATH" not in result["env"]


def test_token_is_lpac_with_only_registry_read_capability(lpac):
    from nooa.runtime.sandbox._win_appcontainer import _sid_text

    result = _json(
        lpac,
        "import ctypes as c, json\n"
        "from ctypes import wintypes as w\n"
        "a = c.WinDLL('advapi32', use_last_error=True)\n"
        "a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]\n"
        "a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]\n"
        "token = w.HANDLE()\n"
        "assert a.OpenProcessToken(w.HANDLE(-1), 8, c.byref(token))\n"
        "values = {}\n"
        "for name, kind in [('appcontainer', 29)]:\n"
        "    value, size = w.DWORD(), w.DWORD()\n"
        "    assert a.GetTokenInformation(token, kind, c.byref(value), 4, c.byref(size)), c.get_last_error()\n"
        "    values[name] = value.value\n"
        "size = w.DWORD()\n"
        "a.GetTokenInformation(token, 30, None, 0, c.byref(size))\n"
        "buffer = c.create_string_buffer(size.value)\n"
        "assert a.GetTokenInformation(token, 30, buffer, size.value, c.byref(size))\n"
        "values['capabilities'] = w.DWORD.from_buffer(buffer).value\n"
        "class Entry(c.Structure):\n"
        "    _fields_ = [('sid', c.c_void_p), ('attributes', w.DWORD)]\n"
        "class Groups(c.Structure):\n"
        "    _fields_ = [('count', w.DWORD), ('entries', Entry * 1)]\n"
        "sid = Groups.from_buffer(buffer).entries[0].sid\n"
        "a.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]\n"
        "text = w.LPWSTR()\n"
        "assert a.ConvertSidToStringSidW(sid, c.byref(text))\n"
        "values['capability_sid'] = text.value\n"
        "print(json.dumps(values))",
    )
    assert result == {
        "appcontainer": 1,
        "capabilities": 1,
        "capability_sid": _sid_text(lpac._profile.registry_read_sid),
    }


def test_readonly_inputs_and_runtime_but_writable_workspace(lpac):
    result = _json(
        lpac,
        "import json\n"
        "from pathlib import Path\n"
        f"source = Path({str(lpac.inputs / 'sample.txt')!r})\n"
        f"runtime = Path({str(lpac.runtime / 'pythonw.exe')!r})\n"
        "result = {'read': source.read_text(encoding='utf-8')}\n"
        "for name, path in [('input_write', source), ('runtime_write', runtime)]:\n"
        "    try:\n"
        "        with path.open('ab') as stream:\n"
        "            pass\n"
        "    except PermissionError as exc:\n"
        "        result[name] = exc.errno\n"
        "    else:\n"
        "        result[name] = 'allowed'\n"
        "Path('result.txt').write_text('workspace', encoding='utf-8')\n"
        "result['write'] = Path('result.txt').read_text(encoding='utf-8')\n"
        "print(json.dumps(result))",
    )
    assert result == {
        "read": "allowed snapshot",
        "input_write": 13,
        "runtime_write": 13,
        "write": "workspace",
    }


def test_ungranted_file_read_and_write_are_denied(lpac, tmp_path):
    secret = tmp_path / "synthetic-secret.txt"
    secret.write_text("synthetic canary", encoding="utf-8")
    result = _json(
        lpac,
        "import json\n"
        "errors = []\n"
        "for mode in ['rb', 'wb']:\n"
        "    try:\n"
        f"        with open({str(secret)!r}, mode):\n"
        "            errors.append('allowed')\n"
        "    except PermissionError as exc:\n"
        "        errors.append(exc.errno)\n"
        "print(json.dumps(errors))",
    )
    assert result == [13, 13]
    assert secret.read_text(encoding="utf-8") == "synthetic canary"


@pytest.mark.parametrize("low_privilege", [False, True], ids=["appcontainer-control", "lpac"])
def test_all_application_packages_is_not_a_grant_to_lpac(
    lpac, tmp_path, monkeypatch, low_privilege
):
    import ctypes
    from ctypes import wintypes

    from nooa.runtime.sandbox import _win_appcontainer as native

    # Grant a synthetic directory to ALL APPLICATION PACKAGES, not this profile.
    with monkeypatch.context() as patch:
        patch.setattr(lpac._profile, "sid_text", "S-1-15-2-1")
        lpac._profile.grant_owned_directory(tmp_path, tmp_path)
    canary = tmp_path / "all-packages.txt"
    canary.write_text("broad package grant", encoding="utf-8")
    update = native._UpdateAttribute
    regular_container = wintypes.DWORD(0)

    def set_attribute(*args):
        if not low_privilege and args[2] == 0x2000F:
            args = (*args[:3], ctypes.byref(regular_container), *args[4:])
        return update(*args)

    # The control deliberately removes only LPAC for trusted canary-reading code.
    monkeypatch.setattr(native, "_UpdateAttribute", set_attribute)
    result = _json(
        lpac,
        "import json\n"
        "try:\n"
        f"    with open({str(canary)!r}, encoding='utf-8') as stream:\n"
        "        result = stream.read()\n"
        "except PermissionError as exc:\n"
        "    result = exc.errno\n"
        "print(json.dumps(result))",
    )
    assert result == (13 if low_privilege else "broad package grant")


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6], ids=["ipv4", "ipv6"])
@pytest.mark.parametrize("transport", [socket.SOCK_STREAM, socket.SOCK_DGRAM], ids=["tcp", "udp"])
def test_loopback_network_denied_with_working_host_control(lpac, family, transport):
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    with socket.socket(family, transport) as listener:
        listener.bind((host, 0))
        if transport == socket.SOCK_STREAM:
            listener.listen()
        address = listener.getsockname()
        with socket.socket(family, transport) as control:
            control.settimeout(2)
            control.connect(address)
            if transport == socket.SOCK_DGRAM:
                control.send(b"synthetic")
                listener.settimeout(2)
                assert listener.recv(64) == b"synthetic"
        result = _json(
            lpac,
            "import json, socket\n"
            "try:\n"
            f"    with socket.socket({int(family)}, {int(transport)}) as client:\n"
            "        client.settimeout(2)\n"
            f"        client.connect({address!r})\n"
            "        client.send(b'synthetic')\n"
            "except OSError as exc:\n"
            "    result = exc.winerror\n"
            "else:\n"
            "    result = 'allowed'\n"
            "print(json.dumps(result))",
        )
        assert result == 10013  # WSAEACCES, not refused/timed-out/unreachable.


def test_registry_read_does_not_expose_private_user_key(lpac):
    import uuid
    import winreg

    name = "Software\\NooaLpacTest-" + uuid.uuid4().hex
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, name) as key:
            winreg.SetValueEx(key, "canary", 0, winreg.REG_SZ, "synthetic secret")
        result = _json(
            lpac,
            "import json, winreg\n"
            "try:\n"
            f"    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, {name!r}) as key:\n"
            "        result = winreg.QueryValueEx(key, 'canary')[0]\n"
            "except OSError as exc:\n"
            "    result = exc.winerror\n"
            "print(json.dumps(result))",
        )
        assert result in (2, 5)  # Virtualized-away or access denied, never the canary.
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)


def test_parent_process_dangerous_access_is_denied(lpac):
    result = _json(
        lpac,
        "import ctypes as c, json\n"
        "from ctypes import wintypes as w\n"
        "k = c.WinDLL('kernel32', use_last_error=True)\n"
        "k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]\n"
        "k.OpenProcess.restype = w.HANDLE\n"
        "values = {}\n"
        "for name, access in [('read', 0x10), ('write', 0x28), ('duplicate', 0x40), "
        "('create_process', 0x80), ('terminate', 1), ('change_acl', 0x40000)]:\n"
        f"    handle = k.OpenProcess(access, False, {os.getpid()})\n"
        "    values[name] = 'allowed' if handle else c.get_last_error()\n"
        "print(json.dumps(values))",
    )
    assert result == dict.fromkeys(
        ["read", "write", "duplicate", "create_process", "terminate", "change_acl"], 5
    )


def test_child_process_creation_is_denied(lpac):
    result = _json(
        lpac,
        "import subprocess, sys, json\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-I', '-S', '-c', 'pass'], check=True)\n"
        "except OSError as exc:\n"
        "    print(json.dumps({'error': exc.winerror}))\n"
        "else:\n"
        "    print(json.dumps({'error': 'allowed'}))",
    )
    assert result["error"] in (5, 367, 1260)  # access denied / child blocked / policy disabled


@pytest.mark.parametrize("descriptor", [1, 2], ids=["stdout", "stderr"])
def test_timeout_and_output_limit_reap_process_and_allow_reuse(lpac, descriptor):
    with pytest.raises(subprocess.TimeoutExpired):
        lpac.run("while True: pass", timeout_s=0.2)
    with pytest.raises(RuntimeError, match="output exceeded"):
        lpac.run(
            f"import os\nwhile True: os.write({descriptor}, b'x' * 8192)",
            max_output_bytes=8192,
        )
    assert _json(lpac, "print('42')") == 42


def test_timeout_unblocks_full_stdin_pipe(lpac):
    with pytest.raises(subprocess.TimeoutExpired):
        lpac.run("while True: pass", input_bytes=b"x" * (2 * 1024 * 1024), timeout_s=0.2)
    assert _json(lpac, "print('42')") == 42


def test_distinct_profiles_cannot_read_each_others_workspace(lpac):
    with _AppContainerPython() as other:
        assert lpac._profile.sid_text != other._profile.sid_text
        for reader, owner in ((lpac, other), (other, lpac)):
            canary = owner.workspace / "private-canary.txt"
            canary.write_bytes(b"private workspace")
            result = _json(
                reader,
                "import json\n"
                "try:\n"
                f"    with open({str(canary)!r}, 'rb') as stream:\n"
                "        result = stream.read().decode()\n"
                "except PermissionError as exc:\n"
                "    result = exc.errno\n"
                "print(json.dumps(result))",
            )
            assert result == 13


@pytest.mark.parametrize(
    "name", ["../escape", "C:\\outside", "data:stream", "NUL", "name.", "COM\u00b9", "con.txt"]
)
def test_invalid_input_paths_fail_before_creating_resources(name):
    with pytest.raises(ValueError, match="plain Windows filenames"):
        _AppContainerPython(inputs={name: b"synthetic"})


def test_case_insensitive_input_aliases_are_rejected():
    with pytest.raises(ValueError, match="filename aliases"):
        _AppContainerPython(inputs={"data.txt": b"one", "DATA.txt": b"two"})


def test_unrelated_inheritable_file_handle_is_not_transferred(lpac, tmp_path):
    import msvcrt

    canary = tmp_path / "handle-canary.txt"
    canary.write_bytes(b"do not inherit")
    with canary.open("rb") as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        os.set_handle_inheritable(handle, True)
        try:
            result = _json(
                lpac,
                "import ctypes as c, json\n"
                "from ctypes import wintypes as w\n"
                "k = c.WinDLL('kernel32', use_last_error=True)\n"
                "k.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]\n"
                "buffer, count = c.create_string_buffer(32), w.DWORD()\n"
                "try:\n"
                f"    ok = k.ReadFile({handle}, buffer, 32, c.byref(count), None)\n"
                "    error = c.get_last_error()\n"
                "except OSError as exc:\n"
                "    ok, error = False, exc.winerror\n"
                "print(json.dumps({'ok': bool(ok), 'error': error}))",
            )
            assert result["ok"] is False
            assert result["error"] in (6, -1073741816)  # ERROR/STATUS_INVALID_HANDLE
        finally:
            os.set_handle_inheritable(handle, False)


def test_parent_token_cannot_be_duplicated(lpac):
    result = _json(
        lpac,
        "import ctypes as c, json\n"
        "from ctypes import wintypes as w\n"
        "k = c.WinDLL('kernel32', use_last_error=True)\n"
        "a = c.WinDLL('advapi32', use_last_error=True)\n"
        "k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]\n"
        "k.OpenProcess.restype = w.HANDLE\n"
        "k.CloseHandle.argtypes = [w.HANDLE]\n"
        "a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]\n"
        f"process = k.OpenProcess(0x1000, False, {os.getpid()})\n"
        "if not process:\n"
        "    result = c.get_last_error()\n"
        "else:\n"
        "    token = w.HANDLE()\n"
        "    ok = a.OpenProcessToken(process, 0x0002, c.byref(token))\n"
        "    result = 'allowed' if ok else c.get_last_error()\n"
        "    if ok: k.CloseHandle(token)\n"
        "    k.CloseHandle(process)\n"
        "print(json.dumps(result))",
    )
    assert result == 5


def test_readonly_input_acl_and_owner_cannot_be_changed(lpac):
    result = _json(
        lpac,
        "import ctypes as c, json\n"
        "from ctypes import wintypes as w\n"
        "k = c.WinDLL('kernel32', use_last_error=True)\n"
        "k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]\n"
        "k.CreateFileW.restype = w.HANDLE\n"
        "k.CloseHandle.argtypes = [w.HANDLE]\n"
        "results = []\n"
        "for rights in [0x40000, 0x80000]:\n"
        f"    handle = k.CreateFileW({str(lpac.inputs / 'sample.txt')!r}, rights, 7, None, 3, 0, None)\n"
        "    if handle == w.HANDLE(-1).value:\n"
        "        results.append(c.get_last_error())\n"
        "    else:\n"
        "        results.append('allowed')\n"
        "        k.CloseHandle(handle)\n"
        "print(json.dumps(results))",
    )
    assert result == [5, 5]


@pytest.mark.parametrize("operation", ["attributes", "create", "job_attribute", "resume"])
def test_startup_failure_never_executes_python_and_releases_handles(lpac, monkeypatch, operation):
    import ctypes

    from nooa._win_job import _image_name
    from nooa.runtime.sandbox import _win_appcontainer as native

    created = []
    start = native.SuspendedProcess.__init__

    def record(self, *args, **kwargs):
        start(self, *args, **kwargs)
        created.append(self.pid)

    def fail_native(*args):
        ctypes.set_last_error(5)
        return False

    update = native._UpdateAttribute

    def fail_job_attribute(*args):
        if args[2] == native._PROC_THREAD_ATTRIBUTE_JOB_LIST:
            return fail_native(*args)
        return update(*args)

    marker = lpac.workspace / "must-not-run.txt"
    with monkeypatch.context() as patch:
        patch.setattr(native.SuspendedProcess, "__init__", record)
        if operation == "attributes":
            patch.setattr(native, "_UpdateAttribute", fail_native)
        elif operation == "create":
            patch.setattr(native, "_CreateProcess", fail_native)
        elif operation == "resume":
            patch.setattr(native, "_ResumeThread", lambda handle: 0xFFFFFFFF)
        else:
            patch.setattr(native, "_UpdateAttribute", fail_job_attribute)
        with pytest.raises(OSError):
            lpac.run("from pathlib import Path\nPath('must-not-run.txt').write_bytes(b'bad')")
    assert not marker.exists()
    assert all(not _image_name(pid) for pid in created)
    assert _json(lpac, "print('42')") == 42


@pytest.mark.parametrize("backend", ["stdlib", "framework"])
def test_job_membership_is_atomic_with_native_creation(lpac, monkeypatch, backend):
    import ctypes

    from nooa._win_job import ProcessJob
    from nooa.runtime.sandbox import _win_appcontainer as native
    from nooa.runtime.sandbox._lpac_process import LpacProcess

    jobs, observed = [], []
    initialize, create = ProcessJob.__init__, native._CreateProcess

    def record_job(self, **kwargs):
        initialize(self, **kwargs)
        jobs.append(self)

    def record_creation(*args):
        result = create(*args)
        if result:
            info = ctypes.cast(args[-1], ctypes.POINTER(native._ProcessInfo)).contents
            observed.append((info.pid, jobs[-1].pids()))
        return result

    def unexpected_assignment(*args):
        pytest.fail("LPAC must not assign a job after creating the process")

    monkeypatch.setattr(ProcessJob, "__init__", record_job)
    monkeypatch.setattr(ProcessJob, "assign", unexpected_assignment)
    monkeypatch.setattr(native, "_CreateProcess", record_creation)
    if backend == "stdlib":
        assert _json(lpac, "print('42')") == 42
    else:
        job = ProcessJob(active_process_limit=1)
        process = None
        try:
            process = LpacProcess(lpac, job, "pass", frame_timeout_s=5)
        finally:
            job.close()
            if process is not None:
                process.close()
                process.close_streams()
    assert len(observed) == 1
    pid, members = observed[0]
    assert members == [pid], "LPAC process escaped job ownership during native creation"


def test_invalid_native_job_fails_without_retry_or_execution(lpac, monkeypatch):
    import ctypes
    from ctypes import wintypes

    from nooa.runtime.sandbox import _win_appcontainer as native

    update, create = native._UpdateAttribute, native._CreateProcess
    # A real but non-job handle: attribute setup may accept it; creation must not.
    invalid_job = (wintypes.HANDLE * 1)(native._GetCurrentProcess())
    attempts = []

    def replace_job(*args):
        if args[2] == native._PROC_THREAD_ATTRIBUTE_JOB_LIST:
            args = (*args[:3], ctypes.byref(invalid_job), ctypes.sizeof(invalid_job), *args[5:])
        return update(*args)

    def record_create(*args):
        attempts.append(True)
        return create(*args)

    def unexpected_resume(*args):
        pytest.fail("An invalid job must not reach process resume")

    monkeypatch.setattr(native, "_UpdateAttribute", replace_job)
    monkeypatch.setattr(native, "_CreateProcess", record_create)
    monkeypatch.setattr(native, "_ResumeThread", unexpected_resume)
    with pytest.raises(OSError):
        lpac.run("raise AssertionError('must not execute')")
    assert len(attempts) == 1


def test_closed_job_is_refused_before_native_creation(lpac, monkeypatch):
    from nooa._win_job import ProcessJob
    from nooa.runtime.sandbox import _win_appcontainer as native
    from nooa.runtime.sandbox._lpac_process import LpacProcess

    def unexpected_create(*args):
        pytest.fail("A closed job must not reach native process creation")

    monkeypatch.setattr(native, "_CreateProcess", unexpected_create)
    job = ProcessJob(active_process_limit=1)
    job.close()
    with pytest.raises(RuntimeError, match="closed job"):
        LpacProcess(lpac, job, "pass", frame_timeout_s=5)


@pytest.mark.parametrize("backend", ["stdlib", "framework"])
@pytest.mark.parametrize("phase", ["created", "running"])
def test_abrupt_owner_exit_reaps_lpac_without_python_cleanup(lpac, tmp_path, backend, phase):
    from nooa import _win_job
    from nooa.runtime.sandbox import _win_appcontainer as native

    pid_file = tmp_path / "worker.pid"
    marker = lpac.workspace / f"owner-exit-{backend}-{phase}"
    workload = (
        f"from pathlib import Path; Path({str(marker)!r}).write_bytes(b'running'); "
        "import time; time.sleep(120)"
    )
    # Borrow this test's profile/runtime; the crashing owner creates no profile
    # or staging directory that would require orphan recovery to clean up.
    source = f"""
import ctypes, os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from nooa._win_job import ProcessJob
from nooa.runtime.sandbox import _win_appcontainer as native
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac_process import LpacProcess
sid = ctypes.c_void_p()
convert = native._fn(native._security, "ConvertStringSidToSidW", ctypes.c_int,
                     ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))
native._check(convert({lpac._profile.sid_text!r}, ctypes.byref(sid)))
owner = _AppContainerPython.__new__(_AppContainerPython)
owner._profile = SimpleNamespace(_created=True, sid=sid,
                                registry_read_sid=native._registry_read_sid(),
                                network_capability_sids=())
owner._lock = threading.RLock()
owner._closed = False
owner.runtime = Path({str(lpac.runtime)!r})
owner.workspace = Path({str(lpac.workspace)!r})
pid = None
def exit_without_cleanup():
    target = Path({str(pid_file)!r})
    temporary = target.with_suffix(".tmp")
    temporary.write_text(str(pid), encoding="ascii")
    temporary.replace(target)
    assert sys.stdin.buffer.read(1) == b"x"
    os._exit(23)
create, resume = native._CreateProcess, native._ResumeThread
def created(*args):
    global pid
    result = create(*args)
    if result:
        pid = ctypes.cast(args[-1], ctypes.POINTER(native._ProcessInfo)).contents.pid
        if {phase!r} == "created":
            exit_without_cleanup()
    return result
def resumed(*args):
    result = resume(*args)
    if result != 0xFFFFFFFF and {phase!r} == "running":
        deadline = time.monotonic() + 15
        while not Path({str(marker)!r}).exists():
            assert time.monotonic() < deadline, "worker never executed"
            time.sleep(0.01)
        exit_without_cleanup()
    return result
native._CreateProcess, native._ResumeThread = created, resumed
if {backend!r} == "stdlib":
    owner.run({workload!r}, timeout_s=60)
else:
    job = ProcessJob(active_process_limit=1)
    process = LpacProcess(owner, job, {workload!r}, frame_timeout_s=5)
raise AssertionError("owner must exit inside the native launch call")
"""
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    handle = None
    try:
        deadline = time.monotonic() + 60
        while not pid_file.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not pid_file.exists():
            child.kill()
            stdout, stderr = child.communicate(timeout=10)
            pytest.fail(f"Owner did not reach {phase}: {stdout!r}, {stderr!r}")
        # Pin the process object before letting the owner die, avoiding PID reuse
        # and retaining a cleanup handle even if the regression reappears.
        pid = int(pid_file.read_text(encoding="ascii"))
        handle = _win_job._OpenProcess(0x100000 | 0x0001, False, pid)
        assert handle
        assert native._Wait(handle, 0) == 258
        assert marker.exists() == (phase == "running")
        stdout, stderr = child.communicate(b"x", timeout=15)
        assert child.returncode == 23, (stdout, stderr)
        assert native._Wait(handle, 5000) == 0, "Owner exit left an orphaned LPAC process"
        if phase == "created":
            assert not marker.exists()
    finally:
        if handle:
            if native._Wait(handle, 0) == 258:
                native._TerminateProcess(handle, 1)
                native._Wait(handle, 5000)
            native.CloseHandle(handle)
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_repeated_runs_do_not_leak_process_or_pipe_handles(lpac):
    import ctypes
    import gc
    from ctypes import wintypes

    from nooa.runtime.sandbox import _win_appcontainer as native

    get_count = native._fn(
        native._kernel,
        "GetProcessHandleCount",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )

    def count():
        gc.collect()
        value = wintypes.DWORD()
        assert get_count(native._GetCurrentProcess(), ctypes.byref(value))
        return value.value

    assert _json(lpac, "print('42')") == 42
    before = count()
    for _ in range(5):
        assert _json(lpac, "print('42')") == 42
    assert count() <= before + 2


def test_profile_delete_failure_can_be_retried(monkeypatch):
    from nooa.runtime.sandbox import _win_appcontainer as native

    profile = native.Profile()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(native, "_DeleteProfile", lambda name: -2147024891)
            with pytest.raises(OSError, match="HRESULT"):
                profile.close()
            assert profile._created and profile.sid
        profile.close()
        assert not profile._created and not profile.sid
        profile.close()
    finally:
        profile.close()


def test_unicode_staging_and_junction_cleanup_do_not_touch_external_data(tmp_path, monkeypatch):
    import _winapi

    from nooa.runtime.sandbox import _appcontainer as launcher

    parent = tmp_path / "\u9694\u79bb runtime"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "canary.txt"
    secret.write_bytes(b"unchanged")
    make_temp = launcher.tempfile.mkdtemp
    monkeypatch.setattr(
        launcher.tempfile, "mkdtemp", lambda **kwargs: make_temp(dir=parent, **kwargs)
    )
    with _AppContainerPython() as instance:
        root = instance.root
        profile = instance._profile
        link = instance.workspace / "junction"
        _winapi.CreateJunction(str(outside), str(link))
        result = _json(
            instance,
            "import json\n"
            "try:\n"
            "    with open('junction/canary.txt', 'rb') as stream:\n"
            "        result = stream.read().decode()\n"
            "except PermissionError as exc:\n"
            "    result = exc.errno\n"
            "print(json.dumps(result))",
        )
        assert result == 13
    assert not root.exists()
    assert not profile._created
    assert secret.read_bytes() == b"unchanged"
    instance.close()
    with pytest.raises(RuntimeError, match="launcher is closed"):
        instance.run("print('must not run')")
