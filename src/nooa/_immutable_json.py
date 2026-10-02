# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Immutable opaque JSON without encoding/copying large string leaves per call."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Self, overload

from pydantic import BeforeValidator, PlainSerializer, SkipValidation


@dataclass(frozen=True, repr=False, slots=True)
class _FrozenObject(Mapping[str, Any]):
    """Deeply immutable, not hashable; immutability need not hash provider blobs."""

    _values: Mapping[str, Any]

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(
            self, "_values", MappingProxyType({k: freeze(v) for k, v in values.items()})
        )

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self


@overload
def freeze(value: dict[str, Any] | _FrozenObject) -> _FrozenObject: ...


@overload
def freeze(value: list[Any] | tuple[Any, ...]) -> tuple[Any, ...]: ...


@overload
def freeze[T: (str, bool, int, float, None)](value: T) -> T: ...


@overload
def freeze(value: object) -> object: ...


def freeze(value: object) -> object:
    if isinstance(value, _FrozenObject):
        return value
    if isinstance(value, dict):
        return _FrozenObject(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Native extensions must contain JSON values only, got {type(value).__name__}")


@overload
def json_containers(value: Mapping[str, Any]) -> dict[str, Any]: ...


@overload
def json_containers(value: tuple[Any, ...]) -> list[Any]: ...


@overload
def json_containers[T: (str, bool, int, float, None)](value: T) -> T: ...


@overload
def json_containers(value: object) -> object: ...


def json_containers(value: object) -> object:
    """Allocate wire/persistence containers, borrowing immutable scalar leaves."""
    if isinstance(value, Mapping):
        return {key: json_containers(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_containers(item) for item in value]
    return value


def _native_object(value: object) -> _FrozenObject:
    if not isinstance(value, (dict, _FrozenObject)):
        raise ValueError("A native extension must be a JSON object")
    return freeze(value)


NativeJSON = Annotated[
    SkipValidation[Mapping[str, Any]],
    BeforeValidator(_native_object),
    PlainSerializer(json_containers),
]
