# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Diagnostic worker. Keep imports light so missing optional packages are reportable."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path, PureWindowsPath
from typing import Literal

PLATFORM = sys.platform
_VIEWER_PACKAGES = ("fastapi", "uvicorn", "python-dotenv", "python-multipart")
_ENVIRONMENT_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "PROGRAMFILES",
    "PROGRAMW6432",
    "PROGRAMFILES(X86)",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
    "NOOA_BASH",
    "NEMO_OO_USER_DIR",
    "NEMO_OO_PROJECT_DIR",
    "NOOA_TRACE_DB",
    "NEMO_OO_TRACE_DB",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONPYCACHEPREFIX",
)


@dataclass(frozen=True)
class Check:
    id: str
    status: Literal["ok", "warning", "error", "skipped"]
    message: str
    fix: str | None = None


def _report(checks: list[Check]) -> dict:
    return {
        "schema_version": 1,
        "ok": not any(check.status == "error" for check in checks),
        "platform": PLATFORM,
        "python": platform.python_version(),
        "executable": sys.executable,
        "utf8_mode": bool(sys.flags.utf8_mode),
        "checks": [asdict(check) for check in checks],
    }


def _worker_environment() -> dict[str, str]:
    # Retain only environment used by the checks. In particular, no API keys,
    # secrets/settings overrides, BASH_ENV startup scripts, or Python injection.
    keys = set(_ENVIRONMENT_KEYS)
    env = {key: value for key, value in os.environ.items() if key.upper() in keys}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return env


def diagnose(workspace: Path, *, port: int, smoke: bool) -> dict:
    try:
        workspace = workspace.expanduser().resolve()
        command = [
            sys.executable,
            "-m",
            "nooa_cli._doctor",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
        ]
        if smoke:
            command.append("--smoke")
        result = subprocess.run(
            command,
            cwd=workspace if workspace.is_dir() else None,
            env=_worker_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90 if smoke else 30,
        )
        report = json.loads(result.stdout)
        if (
            not isinstance(report, dict)
            or report.get("schema_version") != 1
            or not isinstance(report.get("ok"), bool)
            or not isinstance(report.get("checks"), list)
        ):
            raise ValueError("invalid diagnostic report")
        if result.returncode != (0 if report["ok"] else 1):
            raise ValueError(f"diagnostic worker exited with code {result.returncode}")
        return report
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return _report(
            [
                Check(
                    "doctor",
                    "error",
                    f"Diagnostic worker could not finish: {exc}",
                    "Check this Python installation and reinstall with uv add nooa-cli.",
                )
            ]
        )


def _python_check() -> Check:
    description = f"{platform.python_implementation()} {platform.python_version()} on {PLATFORM}"
    if sys.version_info[:2] not in ((3, 12), (3, 13)):
        return Check(
            "python", "error", description, "Use Python 3.12 or 3.13: uv python install 3.13"
        )
    if PLATFORM == "linux" and "microsoft" in platform.release().lower():
        return Check(
            "python",
            "warning",
            description + " (WSL, not native Windows)",
            "For native Windows support, run Windows Python from PowerShell.",
        )
    return Check("python", "ok", description)


