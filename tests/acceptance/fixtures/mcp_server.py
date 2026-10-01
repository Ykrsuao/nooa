# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Local stdio MCP tool for testing Unicode request and response transport."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("acceptance")


@mcp.tool()
def echo(value: str) -> str:
    """Return the supplied text without modification."""
    return value


if __name__ == "__main__":
    mcp.run(transport="stdio")
