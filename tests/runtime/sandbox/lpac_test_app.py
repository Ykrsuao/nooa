# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Import-safe application data/code copied explicitly into the test runtime."""

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel


class Unit(Enum):
    COUNT = "count"


@dataclass
class Item:
    amount: int


class Request(BaseModel):
    item: Item
    unit: Unit = Unit.COUNT


class Answer(BaseModel):
    value: int
    worker: int


def increment(value: int) -> int:
    return value + 1
