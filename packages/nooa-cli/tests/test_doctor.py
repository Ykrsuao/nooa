# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only diagnostic policy, output contract, and failure handling."""

import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from nooa_cli import _doctor, oo
from nooa_cli._doctor import Check


def test_doctor_skips_secrets_preload(monkeypatch, tmp_path):
    import nooa.secrets

    def unexpected():
        pytest.fail("doctor must not preload secrets")

    monkeypatch.setattr(nooa.secrets, "load_secrets_into_env", unexpected)
    report = _doctor._report([Check("sandbox", "warning", "not supported", "Use Linux.")])
    monkeypatch.setattr(_doctor, "diagnose", lambda *args, **kwargs: report)
    result = CliRunner().invoke(oo, ["doctor", "--json", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == report


def test_text_output_includes_fix_and_error_exit(monkeypatch):
    report = _doctor._report([Check("bash", "error", "missing", "Install Git.")])
    monkeypatch.setattr(_doctor, "diagnose", lambda *args, **kwargs: report)
    result = CliRunner().invoke(oo, ["doctor"])
    assert result.exit_code == 1
    assert "[ERROR] bash: missing" in result.output
    assert "Fix: Install Git." in result.output


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_port_is_a_usage_error(port):
    assert CliRunner().invoke(oo, ["doctor", "--port", port]).exit_code == 2


def test_worker_environment_excludes_credentials_and_startup_hooks(monkeypatch):
    for key in ("OPENAI_API_KEY", "NEMO_OO_SECRETS", "BASH_ENV", "ENV", "PYTHONPATH"):
        monkeypatch.setenv(key, "must-not-be-forwarded")
    monkeypatch.setenv("NOOA_BASH", "custom-bash")
    monkeypatch.setenv("NEMO_OO_USER_DIR", "custom-config-location")
    monkeypatch.setenv("PYTHONIOENCODING", "cp936")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    env = _doctor._worker_environment()
    assert "must-not-be-forwarded" not in env.values()
    assert env["NOOA_BASH"] == "custom-bash"
    assert env["NEMO_OO_USER_DIR"] == "custom-config-location"
    assert env["PYTHONIOENCODING"] == "cp936"
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


def test_worker_command_uses_argument_list_and_bounded_timeout(monkeypatch, tmp_path):
    expected = _doctor._report([Check("smoke", "ok", "passed")])
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected))

    monkeypatch.setattr(_doctor.subprocess, "run", run)
    workspace = tmp_path / "\u4e2d\u6587 directory"
    assert _doctor.diagnose(workspace, port=5002, smoke=True) == expected
    command, kwargs = calls[0]
    assert command[-1] == "--smoke"
    assert command[command.index("--workspace") + 1] == str(workspace.resolve())
    assert kwargs["timeout"] == 90
    assert not kwargs.get("shell")


@pytest.mark.parametrize("stdout", ["", "[]", '{"checks": []}', '{"ok": true, "checks": []}'])
def test_invalid_worker_output_is_a_structured_error(monkeypatch, tmp_path, stdout):
    monkeypatch.setattr(
        _doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )
    report = _doctor.diagnose(tmp_path, port=5001, smoke=False)
    assert report["ok"] is False
    assert report["checks"][0]["id"] == "doctor"


def test_worker_timeout_is_a_structured_error(monkeypatch, tmp_path):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("doctor", kwargs["timeout"])

    monkeypatch.setattr(_doctor.subprocess, "run", run)
    report = _doctor.diagnose(tmp_path, port=5001, smoke=False)
    assert report["ok"] is False
    assert "timed out" in report["checks"][0]["message"]


def test_selected_workspace_is_used_for_relative_paths(monkeypatch, tmp_path):
    def run(command, **kwargs):
        assert kwargs["cwd"] == tmp_path.resolve()
        return SimpleNamespace(returncode=0, stdout=json.dumps(_doctor._report([])))

    monkeypatch.setattr(_doctor.subprocess, "run", run)
    assert _doctor.diagnose(tmp_path, port=5001, smoke=False)["ok"] is True


def test_unsupported_python_is_an_error(monkeypatch):
    monkeypatch.setattr(_doctor.sys, "version_info", (3, 11))
    assert _doctor._python_check().status == "error"


