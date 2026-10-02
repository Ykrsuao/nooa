# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contracts for dynamic limit hooks and repeated reasoning validation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nooa.unifiedllm.limits import ContextLimits, context_limits_for, reduced_reply_params
from nooa.unifiedllm.reasoning import ReasoningConfig


def test_custom_limit_hooks_preserve_arguments_and_result_identity():
    limits = ContextLimits(1000, 100)
    reduced = {"max_output_tokens": 50}
    resolve = Mock(return_value=limits)
    reduce = Mock(return_value=reduced)
    client = SimpleNamespace(get_context_limits=resolve, _with_reduced_reply_limit=reduce)
    params = {"temperature": 0.5, "max_tokens": 100}
    assert context_limits_for(client, params, fallback_reserve=20) is limits
    resolve.assert_called_once_with(params, fallback_reserve=20)
    assert reduced_reply_params(client, params, 50) is reduced
    reduce.assert_called_once_with(params, 50)
    assert params == {"temperature": 0.5, "max_tokens": 100}


@pytest.mark.parametrize("hook", [None, False, "not callable"])
def test_non_callable_hooks_keep_legacy_fallback(hook: object):
    client = SimpleNamespace(
        get_context_limits=hook, _with_reduced_reply_limit=hook, context_limit=1000
    )
    assert context_limits_for(client, fallback_reserve=100) == ContextLimits(1000, 100, True)
    params = {"max_completion_tokens": 200, "temperature": 0.5}
    assert context_limits_for(client, params) == ContextLimits(1000, 200)
    assert reduced_reply_params(client, params, 50) == {
        "max_completion_tokens": 50,
        "temperature": 0.5,
    }
    assert params["max_completion_tokens"] == 200


def test_custom_hook_errors_are_not_replaced_by_fallbacks():
    client = SimpleNamespace(
        get_context_limits=Mock(side_effect=ValueError("resolver failed")),
        _with_reduced_reply_limit=Mock(side_effect=ValueError("reducer failed")),
    )
    with pytest.raises(ValueError, match="resolver failed"):
        context_limits_for(client)
    with pytest.raises(ValueError, match="reducer failed"):
        reduced_reply_params(client, {}, 50)


def test_selection_revalidates_other_levels_after_nested_mutation():
    config = ReasoningConfig(
        levels={"low": {"reasoning_effort": "low"}, "high": {"reasoning_effort": "high"}}
    )
    assert config.levels is not None
    config.levels["high"]["model"] = "injected"
    with pytest.raises(ValueError, match="reserved.*model"):
        config.settings("low")


def test_selection_revalidates_default_after_nested_mutation():
    config = ReasoningConfig(
        levels={"low": {"reasoning_effort": "low"}, "high": {"reasoning_effort": "high"}},
        default="high",
    )
    assert config.levels is not None
    del config.levels["high"]
    with pytest.raises(ValueError, match="reasoning_default must name"):
        config.settings("low")
