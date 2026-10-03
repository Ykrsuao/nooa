# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Persistent bash session for SWE tools.

Maintains a long-running bash subprocess with sentinel-based output capture.
Uses a dedicated control file descriptor (fd 3) for sentinels so that
stdout/stderr are 100% user-owned.

Architecture:
  stdin  -> bash (commands only)
  stdout <- pure command output (no sentinel parsing)
  stderr <- pure command stderr (no sentinel parsing)
  fd 3   <- exit code + cwd + sentinel (control channel)

On Windows, bash is Git for Windows' MSYS2 bash and fd 3 is a loopback TCP
connection instead of an inherited pipe; see ``_win_bash``.
"""

import asyncio
import base64
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from nooa.agentdoc import TruncatingStringIO

if sys.platform == "win32":
    from nooa.tools import _win_bash

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 30_000
"""Characters kept of each of a command's stdout and stderr: the first and last half."""
_BOUNDED_CHUNK_CHARS = 65_536  # Pieces fed to the truncating buffer by _bounded
_DRAIN_TIMEOUT = 0.05  # Seconds to wait for remaining output after sentinel
_SIGTERM_GRACE = 5.0  # Seconds to wait for sentinel after SIGTERM
_SIGKILL_GRACE = 2.0  # Seconds to wait for sentinel after SIGKILL
_CONTROL_CONNECT_TIMEOUT = 10.0  # Windows: seconds for bash to open the control socket

# Prints the session cwd on the control channel. MSYS2's ``pwd -W`` gives the
# Windows form (``E:/src``) that Path understands; plain ``pwd`` gives ``/e/src``.
PWD_COMMAND = "pwd -W" if sys.platform == "win32" else "pwd"

# str.translate table that makes text safe inside bash's $'...' quoting: escape
# the backslash and quote, and hex-escape every control character.
_ANSI_C_ESCAPES = {
    **{c: f"\\x{c:02x}" for c in [*range(0x20), 0x7F]},
    ord("\\"): "\\\\",
    ord("'"): "\\'",
}


async def _read_control_line(ctrl: asyncio.StreamReader, timeout: float) -> bytes:
    """Next control-channel line, or b"" at EOF; raises TimeoutError.

    On Windows the channel is a socket, and bash dying resets it (WinError 64)
    instead of closing it cleanly. That is EOF too.
    """
    try:
        return await asyncio.wait_for(ctrl.readline(), timeout=timeout)
    except TimeoutError:
        raise
    except OSError:
        return b""


def _parse_cwd(line: str) -> Path | None:
    """The cwd reported on the control channel, or None if it is not absolute."""
    candidate = line.strip()
    if not candidate or not Path(candidate).is_absolute():
        return None
    return Path(candidate)


