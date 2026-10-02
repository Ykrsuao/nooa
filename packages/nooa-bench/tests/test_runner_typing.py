# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Public trajectory serialization preserves model protocol filtering."""

import json
from typing import Annotated, Any

from nooa_bench.runner import _public_json_default
from pydantic import BaseModel, Field

from nooa import hidden
from nooa.llm_types import AssistantText, LLMResponse


class Plain(BaseModel):
    public: str = "visible"
    excluded: str = Field(default="private", exclude=True)
    quiet: str = Field(default="quiet", repr=False)
    secret: Annotated[str, hidden] = "secret"


class Projected(Plain):
    def __instance_values__(self) -> dict[str, Any]:
        return {"public": "projected", "excluded": self.excluded, "secret": self.secret}


def test_public_model_projection_and_plain_fallback():
    assert _public_json_default(Plain()) == {"public": "visible"}
    assert _public_json_default(Projected()) == {"public": "projected"}


def test_nested_response_uses_public_projection_without_native_state():
    class Envelope(BaseModel):
        response: LLMResponse

    native = {"private": "native-sentinel"}
    response = LLMResponse(
        parts=(AssistantText(text="visible"),),
        raw_response=native,
        replay_scope="scope-sentinel",
    )
    encoded = json.dumps(Envelope(response=response), default=_public_json_default)
    assert json.loads(encoded)["response"]["content"] == "visible"
    assert "native-sentinel" not in encoded
    assert "scope-sentinel" not in encoded
    assert response.raw_response is native
