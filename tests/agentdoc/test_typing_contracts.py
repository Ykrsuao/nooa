# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Omission semantics, reference visibility and optional adapter registration."""

import inspect
import subprocess
import sys
import typing

import pytest

from nooa.agentdoc import spec
from nooa.agentdoc._discover import discover_referenced_types
from nooa.agentdoc._docs import Spec, SpecAnnotation
from nooa.agentdoc._metadata import get_field_metadata


@pytest.mark.parametrize("value", [None, 0, 12])
def test_render_limits_preserve_explicit_values_and_omission(value: int | None):
    marker = spec(max_length=value, max_string=value, max_depth=value)
    assert isinstance(marker, SpecAnnotation)
    assert marker.kwargs == {"max_length": value, "max_string": value, "max_depth": value}
    omitted = spec()
    assert isinstance(omitted, SpecAnnotation)
    assert omitted.kwargs == {}

    class Example:
        text = "example"

    spec(Example, "text", max_string=value)
    spec(Example, "text", description="label")
    assert get_field_metadata(Example, "text")["max_string"] == value


def test_public_limit_types_do_not_expose_the_internal_sentinel():
    annotations = typing.get_type_hints(Spec.__call__)
    signature = inspect.signature(Spec.__call__)
    for name in ("max_length", "max_string", "max_depth"):
        assert annotations[name] == int | None
        assert signature.parameters[name].default is not None


class Visible:
    pass


class Secret:
    pass


class Surface:
    visible: Visible
    secret: Secret

    def __init__(self):
        self.visible = Visible()
        self.secret = Secret()


def test_instance_visibility_does_not_hide_the_class_contract():
    instance = Surface()
    spec(instance, "secret", hidden=True)
    assert discover_referenced_types(instance) == [Visible]
    assert discover_referenced_types(Surface) == [Secret, Visible]
    assert discover_referenced_types(instance, seen={Visible}) == []
    assert discover_referenced_types(Surface, field_names={"visible"}) == [Visible]


def _run(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_plotly_adapter_registers_curated_module_views():
    _run(
        "import sys, types\n"
        "from nooa.agentdoc import doc\n"
        "names = ('plotly', 'plotly.express', 'plotly.graph_objects')\n"
        "modules = [types.ModuleType(name) for name in names]\n"
        "modules[0].__version__ = 'test-version'\n"
        "sys.modules.update(zip(names, modules))\n"
        "import nooa.agentdoc.adapters.plotly\n"
        "assert 'test-version' in doc(modules[0])\n"
        "assert 'scatter_map' in doc(modules[1])\n"
        "assert 'FigureWidget' in doc(modules[2])\n"
    )


def test_missing_plotly_is_not_required_by_core_or_register_all():
    _run(
        "import sys\n"
        "sys.modules['plotly'] = None\n"
        "from nooa.agentdoc import doc\n"
        "from nooa.agentdoc.adapters import register_all\n"
        "assert 'plotly' not in register_all()\n"
        "try:\n"
        "    import nooa.agentdoc.adapters.plotly\n"
        "except ImportError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('Explicit adapter import must require Plotly')\n"
    )
