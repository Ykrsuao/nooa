# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The typed top-level experimental export remains a warning factory."""

import warnings

import pytest


def test_reflexion_export_preserves_factory_and_warning():
    import nooa
    from nooa.experimental import ReflexionStrategy as factory
    from nooa.strategies.reflexion import ReflexionStrategy as strategy_class

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from nooa import ReflexionStrategy

    assert not caught
    assert ReflexionStrategy is factory
    assert "ReflexionStrategy" in nooa.__all__
    with pytest.warns(FutureWarning, match="experimental"):
        instance = ReflexionStrategy()
    assert isinstance(instance, strategy_class)
