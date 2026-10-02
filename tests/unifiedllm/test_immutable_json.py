# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native JSON preserves container shape, immutable leaves and request ownership."""

import copy
from collections.abc import Mapping
from typing import Any, assert_type

import pytest

from nooa._immutable_json import freeze, json_containers


def test_object_round_trip_is_typed_and_detached() -> None:
    text = "opaque-native-payload" * 1000
    original: dict[str, Any] = {"blocks": [{"text": text}], "flag": True, "missing": None}
    frozen: Mapping[str, Any] = freeze(original)
    projected = assert_type(json_containers(frozen), dict[str, Any])

    assert projected == original
    assert projected is not original
    assert projected["blocks"][0] is not original["blocks"][0]
    assert projected["blocks"][0]["text"] is text
    assert freeze(frozen) is frozen
    assert copy.deepcopy(frozen) is frozen
    original["blocks"][0]["text"] = "source edit"
    projected["blocks"][0]["text"] = "request edit"
    assert json_containers(frozen)["blocks"][0]["text"] is text
    with pytest.raises(TypeError):
        frozen["blocks"][0]["text"] = "native edit"


def test_array_round_trip_preserves_scalar_leaves() -> None:
    original: list[Any] = [{"text": "answer"}, 0, 1.5, False, None]
    frozen: tuple[Any, ...] = freeze(original)
    projected = assert_type(json_containers(frozen), list[Any])

    assert projected == original
    assert all(projected[index] is original[index] for index in range(1, len(original)))
    projected[0]["text"] = "edited"
    assert json_containers(frozen)[0] == {"text": "answer"}


@pytest.mark.parametrize("value", [None, "text", True, 0, 1.5])
def test_scalar_leaves_are_borrowed(value: Any) -> None:
    assert freeze(value) is value
    assert json_containers(value) is value


@pytest.mark.parametrize("value", [object(), {"bad": object()}, [object()]])
def test_non_json_values_are_rejected(value: Any) -> None:
    with pytest.raises(TypeError, match="JSON values only"):
        freeze(value)
