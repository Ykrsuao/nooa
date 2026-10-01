# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Local environment diagnostics; no model credentials or configuration writes."""

from pathlib import Path

import click


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Print a machine-readable report.")
@click.option(
    "--smoke", is_flag=True, help="Exercise shell tools in a disposable Unicode workspace."
)
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    help="Workspace to inspect (default: current directory).",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=5001,
    show_default=True,
    help="Check whether the viewer can bind this loopback port.",
)
def command(as_json: bool, smoke: bool, workspace: Path, port: int) -> None:
    """Check Python, shell tools, viewer dependencies, paths, and platform limits."""
    import json

    from nooa_cli._doctor import diagnose

    report = diagnose(workspace, port=port, smoke=smoke)
    if as_json:
        # ASCII JSON survives redirected Windows output even under cp936/cp1252.
        click.echo(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        click.echo("NOOA environment diagnostic")
        for check in report["checks"]:
            click.echo(f"[{check['status'].upper()}] {check['id']}: {check['message']}")
            if check["fix"]:
                click.echo(f"  Fix: {check['fix']}")
        click.echo(
            "Result: " + ("no blocking problems found." if report["ok"] else "action required.")
        )
    if not report["ok"]:
        raise click.exceptions.Exit(1)
