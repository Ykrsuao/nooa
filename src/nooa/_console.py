# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Formatting for human-readable diagnostics on locale-encoded streams."""

from typing import TextIO


def text_for_stream(text: str, stream: TextIO) -> str:
    """Escape unsupported characters without changing the stream or original data.

    Keep Unicode intact on UTF-8 streams and unencoded captures such as StringIO.
    This is only for diagnostic text, never serialized data or protocol messages.
    """
    encoding = getattr(stream, "encoding", None)
    if encoding is None:
        return text
    return text.encode(encoding, errors="backslashreplace").decode(encoding)
