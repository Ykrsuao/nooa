# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Command-line entry points for the NOOA ACP agent."""

import asyncio
import os
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from nooa.unifiedllm import UnifiedLLM


@click.command()
@click.option(
    "--model",
    envvar="NOOA_MODEL",
    required=True,
    help="LiteLLM model name or configured NOOA model alias. Or set NOOA_MODEL.",
)
@click.option(
    "--client-type",
    type=click.Choice(("completion", "responses")),
    default=None,
    help="Override the configured NOOA LLM client type.",
)
@click.option(
    "--sandbox",
    envvar="NOOA_ACP_SANDBOX",
    type=click.Choice(("off", "auto", "linux", "windows")),
    default="off",
    show_default=True,
    help="Choose the native sandbox backend. Or set NOOA_ACP_SANDBOX.",
)
@click.option(
    "--sandbox-mode",
    envvar="NOOA_ACP_SANDBOX_MODE",
    type=click.Choice(("code", "strict")),
    default="strict",
    show_default=True,
    help="code isolates generated Python while shell, skills and MCP stay on the host; strict also restricts tools.",
)
@click.option(
    "--sandbox-network",
    envvar="NOOA_ACP_SANDBOX_NETWORK",
    type=click.Choice(("off", "on")),
    default="off",
    show_default=True,
    help="Network access for generated Python in code mode. Host tools keep their own network access.",
)
def command(
    model: str,
    client_type: str | None,
    sandbox: str,
    sandbox_mode: str,
    sandbox_network: str,
) -> None:
    """Serve the NOOA coding agent over ACP on standard input/output."""
    from nooa.secrets import load_secrets_into_env
    from nooa.unifiedllm import get_llm_client
    from nooa_acp.server import serve

    if sandbox_network == "on" and (sandbox == "off" or sandbox_mode != "code"):
        raise click.UsageError("--sandbox-network on requires an enabled sandbox in code mode")
    load_secrets_into_env()
    nvidia_api_key = os.getenv("NVIDIA_API_KEY") if model.startswith("nvidia_nim/") else None

    def llm_factory() -> "UnifiedLLM":
        overrides = {"api_key": nvidia_api_key} if nvidia_api_key else {}
        return get_llm_client(model, client_type=client_type, **overrides)

    asyncio.run(
        serve(
            llm_factory,
            sandbox=sandbox,
            sandbox_mode=sandbox_mode,
            sandbox_network=sandbox_network,
        )
    )


def main() -> None:
    command()
