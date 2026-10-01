# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""LPAC process adapter for the existing parent-side worker lifecycle."""

from __future__ import annotations

import contextlib
import os
import threading

from nooa.runtime.sandbox._appcontainer import _command, _environment
from nooa.runtime.sandbox._lpac_transport import PipeConnection


class LpacProcess:
    def __init__(self, runtime, job, code, *, frame_timeout_s):
        import msvcrt

        from nooa.runtime.sandbox._win_appcontainer import SuspendedProcess

        self._native = None
        self._closed = False
        self._stderr_thread = None
        self._output_failed = threading.Event()
        self.stderr = bytearray()
        self._streams = contextlib.ExitStack()
        try:
            streams = []
            for _ in range(3):
                read, write = os.pipe()
                streams.append(
                    (
                        self._streams.enter_context(os.fdopen(read, "rb", buffering=0)),
                        self._streams.enter_context(os.fdopen(write, "wb", buffering=0)),
                    )
                )
            (stdin_r, stdin_w), (stdout_r, stdout_w), (stderr_r, stderr_w) = streams
            handles = [msvcrt.get_osfhandle(s.fileno()) for s in (stdin_r, stdout_w, stderr_w)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            self._native = SuspendedProcess(
                runtime._profile,
                _command(runtime.runtime, code),
                runtime.workspace,
                _environment(runtime.workspace),
                handles,
                job=job,
            )
            for stream in (stdin_r, stdout_w, stderr_w):
                stream.close()
            self.connection = PipeConnection(
                stdout_r,
                stdin_w,
                frame_timeout_s=frame_timeout_s,
                failure_event=self._output_failed,
            )

            def drain_errors():
                while chunk := stderr_r.read(4096):
                    if len(self.stderr) + len(chunk) > 65536:
                        self._output_failed.set()
                        return
                    self.stderr.extend(chunk)

            self._stderr_thread = threading.Thread(
                target=drain_errors, name="nooa-lpac-stderr", daemon=True
            )
            self._stderr_thread.start()
            self._native.resume()
        except BaseException:
            job.close()
            try:
                self.close()
            finally:
                self.close_streams()
            raise

    @property
    def pid(self):
        return self._native.pid if self._native is not None else None

    @property
    def exitcode(self):
        return self._native.wait(0) if self._native is not None else None

    def is_alive(self):
        return self._native is not None and not self._closed and self.exitcode is None

    def terminate(self):
        if self._native is not None:
            self._native.close()
            self._native = None

    kill = terminate

    def join(self, timeout=None):
        if self._native is not None:
            self._native.wait(0xFFFFFFFF if timeout is None else int(timeout * 1000))

    def close(self):
        self.terminate()
        if self._stderr_thread is not None:
            self._stderr_thread.join(5)
            if self._stderr_thread.is_alive():
                raise TimeoutError("LPAC stderr reader did not stop")
        # The executor drains its outstanding IPC task before closing connection
        # streams. These references are released separately in close_streams().
        self._closed = True

    def close_streams(self):
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
        self._streams.close()
