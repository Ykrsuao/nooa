# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit importable coding types for a fresh native Windows code worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nooa_cli.coding.activity import ActivityShellTools
from nooa_cli.tools.repo_tools import RepoTools

from nooa import Context
from nooa.agents import TokenBudgetSummarizer
from nooa.config import CodeActConfig, PredictConfig
from nooa.interactive import Done, NeedInput, SummarizationConfig, Waiting
from nooa.skill_registry import SkillRegistry
from nooa.tools import SkillWriting, TodoManager
from nooa.tools.shell_tools import ShellTools


def coding_sandbox_globals() -> dict[str, Any]:
    """Expose the useful types CodingAgent already exposes to Linux cells.

    These references resolve inside the staged nooa/nooa-cli dependency closure.
    Keep this explicit: copying arbitrary agent globals would also copy local
    state or require imports outside that closure. Framework helpers such as
    doc()/strategy and typing names already come from build_namespace(); authoring
    markers and host path/configuration helpers are deliberately not transferred.
    """
    return {
        "Path": Path,
        "Context": Context,
        "CodeActConfig": CodeActConfig,
        "PredictConfig": PredictConfig,
        "SummarizationConfig": SummarizationConfig,
        "Done": Done,
        "NeedInput": NeedInput,
        "Waiting": Waiting,
        "ActivityShellTools": ActivityShellTools,
        "RepoTools": RepoTools,
        "ShellTools": ShellTools,
        "SkillRegistry": SkillRegistry,
        "SkillWriting": SkillWriting,
        "TodoManager": TodoManager,
        "TokenBudgetSummarizer": TokenBudgetSummarizer,
    }
