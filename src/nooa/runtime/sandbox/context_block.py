# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render the active sandbox constraints into an agent-facing context block.

Only guardrails that are actually in force are listed, so the agent adapts to
exactly what it faces (short cells under a tight timeout, no writes outside the
workspace, direct network access, supported data returns).
"""

from __future__ import annotations

from nooa.runtime.sandbox.config import SandboxConfig


def render_host_tools_block() -> str:
    """Common contract for a live Agent proxy on either native backend."""
    return (
        "- Host Agent tools: self methods, fields, skills and MCP are available through "
        "the host proxy. These tools execute OUTSIDE the cell sandbox with host permissions. "
        "They can access the network and modify real project files; shell changes persist. "
        "The cell's network/filesystem limits do not constrain host tools. "
        "Use doc(self) to discover them.\n"
        "- self.<attr> data reads return fresh snapshots. In-place changes to a snapshot, "
        "such as self.items.append(x), do not update the host. Call a host method "
        "(self.record(x)) or reassign (self.items = self.items + [x]) instead."
    )


def render_value_transfer_block() -> str:
    """Describe the shared worker-to-host data protocol without promising pickle."""
    return (
        "- Values sent to host tools or returned from a cell must be supported data "
        "snapshots: numbers, strings, bytes, containers, supported value types and "
        "declared data models. Arbitrary Python objects and callbacks cannot cross "
        "this boundary; keep them in the cell and return a summary. "
        "return_result(value) takes the value itself, not a variable name. "
        "The Out[n] history and caller-seeded session_locals are not injected into "
        "the cell namespace. Generation-method arguments remain available."
    )


def render_sandbox_block(
    config: SandboxConfig, *, cell_timeout: float | None, host_tools: bool = True
) -> str:
    """Render the active sandbox constraints (block body only).

    The context formatter wraps this in a ``<sandbox>...</sandbox>`` envelope, so
    this returns just the inner text.
    """
    lines: list[str] = [
        "Your code runs in an isolated worker process with kernel-enforced limits.",
    ]

    if cell_timeout:
        lines.append(
            f"- Wall-clock: cell deadline: {cell_timeout:g}s, with "
            f"{config.timeout_grace_s:g}s additional grace before hard-kill "
            "(a killed cell loses its output). Keep cells short and return partial results."
        )
    lines.append(
        "- Time spent waiting for host tools is excluded from the cell deadline. "
        "Each host-tool call has its own deadline: "
        + ("disabled." if config.broker_timeout_s == 0 else f"{config.broker_timeout_s:g}s.")
    )
    if config.max_cpu_seconds:
        lines.append(
            f"- CPU: {config.max_cpu_seconds}s of CPU time per worker; a runaway "
            "compute loop is terminated."
        )
    if config.max_memory_mb:
        lines.append(
            f"- Memory: {config.max_memory_mb} MiB of additional address-space headroom "
            "above the worker baseline; allocating past it raises MemoryError. "
            "This is not an absolute committed-memory limit."
        )
    if config.filesystem:
        writable = config.workspace or "(none)"
        rw = [r.path for r in config.allow if r.access == "read_write"]
        ro = [r.path for r in config.allow if r.access == "read"]
        lines.append(
            f"- Filesystem: writable path(s): {writable}"
            + (f", {', '.join(rw)}" if rw else "")
            + ". Other paths are denied unless explicitly readable"
            + (f" (extra readable: {', '.join(ro)})" if ro else "")
            + ("; interpreter and system paths remain read-only" if config.system_paths else "")
            + "."
        )
    if not config.network:
        lines.append(
            "- Network: disabled for direct cell internet sockets. Opening a socket "
            "to the internet raises PermissionError. Host tools have separate permissions."
        )
    if host_tools:
        lines.append(render_host_tools_block())
    else:
        lines.append(
            "- Agent tools: only explicitly granted tools are available. "
            "No other Agent methods or live self fields are exposed."
        )
    lines.append(render_value_transfer_block())
    lines.append(
        "- Recovery: "
        + (
            "a failed worker may be replaced with empty globals."
            if config.recovery == "restart_empty"
            else "worker replacement is disabled within this Agent call."
        )
        + " Neither replacement nor failure rolls back files or host-tool effects."
    )

    return "\n".join(lines)
