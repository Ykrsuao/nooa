# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""pyproject.toml files stay ASCII so `uv sync` builds on non-UTF-8 Windows locales.

uv-dynamic-versioning 0.14.0 reads pyproject.toml with the locale encoding. On a
GBK (cp936) Windows machine a single em-dash in a comment makes every editable
build fail with UnicodeDecodeError. 0.14.1 fixes the read, but it is newer than
the `exclude-newer` cutoff in [tool.uv], so the build backend still resolves to
0.14.0.
"""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_MEMBERS = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"][
    "workspace"
]["members"]
PYPROJECTS = [ROOT / "pyproject.toml", *(ROOT / m / "pyproject.toml" for m in _MEMBERS)]


@pytest.mark.parametrize("path", PYPROJECTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_pyproject_is_ascii(path: Path):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        bad = sorted({ch for ch in line if ord(ch) > 127})
        assert not bad, f"{path.relative_to(ROOT)}:{lineno} has non-ASCII {bad!r}"
