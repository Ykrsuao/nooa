# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded frames over inherited anonymous pipes, not sockets or named brokers."""

from __future__ import annotations

import concurrent.futures
import io
import struct
import time
from multiprocessing.connection import _ConnectionBase


class PipeConnection(_ConnectionBase):
    """Connection-compatible framing; only the child may call pickle ``recv``.

    Frame deadlines include partial headers/bodies and stalled writes. The owner
    must terminate the peer before closing, to unblock any pending writer.
    """

    MAX_FRAME = 32 * 1024 * 1024

    def __init__(self, reader, writer, *, frame_timeout_s=5.0, failure_event=None):
        super().__init__(reader.fileno())
        self._reader = reader
        self._writer = writer
        self._frame_timeout_s = frame_timeout_s
        self._failure_event = failure_event
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nooa-lpac-send"
        )

    def _available(self):
        import _winapi
        import msvcrt

        if self._failure_event is not None and self._failure_event.is_set():
            raise OSError("LPAC raw stderr exceeded its byte limit")
        return _winapi.PeekNamedPipe(msvcrt.get_osfhandle(self._reader.fileno()))[0]

    def _poll(self, timeout):
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._available():
                return True
            if end is not None and time.monotonic() >= end:
                return False
            time.sleep(0.005)

    def _read_exact(self, size, end):
        chunks = []
        while size:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("LPAC partial frame deadline exceeded")
            available = self._available()
            if not available:
                time.sleep(min(0.005, remaining))
                continue
            chunk = self._reader.read(min(size, available, 65536))
            if not chunk:
                raise EOFError
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _recv_bytes(self, maxsize=None):
        self._poll(None)
        end = time.monotonic() + self._frame_timeout_s
        size = struct.unpack("!I", self._read_exact(4, end))[0]
        if size > self.MAX_FRAME or (maxsize is not None and size > maxsize):
            raise OSError("LPAC frame exceeds its byte limit")
        return io.BytesIO(self._read_exact(size, end))

    def _send_bytes(self, buf):
        if len(buf) > self.MAX_FRAME:
            raise OSError("LPAC frame exceeds its byte limit")

        def write():
            for block in (struct.pack("!I", len(buf)), buf):
                view = memoryview(block)
                while view:
                    count = self._writer.write(view)
                    if not count:
                        raise BrokenPipeError
                    view = view[count:]

        try:
            self._pool.submit(write).result(timeout=self._frame_timeout_s)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("LPAC write deadline exceeded") from None

    def _close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._reader.close()
        self._writer.close()