def test_wsl_is_distinguished_from_native_windows(monkeypatch):
    monkeypatch.setattr(_doctor, "PLATFORM", "linux")
    monkeypatch.setattr(_doctor.platform, "release", lambda: "6.6-microsoft-standard-WSL2")
    result = _doctor._python_check()
    assert result.status == "warning"
    assert "not native Windows" in result.message


@pytest.mark.parametrize(
    "path",
    [r"C:\Windows\System32\bash.exe", r"C:\Windows\Sysnative\bash.exe", r"C:\tools\wsl.exe"],
)
def test_wsl_launcher_override_is_rejected_without_execution(monkeypatch, path):
    monkeypatch.setattr(_doctor, "PLATFORM", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(_doctor, "_find_bash", lambda: Path(path))
    result = _doctor._bash_check()
    assert result.status == "error"
    assert "WSL launcher" in result.message


def test_missing_bash_gives_windows_install_advice(monkeypatch):
    monkeypatch.setattr(_doctor, "PLATFORM", "win32")

    def missing():
        raise FileNotFoundError("no bash")

    monkeypatch.setattr(_doctor, "_find_bash", missing)
    result = _doctor._bash_check()
    assert result.status == "error"
    assert "winget" in result.fix
    assert "NOOA_BASH" in result.fix


def test_windows_bash_discovery_reuses_runtime_resolver(monkeypatch, tmp_path):
    if _doctor.PLATFORM != "win32":
        pytest.skip("Windows runtime resolver")
    from nooa.tools import _win_bash

    path = tmp_path / "bash.exe"
    monkeypatch.setattr(_win_bash, "find_bash", lambda: path)
    assert _doctor._find_bash() == path.resolve()


def test_missing_ripgrep_is_a_warning_with_install_advice(monkeypatch):
    monkeypatch.setattr(_doctor, "PLATFORM", "win32")
    monkeypatch.setattr(_doctor.shutil, "which", lambda name: None)
    result = _doctor._tool_check("rg", "winget install --id BurntSushi.ripgrep.MSVC -e")
    assert result.status == "warning"
    assert "BurntSushi.ripgrep.MSVC" in result.fix


def test_missing_viewer_packages_are_optional(monkeypatch):
    def version(name):
        if name == "uvicorn":
            raise _doctor.metadata.PackageNotFoundError(name)
        return "1.0"

    monkeypatch.setattr(_doctor.metadata, "version", version)
    result = _doctor._viewer_check()
    assert result.status == "warning"
    assert "uvicorn" in result.message
    assert result.fix == 'uv add "nooa[viewer]"'


def test_path_checks_do_not_create_directories(tmp_path):
    path = tmp_path / "new config" / "nested"
    assert _doctor._path_check("config", path).status == "ok"
    assert not path.parent.exists()
    assert _doctor._path_check("workspace", path, required=True).status == "error"


def test_file_in_directory_path_is_reported(tmp_path):
    file = tmp_path / "not-a-directory"
    file.write_text("keep", encoding="utf-8")
    assert _doctor._path_check("config", file / "nested").status == "error"
    assert _doctor._path_check("config", file).status == "error"
    assert file.read_text(encoding="utf-8") == "keep"


def test_permission_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(_doctor.os, "access", lambda *args: False)
    assert _doctor._path_check("workspace", tmp_path).status == "error"


def test_database_requires_writable_journal_directory(monkeypatch, tmp_path):
    db = tmp_path / "traces.db"
    db.write_bytes(b"")
    monkeypatch.setattr(_doctor.os, "access", lambda path, mode: path == db)
    result = _doctor._path_check("trace_database", db, directory=False)
    assert result.status == "error"
    assert "journal files" in result.message


def test_empty_database_override_is_not_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("NEMO_OO_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("NOOA_TRACE_DB", "")
    monkeypatch.setenv("NEMO_OO_TRACE_DB", str(tmp_path / "legacy.db"))
    checks = {check.id: check for check in _doctor._configured_paths()}
    assert checks["trace_database"].status == "error"
    assert list(tmp_path.iterdir()) == []


def test_nonexistent_drive_does_not_loop(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    result = _doctor._path_check("config", Path.cwd() / "missing")
    assert result.status == "error"
    assert "No existing parent" in result.message


def test_port_probe_warns_without_stopping_the_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = _doctor._port_check(listener.getsockname()[1])
        assert result.status == "warning"
        assert listener.fileno() >= 0
    assert _doctor._port_check(0).status == "ok"


@pytest.mark.parametrize(("smoke", "bash_status"), [(False, "ok"), (True, "error")])
def test_smoke_is_not_run_without_opt_in_and_prerequisites(
    monkeypatch, tmp_path, smoke, bash_status
):
    monkeypatch.setattr(_doctor, "_bash_check", lambda: Check("bash", bash_status, "test"))
    monkeypatch.setattr(_doctor, "_configured_paths", lambda: [])

    def unexpected():
        pytest.fail("smoke should not start")

    monkeypatch.setattr(_doctor, "_smoke_check", unexpected)
    checks = _doctor.collect_checks(tmp_path, port=0, smoke=smoke)
    assert next(check for check in checks if check.id == "smoke").status == "skipped"


def test_failed_smoke_cleans_temporary_files_and_restores_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    previous = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}

    async def fail(workspace):
        assert workspace.is_dir()
        assert Path(tempfile.gettempdir()) == workspace.parent
        (workspace / "probe").write_text("temporary", encoding="utf-8")
        raise RuntimeError("probe failed")

    monkeypatch.setattr(_doctor, "_exercise_shell", fail)
    result = _doctor._smoke_check()
    assert result.status == "error"
    assert "probe failed" in result.message
    assert list(tmp_path.iterdir()) == []
    assert tempfile.tempdir == str(tmp_path)
    assert {name: os.environ.get(name) for name in previous} == previous


@pytest.mark.parametrize("available", [True, False])
def test_windows_sandbox_prerequisites_are_separate_from_fork_support(monkeypatch, available):
    from nooa.runtime.sandbox import windows
    from nooa.runtime.sandbox._windows_capabilities import WindowsSandboxCapabilities

    monkeypatch.setattr(_doctor, "PLATFORM", "win32")
    report = WindowsSandboxCapabilities(
        native_windows=True,
        native_api_available=available,
        detail="API prerequisite result. No session was started; containment is not verified.",
    )
    monkeypatch.setattr(windows, "probe_windows_sandbox", lambda: report)
    checks = {check.id: check for check in _doctor._sandbox_checks()}
    assert checks["sandbox"].status == "warning"
    assert "no fork-based sandbox" in checks["sandbox"].message
    assert "WindowsSandboxSession" in checks["sandbox"].fix
    native = checks["windows_sandbox"]
    assert native.status == ("ok" if available else "warning")
    assert "prerequisites" in native.message
    assert "No session was started" in native.message
    assert "containment is not verified" in native.message


def test_windows_prerequisite_probe_failure_is_reported(monkeypatch):
    from nooa.runtime.sandbox import windows

    monkeypatch.setattr(_doctor, "PLATFORM", "win32")

    def fail():
        raise OSError("native API loading failed")

    monkeypatch.setattr(windows, "probe_windows_sandbox", fail)
    native = _doctor._sandbox_checks()[1]
    assert native.id == "windows_sandbox"
    assert native.status == "warning"
    assert "native API loading failed" in native.message
    assert "No session was started" in native.message
    assert "containment is not verified" in native.message


def test_non_windows_doctor_does_not_probe_windows(monkeypatch):
    from nooa.runtime.sandbox import windows

    monkeypatch.setattr(_doctor, "PLATFORM", "linux")

    def unexpected():
        pytest.fail("non-Windows doctor must not probe native Windows APIs")

    monkeypatch.setattr(windows, "probe_windows_sandbox", unexpected)
    assert _doctor._sandbox_checks() == [
        Check("sandbox", "skipped", "OS containment is not tested by doctor.")
    ]


def test_successful_smoke_explicitly_does_not_verify_sandbox(monkeypatch):
    async def shell_only(workspace):
        assert workspace.is_dir()

    monkeypatch.setattr(_doctor, "_exercise_shell", shell_only)
    result = _doctor._smoke_check()
    assert result.status == "ok"
    assert "shell check does not start a sandbox or verify containment" in result.message
