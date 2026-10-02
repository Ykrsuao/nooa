# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Python skill reload honors optional module names and module ownership."""

import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from nooa.skill_registry import SkillRegistry


@pytest.mark.parametrize("state", ["missing_name", "owned", "replaced"])
async def test_reload_module_cleanup(state: str, tmp_path: Path):
    (tmp_path / "demo.py").write_text(
        "from nooa.skill import Skill\nclass Demo(Skill):\n    value = 'available'\n",
        encoding="utf-8",
    )
    with patch("nooa.skill_registry.entry_points", return_value=[]):
        registry = SkillRegistry(SimpleNamespace())
    replacement = ModuleType("replacement")
    old_name: str | None = None
    new_name: str | None = None
    try:
        registry.discover_skills_dirs([tmp_path])
        original = registry["ext.demo"]
        source = registry._sources["ext.demo"]
        old_name = source.module_name
        assert old_name is not None
        old_module = sys.modules[old_name]
        if state == "missing_name":
            registry._sources["ext.demo"] = replace(source, module_name=None)
        elif state == "replaced":
            sys.modules[old_name] = replacement

        assert await registry.reload("ext.demo") == "Reloaded ext.demo (self.demo)"
        current = registry["ext.demo"]
        assert current is not original
        assert original.value == current.value == "available"
        new_name = registry._sources["ext.demo"].module_name
        assert new_name is not None and new_name != old_name
        assert registry._python_modules[new_name] is sys.modules[new_name]
        if state == "missing_name":
            assert registry._python_modules[old_name] is old_module
            assert sys.modules[old_name] is old_module
        else:
            assert old_name not in registry._python_modules
            if state == "replaced":
                assert sys.modules[old_name] is replacement
            else:
                assert old_name not in sys.modules
        await registry.aclose()
        assert new_name not in sys.modules
        if state == "replaced":
            assert sys.modules[old_name] is replacement
        else:
            assert old_name not in sys.modules
    finally:
        await registry.aclose()
        if old_name is not None and sys.modules.get(old_name) is replacement:
            sys.modules.pop(old_name, None)
