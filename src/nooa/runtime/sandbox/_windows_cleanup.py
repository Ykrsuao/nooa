# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded, synchronous removal of an already-authorized private Windows tree."""

from __future__ import annotations

import os
import shutil
import stat
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _remove_tree(path: Path, *, onexc=None) -> None:
    """Remove independent subtrees concurrently, joining all I/O before returning.

    The caller must validate ownership and retain any recovery directory pins.
    Reparse directories are unlinked with os.rmdir, never enumerated here.
    The final root removal still uses the caller's
    onexc handler so a pinned recovery root can be deleted by its native handle.
    """
    from nooa.runtime.sandbox._appcontainer import _long_path

    path = _long_path(Path(path))

    def checked(function, target):
        try:
            function(target)
        except OSError as exc:
            if onexc is None:
                raise
            onexc(function, str(target), exc)

    # Keep stdlib behavior for a reparse root (including refusing symlink roots).
    if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        shutil.rmtree(path, onexc=onexc)
        return

    # Root/runtime/packages/package is too coarse for large dependency trees.
    # One more level splits e.g. litellm's independent resource directories.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="nooa-cleanup") as pool:
        pending = deque()
        directories = []

        def submit(function, *args, **kwargs):
            if len(pending) >= 32:
                pending.popleft().result()
            pending.append(pool.submit(function, *args, **kwargs))

        def visit(directory, depth):
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                if onexc is None:
                    raise
                onexc(os.scandir, str(directory), exc)
                return
            for entry in entries:
                target = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        submit(checked, os.rmdir, target)
                    elif depth < 3:
                        visit(target, depth + 1)
                    else:
                        submit(shutil.rmtree, target, onexc=onexc)
                else:
                    submit(checked, os.unlink, target)
            directories.append(directory)

        visit(path, 0)
        for future in pending:
            future.result()
    # No worker can still hold a child open, even when an earlier worker failed.
    for directory in directories:
        checked(os.rmdir, directory)