def _tool_check(name: str, windows_install: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(name, "ok", f"Found {path} (not executed).")
    fix = (
        windows_install
        if PLATFORM == "win32"
        else f"Install {name} with your system package manager."
    )
    return Check(name, "warning", f"{name} is not on PATH.", fix)


def _find_bash() -> Path:
    if PLATFORM == "win32":
        from nooa.tools._win_bash import find_bash

        return find_bash().resolve()
    # Match BashSession, which deliberately uses /bin/bash rather than PATH.
    path = Path("/bin/bash")
    if not path.is_file():
        raise FileNotFoundError("/bin/bash is missing")
    return path


def _bash_check() -> Check:
    fix = (
        "Install Git for Windows: winget install --id Git.Git -e; "
        "or set NOOA_BASH to an MSYS2 bash.exe (not a WSL launcher)."
        if PLATFORM == "win32"
        else "Install bash with your system package manager."
    )
    try:
        path = _find_bash()
        if PLATFORM == "win32":
            candidate = PureWindowsPath(str(path))
            windows = PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows"))
            if candidate.name.lower() == "wsl.exe" or candidate.parent in (
                windows / "System32",
                windows / "Sysnative",
            ):
                return Check(
                    "bash", "error", f"{path} is a Windows/WSL launcher, not MSYS2 bash.", fix
                )
        return Check("bash", "ok", f"Found {path}; use --smoke to verify execution.")
    except (ImportError, OSError, RuntimeError) as exc:
        return Check("bash", "error", str(exc), fix)


def _viewer_check() -> Check:
    installed = []
    missing = []
    for package in _VIEWER_PACKAGES:
        try:
            installed.append(f"{package} {metadata.version(package)}")
        except metadata.PackageNotFoundError:
            missing.append(package)
    if missing:
        return Check(
            "viewer",
            "warning",
            "Missing optional viewer packages: " + ", ".join(missing),
            'uv add "nooa[viewer]"',
        )
    return Check("viewer", "ok", "Installed: " + ", ".join(installed) + " (metadata check only).")


def _path_check(name: str, path: Path, *, directory: bool = True, required: bool = False) -> Check:
    try:
        path = path.expanduser().absolute()
        if path.is_symlink() and not path.exists():
            raise OSError(f"{path} is a broken symlink")
        if required and not path.exists():
            raise OSError(f"{path} does not exist")
        if path.exists() and (not path.is_dir() if directory else not path.is_file()):
            raise OSError(f"{path} is not a {'directory' if directory else 'file'}")
        probe = path
        while not probe.exists():
            if probe.parent == probe:
                raise OSError(f"No existing parent directory for {path}")
            probe = probe.parent
        if probe != path and not probe.is_dir():
            raise OSError(f"{probe} is not a directory")
        mode = os.W_OK | (os.X_OK if probe.is_dir() else 0)
        if not os.access(probe, mode):
            raise PermissionError(f"No write access reported for {probe}")
        if not directory and path.is_file() and not os.access(path.parent, os.W_OK | os.X_OK):
            raise PermissionError(f"No write access for SQLite journal files in {path.parent}")
        return Check(
            name,
            "ok",
            f"{path}; write access appears available at {probe} "
            "(read-only permission estimate, no file created).",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return Check(name, "error", str(exc), "Choose a writable path or correct its permissions.")


def _configured_paths() -> list[Check]:
    from nooa.paths import get_project_dir, get_user_dir

    user = get_user_dir()
    value = os.environ.get("NOOA_TRACE_DB", os.environ.get("NEMO_OO_TRACE_DB"))
    db = Path(value) if value is not None else user / "traces.db"
    return [
        _path_check("user_directory", user),
        _path_check("project_directory", get_project_dir()),
        _path_check("trace_database", db, directory=False),
    ]


def _port_check(port: int) -> Check:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if PLATFORM == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        return Check("viewer_port", "ok", f"127.0.0.1:{port} can be bound (no server started).")
    except OSError as exc:
        alternative = port + 1 if port < 65535 else port - 1
        return Check(
            "viewer_port",
            "warning",
            f"127.0.0.1:{port} is occupied or unavailable: {exc}",
            f"An existing viewer may already use it. Choose another port: nooa start-dev --port {alternative}",
        )


async def _exercise_shell(workspace: Path) -> None:
    from nooa.tools.shell_tools import ShellTools

    shell = ShellTools(cwd=str(workspace))
    pending = None
    last_process = None
    try:
        async with asyncio.timeout(45):
            filename = "\u4e2d\u6587 file.txt"
            text = "\u4e2d\u6587 smoke"
            await shell.write_file(filename, text)
            code = f"from pathlib import Path; print(Path({filename!r}).read_text(encoding='utf-8'), end='')"
            python = shlex.quote(sys.executable.replace("\\", "/"))
            result = await shell.run(f"{python} -c {shlex.quote(code)}", timeout=10)
            if result.returncode or result.stdout != text:
                raise RuntimeError(
                    "Unicode file/command output did not round-trip as UTF-8: "
                    f"exit={result.returncode}, stdout={result.stdout[:500]!r}, "
                    f"stderr={result.stderr[:500]!r}"
                )
            last_process = shell.session._process
            pending = asyncio.create_task(shell.run("printf ready > ready; sleep 60", timeout=70))
            async with asyncio.timeout(10):
                while not (workspace / "ready").exists():
                    if pending.done():
                        raise RuntimeError("The cancellation probe failed to start.")
                    await asyncio.sleep(0.05)
            pending.cancel()
            try:
                await asyncio.wait_for(pending, timeout=10)
            except asyncio.CancelledError:
                pass
            else:
                raise RuntimeError("The running command did not acknowledge cancellation.")
            if last_process is None or last_process.returncode is None:
                raise RuntimeError("The cancelled shell process was not reaped.")
            result = await shell.run("printf recovered", timeout=10)
            last_process = shell.session._process
            if result.returncode or result.stdout != "recovered":
                raise RuntimeError("The shell could not recover after cancellation.")
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await shell.close()
    if last_process is None or last_process.returncode is None:
        raise RuntimeError("Shell shutdown did not reap the process.")


def _smoke_check() -> Check:
    previous_temp = tempfile.tempdir
    names = ("TEMP", "TMP", "TMPDIR")
    previous = {name: os.environ.get(name) for name in names}
    try:
        with tempfile.TemporaryDirectory(prefix="nooa-doctor-") as temporary:
            root = Path(temporary).resolve()
            workspace = root / "\u4e2d\u6587 workspace"
            workspace.mkdir()
            # Keep the Windows python3 shim and shell-created temp files inside
            # the same disposable tree, including tempfile's cached temp path.
            tempfile.tempdir = str(root)
            os.environ.update({name: str(root) for name in names})
            try:
                asyncio.run(_exercise_shell(workspace))
            finally:
                tempfile.tempdir = previous_temp
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        return Check(
            "smoke",
            "ok",
            "Unicode I/O, command cancellation, recovery, and shell cleanup passed. "
            "This shell check does not start a sandbox or verify containment.",
        )
    except Exception as exc:
        return Check(
            "smoke",
            "error",
            f"{type(exc).__name__}: {exc}",
            "Check the bash diagnostic, loopback/firewall access, TEMP permissions, "
            "and any explicit PYTHONIOENCODING override; then retry nooa doctor --smoke.",
        )


def _sandbox_checks() -> list[Check]:
    if PLATFORM != "win32":
        return [Check("sandbox", "skipped", "OS containment is not tested by doctor.")]
    checks = [
        Check(
            "sandbox",
            "warning",
            "Native Windows has no fork-based sandbox. In-process execution is not containment.",
            "Use an explicit WindowsSandboxSession with WindowsSandboxPolicy, "
            "or an isolated, supported Linux environment for fork-based execution.",
        )
    ]
    try:
        from nooa.runtime.sandbox.windows import probe_windows_sandbox

        capabilities = probe_windows_sandbox()
        checks.append(
            Check(
                "windows_sandbox",
                "ok" if capabilities.native_api_available else "warning",
                f"Explicit WindowsSandboxSession prerequisites: {capabilities.detail}",
                None
                if capabilities.native_api_available
                else "Check native Windows AppContainer and Job Object API availability. "
                "WindowsSandboxSession fails closed when its prerequisites are unavailable.",
            )
        )
    except Exception as exc:
        checks.append(
            Check(
                "windows_sandbox",
                "warning",
                f"WindowsSandboxSession prerequisite probe failed: {type(exc).__name__}: {exc}. "
                "No session was started; containment is not verified.",
                "Check the core installation and native Windows support.",
            )
        )
    return checks


def collect_checks(workspace: Path, *, port: int, smoke: bool) -> list[Check]:
    checks = [
        _python_check(),
        _tool_check("git", "winget install --id Git.Git -e"),
        _tool_check("rg", "winget install --id BurntSushi.ripgrep.MSVC -e"),
        _bash_check(),
        _viewer_check(),
        _path_check("workspace", workspace, required=True),
        _port_check(port),
    ]
    try:
        checks.extend(_configured_paths())
    except Exception as exc:
        checks.append(
            Check("paths", "error", str(exc), "Repair the core installation: uv add nooa")
        )
    checks.extend(_sandbox_checks())
    if not smoke:
        checks.append(
            Check(
                "smoke",
                "skipped",
                "Not requested; run nooa doctor --smoke for a disposable shell check. "
                "It does not start a sandbox or verify containment.",
            )
        )
    elif any(check.status == "error" for check in checks if check.id in ("python", "bash")):
        checks.append(
            Check("smoke", "skipped", "Fix Python/bash errors before running the smoke check.")
        )
    else:
        checks.append(_smoke_check())
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    # Dependency notices must not corrupt the JSON protocol with the CLI.
    with contextlib.redirect_stdout(sys.stderr):
        checks = collect_checks(args.workspace, port=args.port, smoke=args.smoke)
    report = _report(checks)
    print(json.dumps(report, ensure_ascii=True))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
