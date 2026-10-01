# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Agent-facing managed Windows policy, never a Linux SandboxConfig rendering."""

from __future__ import annotations

import json
from collections.abc import Iterable

from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy


def _names(names: Iterable[str]) -> str:
    # Names are data, not prompt markup. Do not expose host paths, URLs or payloads.
    return (
        json.dumps(sorted(names), ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
    )


def _render_windows_policy(policy: _WindowsSandboxPolicy) -> str:
    """Describe provisioned grants; the owning strategy checks session readiness."""
    lines = [
        "Your code runs in a native Windows LPAC worker, not the host Python process.",
        "- Workspace: private and disposable, "
        + ("read-only." if policy.workspace_access == "read" else "read/write.")
        + " It is not a mounted host directory. Runtime and staged packages are read-only.",
        "- Inputs: immutable byte snapshots in ../inputs, named "
        + _names(policy.inputs)
        + ". These are not live host files.",
        "- Filesystem: no direct host path grants. OS-granted resources, including "
        "registryRead, remain available; this is not a claim that all OS resources are hidden.",
        "- Network: direct internet sockets are denied. Parent tools are separate "
        "trusted capabilities and may have host-side effects.",
        "- Processes: the worker cannot launch child processes.",
    ]
    if policy.files:
        lines.append(
            "- File broker: await self.read_file(name) returns bytes; names "
            + _names(policy.files)
            + "."
        )
        writable = [name for name, grant in policy.files.items() if grant.writable]
        if writable:
            lines.append(
                "  await self.write_file(name, data) replaces an existing file with bytes; "
                "writable names " + _names(writable) + ". No creation, deletion or rename."
            )
    if policy.directories:
        lines.append(
            "- Directory broker: await self.list_directory(name, path='') lists one directory; "
            "await self.read_directory(name, path) returns bytes; names "
            + _names(policy.directories)
            + ". Paths are relative slash-separated components, without '.' or '..'; "
            "reparse entries are not traversed. Listings are not atomic snapshots."
        )
        writable = [name for name, grant in policy.directories.items() if grant.writable]
        if writable:
            lines.append(
                "  await self.write_directory(name, path, data) replaces an existing file "
                "with bytes; writable names "
                + _names(writable)
                + ". No creation, deletion or rename."
            )
        lines.append(f"  Listings are bounded to {policy.max_directory_entries} entries per call.")
    if policy.files or policy.directories:
        lines.append(
            f"- File operations are bounded to {policy.max_file_bytes} bytes per read/write. "
            "Brokered host changes are live, not worker path access. In-flight filesystem "
            "operations drain on cancellation; a deadline does not forcibly interrupt disk I/O."
        )
    if policy.https:
        lines.append(
            "- HTTPS broker: await self.fetch_https(name) returns status, content_type and "
            "body bytes; names "
            + _names(policy.https)
            + ". GET only to exact configured URLs and pinned public IPs with verified TLS. "
            "No worker-supplied URLs, headers or bodies; no redirects or compressed responses. "
            f"Response limit: {policy.max_response_bytes} bytes; "
            f"whole-request deadline: {policy.https_timeout_s:g}s."
        )
    else:
        lines.append("- HTTPS broker: no endpoints granted.")
    lines.append(
        "- Agent tools: "
        + _names(policy.tools)
        + "; argument predicates apply to "
        + _names(policy.tool_policies)
        + ". No other Agent methods or live self fields are exposed. "
        "Use doc(self) for granted tool signatures."
    )
    lines.append(
        "- Values cross the worker boundary as explicit data snapshots and declared data "
        "types, not arbitrary live host objects or callbacks. return_result(value) takes "
        "the value itself."
    )
    lines.append(
        "- Cell deadline: "
        + ("disabled." if policy.cell_timeout_s is None else f"{policy.cell_timeout_s:g}s.")
        + f" Worker startup deadline: {policy.startup_timeout_s:g}s; "
        f"IPC frame deadline: {policy.frame_timeout_s:g}s; parent-tool deadline: "
        + ("disabled." if policy.broker_timeout_s == 0 else f"{policy.broker_timeout_s:g}s.")
    )
    lines.append(
        "- Memory: "
        + (
            f"{policy.memory_limit_bytes} absolute committed bytes per worker/job."
            if policy.memory_limit_bytes
            else "no configured Job Object memory limit."
        )
        + " This is not extra address-space headroom or an RSS limit."
    )
    lines.append(
        "- CPU: "
        + (
            f"{policy.cpu_time_limit_s}s lifetime user-mode CPU per worker/job."
            if policy.cpu_time_limit_s
            else "no configured Job Object CPU limit."
        )
        + " Native budgets include startup, exclude kernel CPU and parent callbacks, "
        "and reset on worker replacement or a new Agent call."
    )
    lines.append(
        "- Recovery: "
        + (
            "a failed worker may be replaced with empty globals."
            if policy.recovery == "restart_empty"
            else "worker replacement is disabled within this Agent call."
        )
        + " New Agent calls use fresh workers/globals but share workspace files in this "
        "session. Neither replacement nor failure rolls back files or parent-tool effects. "
        "There are no cumulative session or disk quotas."
    )
    lines.append(
        "- Async runtime: tasks, timers and thread wakeups are supported, not asynchronous "
        "descriptor I/O. Await asynchronous broker methods."
    )
    return "\n".join(lines)
