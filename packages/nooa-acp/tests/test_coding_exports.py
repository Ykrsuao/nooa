# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ACP's coding imports preserve concrete types and lazy framework loading."""

import subprocess
import sys
from importlib import import_module
from typing import TYPE_CHECKING, assert_type

import pytest
from nooa_cli import coding

if TYPE_CHECKING:
    from nooa_cli.coding.activity import (
        ActivityShellTools,
        FileEdit,
        TerminalCommandFinished,
        TerminalCommandOutput,
        TerminalCommandStarted,
    )
    from nooa_cli.coding.agent import CodingAgent
    from nooa_cli.coding.slash_commands import CodingSlashCommand, CodingSlashCommandRegistry

    assert_type(coding.ActivityShellTools, type[ActivityShellTools])
    assert_type(coding.FileEdit, type[FileEdit])
    assert_type(coding.TerminalCommandFinished, type[TerminalCommandFinished])
    assert_type(coding.TerminalCommandOutput, type[TerminalCommandOutput])
    assert_type(coding.TerminalCommandStarted, type[TerminalCommandStarted])
    assert_type(coding.CodingAgent, type[CodingAgent])
    assert_type(coding.CodingSlashCommand, type[CodingSlashCommand])
    assert_type(coding.CodingSlashCommandRegistry, type[CodingSlashCommandRegistry])


def test_coding_facade_and_leaf_imports_keep_host_components_lazy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from nooa_cli import coding\n"
            "assert 'nooa' not in sys.modules\n"
            "from nooa_cli.coding import load_coding_skills_dirs\n"
            "assert callable(load_coding_skills_dirs)\n"
            "assert 'nooa_cli.coding.agent' not in sys.modules\n"
            "assert 'nooa_cli.coding.activity' not in sys.modules\n"
            "assert 'nooa_cli.coding.slash_commands' not in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", coding.__all__)
def test_coding_exports_keep_concrete_identity(name):
    module = import_module(f"nooa_cli.coding.{coding._EXPORT_MODULES[name]}")
    exported = getattr(coding, name)
    assert exported is getattr(module, name)
    assert vars(coding)[name] is exported


def test_unknown_coding_export_is_rejected():
    with pytest.raises(AttributeError, match="has no attribute 'unknown_coding_component'"):
        _ = coding.unknown_coding_component