def _bounded(text: str) -> str:
    """``text`` cut to ``MAX_OUTPUT_CHARS``: its head and tail around the standard notice.

    The tail matters as much as the head: a failing command usually ends
    with its error.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    buffer = TruncatingStringIO(limit=MAX_OUTPUT_CHARS)
    # Feed the buffer in bounded pieces: one write of the whole stream would
    # copy everything past the head a second time before the tail is trimmed.
    for start in range(0, len(text), _BOUNDED_CHUNK_CHARS):
        buffer.write(text[start : start + _BOUNDED_CHUNK_CHARS])
    return buffer.getvalue()


class BashSession:
    """A persistent bash shell session with dedicated control channel.

    Commands are serialized via an internal asyncio.Lock — concurrent
    ``run()`` / ``run_stream()`` calls from the same event loop will queue
    and execute one at a time.  This is safe but sequential; for true
    parallelism, create multiple BashSession instances.

    Usage::

        session = BashSession(cwd="/my/project")
        await session.start()
        stdout, stderr, code = await session.run("ls -la")
        stdout, stderr, code = await session.run("cd src && pwd")  # cd persists!
        await session.close()
    """

    def __init__(self, cwd: str | Path = ".", init_command: str | None = None) -> None:
        self._cwd = Path(cwd).resolve()
        # Optional shell snippet run once every time the session (re)starts —
        # before any user command — to set up the environment (e.g. activating a
        # conda env). Re-run on reset() because a fresh bash loses prior env.
        self._init_command = init_command
        self._running_init = False
        self._process: asyncio.subprocess.Process | None = None
        self._control_reader: asyncio.StreamReader | None = None
        self._control_transport: asyncio.BaseTransport | None = None
        self._control_writer: asyncio.StreamWriter | None = None  # Windows socket channel
        self._job = None  # Windows: _win_bash.ProcessJob holding bash's process tree
        self._started = False
        self._started_on_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        # Loop that last replaced ``_lock`` in _ensure_lock_on_current_loop.
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._last_successful_command: float | None = None
        self._last_command: str = ""
        self._start_count: int = 0
        self._close_task: asyncio.Task[None] | None = None

    @property
    def cwd(self) -> Path:
        """Current working directory of the session."""
        return self._cwd

    def __del__(self) -> None:
        """Best-effort cleanup: kill the bash subprocess if still running."""
        proc = self._process
        job = self._job
        if job is not None:
            try:
                job.close()
            except Exception:
                pass  # Win32 bindings may already be gone during shutdown.
        if proc is not None and proc.returncode is None:
            try:
                # During interpreter shutdown, module globals (os, signal) may
                # be None, causing TypeError. Broad except handles all cases.
                if sys.platform == "win32":
                    proc.kill()
                else:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def __aenter__(self) -> "BashSession":
        """Support ``async with BashSession() as session:`` usage."""
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _diagnose_death(self, context: str) -> str:
        """Capture diagnostic info about why bash died. Logs at ERROR level."""
        proc = self._process
        parts = [f"[BASH_DEATH] context={context}"]
        if self._last_successful_command is not None:
            parts.append(
                f"  last_successful_cmd_ago={time.time() - self._last_successful_command:.1f}s"
            )
        else:
            parts.append("  last_successful_cmd_ago=never")
        parts.append(f"  last_command={self._last_command[:200]!r}")
        parts.append(f"  start_count={self._start_count}")
        parts.append(f"  cwd={self._cwd}")
        if proc is None:
            parts.append("  proc=None")
        else:
            parts.append(f"  proc.pid={proc.pid}")
            parts.append(f"  proc.returncode={proc.returncode}")
            if proc.returncode is not None and proc.returncode < 0:
                sig_num = -proc.returncode
                try:
                    sig_name = signal.Signals(sig_num).name
                except (ValueError, AttributeError):
                    sig_name = f"signal {sig_num}"
                parts.append(f"  killed_by={sig_name}")
            # Try to read /proc/<pid>/status before it disappears
            try:
                with open(f"/proc/{proc.pid}/status", encoding="utf-8") as f:
                    for line in f:
                        if any(k in line for k in ("State:", "SigPnd:", "SigCgt:")):
                            parts.append(f"  /proc/status: {line.strip()}")
            except (FileNotFoundError, PermissionError, OSError):
                parts.append("  /proc/status: unavailable (process reaped)")
        # Check cwd accessibility (detects virtiofs / mount failures)
        try:
            os.stat(str(self._cwd))
            parts.append("  cwd_stat=OK")
        except OSError as e:
            parts.append(f"  cwd_stat=FAILED: {e}")
        # FD count of parent — detects FD leaks that can trigger OOM-killer
        try:
            fd_count = len(os.listdir("/proc/self/fd"))
            parts.append(f"  parent_fd_count={fd_count}")
        except OSError:
            pass
        diag = "\n".join(parts)
        logger.error(diag)
        try:
            from nooa.runtime.harness_metrics import get_harness_metrics

            get_harness_metrics().shell_death(context, diag)
        except Exception:
            pass  # telemetry must not break recovery
        return diag

    async def start(self) -> None:
        """Start the bash subprocess with a dedicated control fd.

        Concurrent callers share one startup: the session lock serializes them,
        and later callers find the session already started.
        """
        async with self._command_scope():
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        """start() for callers that already hold the lock."""
        if self._close_task is not None:
            await asyncio.shield(self._close_task)
        if self._started:
            return

        self._start_count += 1
        env = os.environ.copy()
        env["PS1"] = ""
        env["TERM"] = "dumb"

        if sys.platform == "win32":
            await self._spawn_windows(env)
        else:
            await self._spawn_posix(env)
        self._started = True
        self._started_on_loop = asyncio.get_running_loop()
        assert self._process is not None and self._process.stdin is not None

        # Drain startup — send a no-op through the control channel.
        sentinel = f"__CTRL_{secrets.token_hex(8)}__"
        self._process.stdin.write(f"echo {sentinel} >&3\n".encode())
        await self._process.stdin.drain()
        await self._read_control_until(sentinel, timeout=5.0)

        # Run the one-time init command (env setup) before any user command.
        # ``_running_init`` guards against re-entry if _send_and_wait triggers a
        # reset (which would call start() again). _send_and_wait drains
        # stdout/stderr so init output never bleeds into the first user command.
        if self._init_command and not self._running_init:
            self._running_init = True
            try:
                init_sentinel = f"__CTRL_{secrets.token_hex(8)}__"
                init_script = (
                    f"{self._init_command}\n_nemo_ec=$?\n"
                    f"echo $_nemo_ec >&3\n{PWD_COMMAND} >&3\necho {init_sentinel} >&3\n"
                )
                ctrl_lines, _out, _err, _timed = await self._send_and_wait(
                    init_script, init_sentinel, timeout=60.0
                )
                if len(ctrl_lines) >= 2 and (cwd := _parse_cwd(ctrl_lines[1])):
                    self._cwd = cwd
            finally:
                self._running_init = False

    async def _spawn_posix(self, env: dict[str, str]) -> None:
        """Start /bin/bash with the control channel on an inherited pipe (fd 3)."""
        # Create pipe for control channel (fd 3 inside bash).
        ctrl_r, ctrl_w = os.pipe()
        try:
            self._process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--norc",
                "--noprofile",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=env,
                start_new_session=True,
                pass_fds=(ctrl_w,),
            )
        except Exception:
            os.close(ctrl_r)
            os.close(ctrl_w)
            raise

        # Dup the write end to fd 3 inside bash, then close the original.
        assert self._process.stdin is not None
        self._process.stdin.write(f"exec 3>&{ctrl_w} {ctrl_w}>&-\n".encode())
        await self._process.stdin.drain()
        os.close(ctrl_w)

        # Wrap the read end in an asyncio StreamReader.
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=2**20)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            os.fdopen(ctrl_r, "rb", 0),
        )
        self._control_reader = reader
        self._control_transport = transport

    async def _spawn_windows(self, env: dict[str, str]) -> None:
        """Start MSYS2 bash in a Job Object; bash dials back the control channel.

        Windows cannot hand bash an extra fd, so bash opens fd 3 itself as a
        loopback TCP connection (``/dev/tcp``) and proves who it is with a
        one-time token. The connection reaches EOF when bash dies, like the pipe.
        """
        assert sys.platform == "win32"
        bash = _win_bash.find_bash()
        env = _win_bash.bash_env(bash, env)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            port = listener.getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                str(bash),
                "--norc",
                "--noprofile",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=env,
                # Prevent BASH_ENV and other startup code from spawning children
                # before the process joins its job. Keep console windows hidden.
                creationflags=subprocess.CREATE_NO_WINDOW | _win_bash.CREATE_SUSPENDED,
            )
            self._process = process
            try:
                self._job = _win_bash.ProcessJob()
                self._job.assign(process.pid)
                _win_bash.resume_suspended_process(process.pid)
                token = secrets.token_hex(16)
                assert process.stdin is not None
                process.stdin.write(
                    f"exec 3<>/dev/tcp/127.0.0.1/{port}; echo {token} >&3\n".encode()
                )
                await process.stdin.drain()
                reader, writer = await asyncio.wait_for(
                    self._accept_control(listener, token), timeout=_CONTROL_CONNECT_TIMEOUT
                )
            except BaseException:
                self._close_job()
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Keep ownership until shielded cleanup has reaped the process
                # and closed its pipes, even if startup is cancelled again.
                await self.close()
                raise
        finally:
            listener.close()
        self._control_reader = reader
        # Hold the writer: a StreamWriter closes its transport when collected.
        self._control_writer = writer
        self._control_transport = writer.transport

    @staticmethod
    async def _accept_control(
        listener: socket.socket, token: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Accept the first connection that presents *token*; drop any other."""
        loop = asyncio.get_running_loop()
        while True:
            conn, _ = await loop.sock_accept(listener)
            try:
                reader, writer = await asyncio.open_connection(sock=conn, limit=2**20)
            except BaseException:
                conn.close()
                raise
            authenticated = False
            try:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                except (OSError, ValueError):
                    # A reset, timeout, or oversized line from another local
                    # client must not prevent bash from connecting.
                    continue
                if line.strip() == token.encode():
                    authenticated = True
                    return reader, writer
            finally:
                if not authenticated:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

    def _close_job(self) -> None:
        """Windows: kill everything bash started and release the job."""
        job, self._job = self._job, None
        if job is not None:
            job.close()

    def _build_script(self, command: str, sentinel: str) -> str:
        """Compose the wire script: the command, then the control-channel protocol.

        The command is base64'd and decoded inside bash, so bash's parser never
        reads it as shell text. Parsing it directly is unsafe; the
        protocol lines travel on the same stdin. An unbalanced quote, paren or
        heredoc in the command will consume them as string content. A command
        that reads a bare stdin (``cat``) swallows the same lines as input;
        the redirect from /dev/null prevents this.

        The payload travels in a here-string so command length is bounded by memory
        rather than ARG_MAX.
        """
        protocol = f"_nemo_ec=$?\necho $_nemo_ec >&3\n{PWD_COMMAND} >&3\necho {sentinel} >&3\n"
        if sys.platform == "win32":
            # MSYS2 emulates fork, so the command substitution and external
            # base64 below cost ~80ms per command there. An ANSI-C quoted word
            # is decoded by bash itself and, with every newline escaped, still
            # keeps the command on one line.
            return f"eval $'{command.translate(_ANSI_C_ESCAPES)}' </dev/null\n{protocol}"
        # b64encode, not encodebytes: the latter wraps at 76 characters, and a
        # newline inside the here-string would split the payload across lines.
        blob = base64.b64encode(command.encode()).decode()
        return f'eval "$(base64 -d <<<{blob})" </dev/null\n{protocol}'

    def _ensure_lock_on_current_loop(self) -> None:
        """Recreate the lock if the event loop changed since it was created."""
        loop = asyncio.get_running_loop()
        # A caller on this loop already replaced the lock and may hold it while
        # it restarts bash; replacing it again would let the next caller in.
        if self._lock_loop is loop:
            return
        if self._started_on_loop is not None and self._started_on_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop

    @asynccontextmanager
    async def _command_scope(self) -> AsyncIterator[None]:
        self._ensure_lock_on_current_loop()
        async with self._lock:
            try:
                yield
            except BaseException:
                # A cancelled/abandoned command must not leave its readers or
                # control replies for the next caller. Restart lazily on reuse.
                await self.close()
                raise

    async def run(self, command: str, timeout: float = 30.0) -> tuple[str, str, int]:
        """Run a command and return (stdout, stderr, exit_code).

        The session persists state: cd, export, etc. carry over.
        Concurrent calls are serialized via an internal lock.

        On timeout, exit_code is 124 — same as the ``timeout(1)`` command.
        Use ``run_with_timeout_flag()`` if you need to distinguish a real
        timeout from a command that exits 124 naturally.
        """
        async with self._command_scope():
            stdout, stderr, code, _ = await self._run_unlocked(command, timeout)
            return stdout, stderr, code

    async def run_with_timeout_flag(
        self, command: str, timeout: float = 30.0
    ) -> tuple[str, str, int, bool]:
        """Like run(), but returns a 4th element: whether the command timed out."""
        async with self._command_scope():
            return await self._run_unlocked(command, timeout)

    async def _run_unlocked(self, command: str, timeout: float) -> tuple[str, str, int, bool]:
        """Actual run implementation (caller must hold self._lock).

        Returns (stdout, stderr, exit_code, timed_out).
        """
        if not self._started:
            await self._start_unlocked()
        elif self._started_on_loop is not asyncio.get_running_loop():
            await self._reset_for_loop_change()

        self._last_command = command
        sentinel = f"__CTRL_{secrets.token_hex(8)}__"

        # Command runs normally; exit code + cwd + sentinel go to fd 3.
        script = self._build_script(command, sentinel)

        ctrl_lines, stdout, stderr, timed_out = await self._send_and_wait(script, sentinel, timeout)

        # Parse control channel: [exit_code, cwd]
        # Empty ctrl_lines means bash died (EOF on control fd) → non-zero exit.
        exit_code = -1 if not ctrl_lines else 0
        if ctrl_lines:
            try:
                exit_code = int(ctrl_lines[0].strip())
            except (ValueError, IndexError):
                pass
            if len(ctrl_lines) >= 2 and (cwd := _parse_cwd(ctrl_lines[1])):
                self._cwd = cwd

        stdout, stderr = _bounded(stdout), _bounded(stderr)

        if timed_out:
            exit_code = 124
        elif ctrl_lines:
            self._last_successful_command = time.time()

        return stdout.strip(), stderr.strip(), exit_code, timed_out

    async def run_stream(
        self, command: str, timeout: float = 30.0
    ) -> AsyncGenerator[tuple[str, str]]:
        """Run a command and yield (stream_name, chunk) pairs as output arrives.

        stream_name is 'stdout' or 'stderr'. After the command finishes,
        yields ('__done__', 'exit_code,timed_out_flag') where timed_out_flag
        is '1' if the command timed out, '0' otherwise.

        Concurrent calls are serialized via an internal lock.
        """
        async with self._command_scope():
            async with aclosing(self._run_stream_unlocked(command, timeout)) as stream:
                async for item in stream:
                    try:
                        yield item
                    except GeneratorExit:
                        if item[0] == "__done__":
                            return  # The caller consumed the completion marker.
                        raise

    async def _run_stream_unlocked(
        self, command: str, timeout: float
    ) -> AsyncGenerator[tuple[str, str]]:
        """Actual run_stream implementation (caller must hold self._lock)."""
        if not self._started:
            await self._start_unlocked()
        elif self._started_on_loop is not asyncio.get_running_loop():
            await self._reset_for_loop_change()

        self._last_command = command
        sentinel = f"__CTRL_{secrets.token_hex(8)}__"
        script = self._build_script(command, sentinel)

        proc = self._process
        ctrl = self._control_reader
        if proc is None or proc.stdin is None or ctrl is None or proc.returncode is not None:
            self._diagnose_death("run_stream_pre_check")
            await self.reset()
            proc = self._process
            ctrl = self._control_reader
            if proc is None or proc.stdin is None or ctrl is None:
                raise RuntimeError("Bash session failed to restart")

        try:
            proc.stdin.write(script.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._diagnose_death(f"run_stream_write: {e}")
            await self.reset()
            proc = self._process
            ctrl = self._control_reader
            if proc is None or proc.stdin is None or ctrl is None:
                raise RuntimeError("Bash session failed to restart") from None
            try:
                proc.stdin.write(script.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e2:
                self._diagnose_death(f"run_stream_retry: {e2}")
                raise RuntimeError("Bash session recovery failed") from e2

        assert proc.stdout is not None and proc.stderr is not None

        # Read stdout/stderr concurrently, yielding chunks as they arrive,
        # while watching the control fd for the sentinel.
        stdout_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        stderr_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def _read_stream(stream, name, queue):
            try:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    queue.put_nowait((name, chunk.decode("utf-8", errors="replace")))
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            finally:
                queue.put_nowait(None)

        stdout_task = asyncio.create_task(_read_stream(proc.stdout, "stdout", stdout_queue))
        stderr_task = asyncio.create_task(_read_stream(proc.stderr, "stderr", stderr_queue))

        try:
            ctrl_lines, timed_out = await self._read_control_until(sentinel, timeout)
        finally:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        # Drain queues
        for q in (stdout_queue, stderr_queue):
            while not q.empty():
                item = q.get_nowait()
                if item is not None:
                    yield item

        # Greedy-drain remaining pipe data
        for stream, name in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), timeout=_DRAIN_TIMEOUT)
                    if not chunk:
                        break
                    yield (name, chunk.decode("utf-8", errors="replace"))
                except (TimeoutError, Exception):
                    break

        # Parse exit code
        exit_code = -1 if not ctrl_lines else 0
        if ctrl_lines:
            try:
                exit_code = int(ctrl_lines[0].strip())
            except (ValueError, IndexError):
                pass
            if len(ctrl_lines) >= 2 and (cwd := _parse_cwd(ctrl_lines[1])):
                self._cwd = cwd

        if timed_out:
            exit_code = 124
        elif ctrl_lines:
            self._last_successful_command = time.time()

        yield ("__done__", f"{exit_code},{1 if timed_out else 0}")

    async def _send_and_wait(
        self, script: str, sentinel: str, timeout: float
    ) -> tuple[list[str], str, str, bool]:
        """Write script to stdin; drain stdout/stderr while waiting for sentinel.

        Drains stdout and stderr concurrently with reading the control fd to
        prevent pipe deadlock on commands producing large output (>64KB).

        Returns (control_lines, stdout, stderr, timed_out).
        Auto-resets on dead process or broken pipe.
        """
        proc = self._process
        ctrl = self._control_reader
        if proc is None or proc.stdin is None or ctrl is None or proc.returncode is not None:
            self._diagnose_death("send_and_wait_pre_check")
            logger.warning("Bash process dead or missing — resetting session")
            await self.reset()
            proc = self._process
            ctrl = self._control_reader
            if proc is None or proc.stdin is None or ctrl is None:
                raise RuntimeError("Bash session failed to restart")

        try:
            proc.stdin.write(script.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._diagnose_death(f"send_and_wait_write: {e}")
            logger.warning("Pipe error writing to bash (%s) — resetting session", e)
            await self.reset()
            proc = self._process
            ctrl = self._control_reader
            if proc is None or proc.stdin is None or ctrl is None:
                raise RuntimeError("Bash session failed to restart") from e
            try:
                proc.stdin.write(script.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e2:
                self._diagnose_death(f"send_and_wait_retry: {e2}")
                raise RuntimeError("Bash session recovery failed") from e2

        # Drain stdout/stderr concurrently with control fd to prevent deadlock.
        assert proc.stdout is not None and proc.stderr is not None
        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []

        async def accumulate(stream: asyncio.StreamReader, buf: list[bytes]) -> None:
            """Read from stream until EOF or external cancellation."""
            try:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return
                    buf.append(chunk)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        stdout_task = asyncio.create_task(accumulate(proc.stdout, stdout_buf))
        stderr_task = asyncio.create_task(accumulate(proc.stderr, stderr_buf))

        try:
            ctrl_lines, timed_out = await self._read_control_until(sentinel, timeout)
        finally:
            # Also cancel on exceptional exits, before close() or a later
            # command can read from these same StreamReaders.
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        # Now greedy-drain remaining output (sole reader per stream, safe).
        for stream, buf in [(proc.stdout, stdout_buf), (proc.stderr, stderr_buf)]:
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.read(65536), timeout=_DRAIN_TIMEOUT)
                    if not chunk:
                        break
                    buf.append(chunk)
                except (TimeoutError, Exception):
                    break

        stdout = b"".join(stdout_buf).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_buf).decode("utf-8", errors="replace")
        return ctrl_lines, stdout, stderr, timed_out

    async def _read_control_until(self, sentinel: str, timeout: float) -> tuple[list[str], bool]:
        """Read lines from control fd until sentinel. Returns (lines, timed_out)."""
        ctrl = self._control_reader
        assert ctrl is not None
        lines: list[str] = []
        timed_out = False
        while True:
            try:
                raw = await _read_control_line(ctrl, timeout)
            except TimeoutError:
                timed_out = True
                break
            if not raw:
                self._diagnose_death("control_fd_eof")
                break  # EOF — bash died
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if sentinel in line:
                break
            lines.append(line)

        if timed_out:
            proc = self._process
            if proc is not None:
                recovered = await self._interrupt_and_recover(proc, sentinel, timeout)
                if not recovered:
                    self._diagnose_death("timeout_recovery_failed")
                    logger.warning("Timeout recovery failed — resetting session")
                    await self.reset()

        return lines, timed_out

    async def _interrupt_and_recover(
        self,
        proc: asyncio.subprocess.Process,
        sentinel: str,
        original_timeout: float,
    ) -> bool:
        """Kill child processes and wait for sentinel on control fd.

        Graduated: SIGTERM children -> 5s -> SIGINT bash -> 2s.
        """
        ctrl = self._control_reader
        assert ctrl is not None

        async def try_drain(grace: float) -> bool:
            while True:
                try:
                    raw = await _read_control_line(ctrl, grace)
                except TimeoutError:
                    return False
                if not raw:
                    return False
                if sentinel in raw.decode("utf-8", errors="replace"):
                    return True

        if sys.platform == "win32":
            # No signals: terminate every process bash started, then wait for
            # bash to finish the protocol. A busy builtin (no child process)
            # cannot be interrupted, so the caller resets the session instead.
            for grace in (_SIGTERM_GRACE, _SIGKILL_GRACE):
                job = self._job
                if job is None or not job.kill_descendants(proc.pid):
                    return False
                if await try_drain(grace):
                    return True
            return False

        async def kill_children(sig: int) -> None:
            killed_any = False
            try:
                pgrep = await asyncio.create_subprocess_exec(
                    "pgrep",
                    "-P",
                    str(proc.pid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(pgrep.communicate(), timeout=2.0)
                if stdout:
                    for pid_str in stdout.decode().split():
                        if pid_str.strip():
                            try:
                                os.kill(int(pid_str), sig)
                                killed_any = True
                            except (ProcessLookupError, OSError):
                                pass
            except (TimeoutError, OSError, FileNotFoundError):
                pass
            if not killed_any:
                # SIGINT to bash (like Ctrl-C) to break pending reads.
                try:
                    os.kill(proc.pid, signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass

        await kill_children(signal.SIGTERM)
        if await try_drain(_SIGTERM_GRACE):
            return True
        await kill_children(signal.SIGKILL)
        if await try_drain(_SIGKILL_GRACE):
            return True
        return False

    async def _reset_for_loop_change(self) -> None:
        """Reset after detecting that the event loop changed (gl-212)."""
        logger.warning("BashSession: event loop changed — resetting (env/aliases lost)")
        try:
            from nooa.runtime.harness_metrics import get_harness_metrics

            get_harness_metrics().shell_death(
                "loop_change_reset",
                f"BashSession reset due to event loop change (gl-212). "
                f"cwd={self._cwd}, start_count={self._start_count}",
            )
        except Exception:
            pass
        await self.reset()

    async def reset(self) -> None:
        """Kill the current session and start a fresh one, preserving cwd.

        Called with the lock held on recovery paths, so it must not take it.
        """
        cwd = self._cwd
        await self.close()
        self._cwd = cwd
        await self._start_unlocked()

    async def close(self) -> None:
        """Terminate the session, finishing cleanup even if the caller is cancelled."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_impl())
        task = self._close_task
        cancelled = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            task.result()
        finally:
            if task.done() and self._close_task is task:
                self._close_task = None
        if cancelled is not None:
            raise cancelled

    async def _close_impl(self) -> None:
        proc = self._process
        # Process has no public accessor for its loop or subprocess transport.
        # Use the physical owner, not _started_on_loop (the logical session).
        owner_loop: asyncio.AbstractEventLoop | None = getattr(proc, "_loop", self._started_on_loop)
        same_loop = owner_loop is asyncio.get_running_loop()
        stale_windows = (
            sys.platform == "win32" and owner_loop is not None and owner_loop.is_closed()
        )
        if self._control_transport is not None:
            if stale_windows:
                _win_bash.close_stale_pipe(self._control_transport)
            else:
                try:
                    self._control_transport.close()
                except Exception:
                    pass  # Transport may be bound to a dead loop (gl-212)
            self._control_transport = None
        writer = self._control_writer
        if writer is not None and same_loop:
            try:
                await writer.wait_closed()
            except OSError:
                pass
        self._control_writer = None
        self._control_reader = None

        if self._process is not None and self._process.returncode is None:
            if sys.platform == "win32":
                # The job holds bash and everything it started; Windows has no
                # graceful equivalent of SIGTERM for them.
                self._close_job()
                if same_loop:
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=3.0)
                    except TimeoutError:
                        pass
            elif same_loop:
                # Graceful shutdown: SIGTERM → wait → SIGKILL on timeout
                try:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except TimeoutError:
                    try:
                        pgid = os.getpgid(self._process.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
            else:
                # Cross-loop (gl-212): transport is dead, just kill immediately.
                try:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        self._process.kill()
                    except Exception:
                        pass
        # Also reaps background jobs that outlived an already-dead bash.
        self._close_job()
        if proc is not None:
            transport: asyncio.SubprocessTransport | None = getattr(proc, "_transport", None)
            assert transport is not None
            if stale_windows:
                native = transport.get_extra_info("subprocess")
                if native is not None:
                    await asyncio.to_thread(native.wait, 3.0)
                for fd in (0, 1, 2):
                    pipe = transport.get_pipe_transport(fd)
                    if pipe is not None:
                        _win_bash.close_stale_pipe(pipe)
            try:
                transport.close()
            except RuntimeError:
                if not (owner_loop is not None and owner_loop.is_closed()):
                    raise
            if same_loop and proc.stdin is not None:
                try:
                    await proc.stdin.wait_closed()
                except (OSError, BrokenPipeError):
                    pass
        self._process = None
        self._started = False
        self._started_on_loop = None
        # A fresh lock lets a later caller on another loop start cleanly. Keep
        # the current one while it is held: reset() runs close() under the
        # lock, and swapping it there would let a queued caller start a second
        # bash concurrently.
        if not self._lock.locked():
            self._lock = asyncio.Lock()
            self._lock_loop = None
