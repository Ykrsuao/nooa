# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cross-platform exclusive advisory file locks on an open file descriptor.

POSIX uses ``fcntl.flock``. Windows uses ``msvcrt.locking``, which differs in
two ways that callers never see:

- Windows byte-range locks are mandatory: other processes cannot read locked
  bytes. Lock files here carry readable content (an owner PID), so the lock is
  taken on one byte far past any content instead of at offset 0.
- ``msvcrt.locking`` locks from the current file position, so the position is
  saved and restored around each call.

On both platforms the OS releases the lock when the owning process exits.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

if sys.platform == "win32":
    import msvcrt

    # Past any content a lock file will ever hold; locking beyond EOF is allowed.
    _LOCK_OFFSET = 0x7FFF_FFFE
    _CONTENDED = {errno.EACCES, errno.EDEADLK}
    _POLL_INTERVAL = 0.05

    def _locking(fd: int, mode: int) -> None:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        """Take an exclusive lock on *fd*; raise OSError if non-blocking and held."""
        while True:
            try:
                _locking(fd, msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                # LK_LOCK gives up after ~10s, so blocking waits are a poll loop.
                if not blocking or exc.errno not in _CONTENDED:
                    raise
            time.sleep(_POLL_INTERVAL)

    def unlock(fd: int) -> None:
        """Release a lock taken with :func:`lock_exclusive`."""
        _locking(fd, msvcrt.LK_UNLCK)

else:
    import fcntl

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        """Take an exclusive lock on *fd*; raise OSError if non-blocking and held."""
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fd: int) -> None:
        """Release a lock taken with :func:`lock_exclusive`."""
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def locked(fd: int) -> Iterator[None]:
    """Hold a blocking exclusive lock on *fd* for the duration of the block."""
    lock_exclusive(fd)
    try:
        yield
    finally:
        unlock(fd)
