# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Smoke test: verify the nooa CLI package is importable and the main entry point exists."""

import subprocess
import sys


def test_cli_importable():
    from nooa_cli import main

    assert callable(main)


def test_commands_discoverable():
    from nooa_cli.commands import discover_commands

    commands = list(discover_commands())
    assert len(commands) > 0
    names = [name for name, _ in commands]
    assert "start-dev" in names
    assert "eval" in names
    assert "config" in names
    assert "doctor" in names


def test_cli_discovery_does_not_import_tracing_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nooa_cli; assert 'nooa.tracing' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr


def test_doctor_help_does_not_import_core_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from click.testing import CliRunner\n"
            "from nooa_cli import oo\n"
            "result = CliRunner().invoke(oo, ['doctor', '--help'])\n"
            "assert result.exit_code == 0, result.output\n"
            "assert 'nooa' not in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
