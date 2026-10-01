# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check wheel acceptance scheduling without building or launching workers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_acceptance_budget_preserves_isolation_and_complete_selection(
    monkeypatch, tmp_path, capsys, selected, fail
):
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_install.py"
    spec = importlib.util.spec_from_file_location("_smoke_install", path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    temporary_directory = script.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        script.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs),
    )
    monkeypatch.setattr(script.shutil, "which", lambda _: "uv")
    monkeypatch.setenv("PYTHONPATH", "must-not-reach-installed-tests")
    monkeypatch.setenv("PYTHONHOME", "must-not-reach-installed-tests")
    options = ["--test-file", "test_windows_api.py"] * 2 if selected else []
    monkeypatch.setattr(script.sys, "argv", [str(path), "--python", "3.12", *options])
    launches = []

    def run(command, **kwargs):
        if command[1] == "build":
            wheels = Path(command[command.index("--out-dir") + 1])
            wheels.mkdir(exist_ok=True)
            package = command[command.index("--package") + 1]
            (wheels / f"{package.replace('-', '_')}-0.0.0-py3-none-any.whl").write_bytes(b"")
        if command[1:4] == ["-I", "-m", "pytest"]:
            launches.append((command, kwargs))
            suite = kwargs["cwd"] / "acceptance"
            assert all((suite / name).is_file() for name in script.NATIVE_TESTS)
            if fail:
                raise script.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(script.subprocess, "run", run)
    if fail:
        with pytest.raises(script.subprocess.CalledProcessError):
            script.main()
    else:
        script.main()
    assert ("acceptance passed." in capsys.readouterr().out) is not fail

    assert len(launches) == 1
    command, options = launches[0]
    suite = options["cwd"] / "acceptance"
    assert options["timeout"] == (1800 if selected else 3600)
    assert options["check"] is True
    assert "PYTHONPATH" not in options["env"] and "PYTHONHOME" not in options["env"]
    assert options["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert options["env"]["NOOA_SMOKE_INSTALL_ROOT"] == str(
        options["cwd"] / "\u5e72\u51c0\u5b89\u88c5 environment"
    )
    assert command[4:8] == ["-p", "pytest_asyncio.plugin", "-p", "pytest_timeout"]
    targets = command[command.index("--confcutdir") + 2 :]
    assert targets == (
        [
            str(suite / "test_installed_workflows.py") + "::test_imports_are_from_installed_wheels",
            str(suite / "test_windows_api.py"),
        ]
        if selected
        else [str(suite)]
    )
    assert not options["cwd"].exists()
