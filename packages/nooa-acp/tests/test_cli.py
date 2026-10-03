# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Model selection for the ACP entry point."""

import os
import shutil
import subprocess

import click.testing
import pytest
from nooa_acp.cli import command


@pytest.fixture
def stubbed_serve(monkeypatch):
    """Capture the llm_factory the command builds instead of serving."""
    captured = {}

    def fake_serve(llm_factory, *, sandbox="off", sandbox_mode="strict", sandbox_network="off"):
        captured["llm_factory"] = llm_factory
        captured["sandbox"] = sandbox
        captured["sandbox_mode"] = sandbox_mode
        captured["sandbox_network"] = sandbox_network
        return "coroutine-placeholder"

    monkeypatch.setattr("nooa_acp.server.serve", fake_serve)
    monkeypatch.setattr("nooa_acp.cli.asyncio.run", lambda coro: coro)
    monkeypatch.setattr("nooa.secrets.load_secrets_into_env", lambda *a, **k: None)
    return captured


def test_model_is_required(monkeypatch):
    monkeypatch.delenv("NOOA_MODEL", raising=False)

    result = click.testing.CliRunner().invoke(command, [])

    # No default model: the caller has to choose one.
    assert result.exit_code == 2
    assert "--model" in result.output


def test_model_is_read_from_the_environment(monkeypatch, stubbed_serve):
    monkeypatch.setenv("NOOA_MODEL", "openai/gpt-4o-mini")
    requested = {}
    monkeypatch.setattr(
        "nooa.unifiedllm.get_llm_client",
        lambda name, **kwargs: requested.setdefault("name", name),
    )

    result = click.testing.CliRunner().invoke(command, [])

    assert result.exit_code == 0, result.output
    stubbed_serve["llm_factory"]()
    assert requested["name"] == "openai/gpt-4o-mini"


def test_explicit_flag_overrides_the_environment(monkeypatch, stubbed_serve):
    monkeypatch.setenv("NOOA_MODEL", "openai/gpt-4o-mini")
    requested = {}
    monkeypatch.setattr(
        "nooa.unifiedllm.get_llm_client",
        lambda name, **kwargs: requested.setdefault("name", name),
    )

    result = click.testing.CliRunner().invoke(command, ["--model", "anthropic/claude-sonnet-4-5"])

    assert result.exit_code == 0, result.output
    stubbed_serve["llm_factory"]()
    assert requested["name"] == "anthropic/claude-sonnet-4-5"


@pytest.mark.parametrize("sandbox", ["off", "auto", "linux", "windows"])
def test_sandbox_selection_is_passed_to_server(stubbed_serve, sandbox):
    result = click.testing.CliRunner().invoke(command, ["--model", "test", "--sandbox", sandbox])
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox"] == sandbox


def test_sandbox_defaults_to_off(stubbed_serve):
    result = click.testing.CliRunner().invoke(command, ["--model", "test"])
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox"] == "off"
    assert stubbed_serve["sandbox_mode"] == "strict"
    assert stubbed_serve["sandbox_network"] == "off"


def test_code_mode_and_network_are_passed_to_server(stubbed_serve):
    result = click.testing.CliRunner().invoke(
        command,
        [
            "--model",
            "test",
            "--sandbox",
            "auto",
            "--sandbox-mode",
            "code",
            "--sandbox-network",
            "on",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox_mode"] == "code"
    assert stubbed_serve["sandbox_network"] == "on"


def test_code_mode_and_network_can_be_configured_from_environment(stubbed_serve, monkeypatch):
    monkeypatch.setenv("NOOA_ACP_SANDBOX", "auto")
    monkeypatch.setenv("NOOA_ACP_SANDBOX_MODE", "code")
    monkeypatch.setenv("NOOA_ACP_SANDBOX_NETWORK", "on")
    result = click.testing.CliRunner().invoke(command, ["--model", "test"])
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox_mode"] == "code"
    assert stubbed_serve["sandbox_network"] == "on"


@pytest.mark.parametrize("sandbox,mode", [("off", "code"), ("off", "strict"), ("auto", "strict")])
def test_network_on_rejects_disabled_sandbox_and_strict_mode(stubbed_serve, sandbox, mode):
    result = click.testing.CliRunner().invoke(
        command,
        [
            "--model",
            "test",
            "--sandbox",
            sandbox,
            "--sandbox-mode",
            mode,
            "--sandbox-network",
            "on",
        ],
    )
    assert result.exit_code == 2
    assert "requires an enabled sandbox in code mode" in result.output
    assert "llm_factory" not in stubbed_serve


def test_sandbox_can_be_enabled_from_environment(stubbed_serve, monkeypatch):
    monkeypatch.setenv("NOOA_ACP_SANDBOX", "auto")
    result = click.testing.CliRunner().invoke(command, ["--model", "test"])
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox"] == "auto"


def test_explicit_sandbox_flag_overrides_environment(stubbed_serve, monkeypatch):
    monkeypatch.setenv("NOOA_ACP_SANDBOX", "auto")
    result = click.testing.CliRunner().invoke(command, ["--model", "test", "--sandbox", "off"])
    assert result.exit_code == 0, result.output
    assert stubbed_serve["sandbox"] == "off"


def _console_script() -> str:
    """Locate the installed ``nooa-acp`` console script.

    Deliberately fails rather than skips. Every other test in this package
    imports ``nooa_acp``, so the package is always installed when these run; a
    missing script means the ``[project.scripts]`` entry is broken, which is
    exactly the breakage this test exists to catch.
    """
    path = shutil.which("nooa-acp")
    assert path is not None, "nooa-acp console script is not installed — check [project.scripts]"
    return path


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("NOOA_MODEL", None)
    return env


def test_console_script_is_installed_and_runnable():
    # Covers the [project.scripts] -> nooa_acp.cli:main binding, which the
    # in-process CliRunner tests and the fake_agent fixture both bypass.
    result = subprocess.run(
        [_console_script(), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert "Serve the NOOA coding agent over ACP" in result.stdout


def test_console_script_requires_a_model():
    # Also proves main() reaches the click command: without the wiring this
    # would not produce a usage error.
    result = subprocess.run(
        [_console_script()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=_clean_env(),
    )

    assert result.returncode == 2
    assert "--model" in result.stderr
