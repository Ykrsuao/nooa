# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prerequisite reporting must never become a containment claim or launch."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from nooa.runtime.sandbox import _windows_capabilities as capabilities


@pytest.mark.parametrize("platform", ["linux", "darwin", "cygwin"])
def test_non_windows_probe_does_not_load_native_bindings(monkeypatch, platform):
    monkeypatch.setattr(capabilities, "sys", SimpleNamespace(platform=platform))

    def unexpected(name):
        pytest.fail(f"native bindings must not load on {platform}: {name}")

    monkeypatch.setattr(capabilities, "import_module", unexpected)
    report = capabilities.probe_windows_sandbox()
    assert not report.native_windows
    assert not report.native_api_available
    assert not report.containment_verified
    assert "requires native Windows" in report.detail


def test_windows_probe_only_loads_bindings_without_using_resources(monkeypatch):
    monkeypatch.setattr(capabilities, "sys", SimpleNamespace(platform="win32"))
    imports = []

    class UnusableBindings:
        def __getattr__(self, name):
            pytest.fail(f"a prerequisite probe must not use native resources: {name}")

    def load(name):
        imports.append(name)
        return UnusableBindings()

    monkeypatch.setattr(capabilities, "import_module", load)
    report = capabilities.probe_windows_sandbox()
    assert report.native_windows
    assert report.native_api_available
    assert imports == ["nooa.runtime.sandbox._win_appcontainer", "nooa._win_job"]
    assert not report.containment_verified
    assert "No session was started" in report.detail
    assert "containment is not verified" in report.detail


@pytest.mark.parametrize(
    "failed_module", ["nooa.runtime.sandbox._win_appcontainer", "nooa._win_job"]
)
@pytest.mark.parametrize(
    "error", [ImportError("missing binding"), OSError("missing DLL"), AttributeError("missing API")]
)
def test_native_binding_failure_is_a_conservative_report(monkeypatch, failed_module, error):
    monkeypatch.setattr(capabilities, "sys", SimpleNamespace(platform="win32"))

    def load(name):
        if name == failed_module:
            raise error
        return SimpleNamespace()

    monkeypatch.setattr(capabilities, "import_module", load)
    report = capabilities.probe_windows_sandbox()
    assert report.native_windows
    assert not report.native_api_available
    assert not report.containment_verified
    assert type(error).__name__ in report.detail
    assert str(error) in report.detail
    assert "No session was started" in report.detail


@pytest.mark.parametrize("attribute", ["native_api_available", "containment_verified"])
def test_report_cannot_be_changed_to_claim_verified_containment(attribute):
    report = capabilities.WindowsSandboxCapabilities(True, True, "bindings loaded")
    with pytest.raises(FrozenInstanceError):
        setattr(report, attribute, True)
