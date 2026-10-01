# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared coding-agent components used by terminal and protocol hosts."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .activity import (
        ActivityShellTools,
        FileEdit,
        TerminalCommandFinished,
        TerminalCommandOutput,
        TerminalCommandStarted,
    )
    from .agent import CodingAgent
    from .instructions import discover_agent_instruction_files, render_agent_instructions
    from .settings import load_coding_skills_dirs
    from .slash_commands import CodingSlashCommand, CodingSlashCommandRegistry

_EXPORT_MODULES = {
    "ActivityShellTools": "activity",
    "FileEdit": "activity",
    "TerminalCommandFinished": "activity",
    "TerminalCommandOutput": "activity",
    "TerminalCommandStarted": "activity",
    "CodingAgent": "agent",
    "discover_agent_instruction_files": "instructions",
    "render_agent_instructions": "instructions",
    "load_coding_skills_dirs": "settings",
    "CodingSlashCommand": "slash_commands",
    "CodingSlashCommandRegistry": "slash_commands",
}


def __getattr__(name: str):
    """Load host components only when requested, not for leaf utility imports."""
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORT_MODULES[name]}"), name)
    globals()[name] = value
    return value


__all__ = [
    "ActivityShellTools",
    "CodingAgent",
    "CodingSlashCommand",
    "CodingSlashCommandRegistry",
    "FileEdit",
    "TerminalCommandFinished",
    "TerminalCommandOutput",
    "TerminalCommandStarted",
    "discover_agent_instruction_files",
    "load_coding_skills_dirs",
    "render_agent_instructions",
]
