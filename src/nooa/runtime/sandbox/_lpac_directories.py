# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal host-directory broker. No host ACL edits or direct worker path grants."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nooa.runtime.sandbox._appcontainer import _input_name
from nooa.runtime.sandbox._lpac_files import (
    _FileGrant,
    _finish_file_io,
    _local_path,
    _mark_created_file_for_deletion,
    _open_native_file,
    _open_pinned_file,
    _verify_handle_path,
)


@dataclass(frozen=True)
class _DirectoryGrant:
    path: Path
    writable: bool = False


def _relative_parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if type(path) is not str:
        raise TypeError("directory paths must be strings")
    if path == "" and allow_root:
        return ()
    parts = path.split("/")
    if len(parts) > 32 or len(path) > 8192:
        raise ValueError("directory path exceeds the component or length limit")
    for part in parts:
        _input_name(part)
    return tuple(parts)


class _PinnedDirectory:
    def __init__(self, path: Path, *, parent: _PinnedDirectory | None = None):
        import ctypes
        from ctypes import wintypes as w

        from nooa.runtime.sandbox._win_appcontainer import CloseHandle, _check, _fn, _kernel

        self.path = path
        self.handle = _open_native_file(
            Path(path.name) if parent is not None else path,
            False,
            directory=True,
            root_handle=parent.handle if parent is not None else None,
        )
        try:
            query = _fn(
                _kernel,
                "GetFileInformationByHandleEx",
                w.BOOL,
                w.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                w.DWORD,
            )
            info = (w.DWORD * 2)()
            _check(query(self.handle, 9, info, ctypes.sizeof(info)))  # FileAttributeTagInfo
            # OPEN_REPARSE_POINT may open the leaf itself without traversing it.
            if not info[0] & 0x10 or info[0] & 0x400:
                raise PermissionError("directory grants require non-reparse directories")
            _verify_handle_path(self.handle, path)
        except BaseException:
            CloseHandle(self.handle)
            raise

    def close(self):
        from nooa.runtime.sandbox._win_appcontainer import CloseHandle

        if self.handle is not None:
            CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _entries(directory: _PinnedDirectory, limit: int) -> list[dict[str, str]]:
    """Enumerate the pinned object, not its path; each native page is bounded."""
    import ctypes
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import _fn, _kernel

    class Entry(ctypes.Structure):
        _fields_ = [
            ("next", w.DWORD),
            ("index", w.DWORD),
            ("created", ctypes.c_int64),
            ("accessed", ctypes.c_int64),
            ("written", ctypes.c_int64),
            ("changed", ctypes.c_int64),
            ("size", ctypes.c_int64),
            ("allocated", ctypes.c_int64),
            ("attributes", w.DWORD),
            ("name_bytes", w.DWORD),
            ("ea_size", w.DWORD),
            ("short_length", ctypes.c_byte),
            ("short_name", w.WCHAR * 12),
            ("file_id", ctypes.c_int64),
        ]

    query = _fn(
        _kernel,
        "GetFileInformationByHandleEx",
        w.BOOL,
        w.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        w.DWORD,
    )
    results = []
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(65536)
        if not query(directory.handle, 11 if restart else 10, buffer, len(buffer)):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return sorted(results, key=lambda entry: (entry["name"].casefold(), entry["name"]))
            raise ctypes.WinError(error)
        restart = False
        raw = buffer.raw
        offset = 0
        while True:
            if offset + ctypes.sizeof(Entry) > len(buffer):
                raise OSError("invalid directory enumeration record")
            entry = Entry.from_buffer(buffer, offset)
            end = offset + ctypes.sizeof(Entry) + entry.name_bytes
            if not entry.name_bytes or entry.name_bytes % 2 or end > len(buffer):
                raise OSError("invalid directory enumeration name")
            name = raw[offset + ctypes.sizeof(Entry) : end].decode("utf-16-le")
            if name not in (".", ".."):
                if len(results) >= limit:
                    raise ValueError("directory exceeds max_entries")
                _input_name(name)
                kind = (
                    "reparse"
                    if entry.attributes & 0x400
                    else "directory"
                    if entry.attributes & 0x10
                    else "file"
                )
                results.append({"name": name, "kind": kind})
            if not entry.next:
                break
            if entry.next < ctypes.sizeof(Entry) + entry.name_bytes:
                raise OSError("invalid directory enumeration offset")
            offset += entry.next


class _DirectoryBroker:
    """Bounded live listing/read/replace of existing files under named directories.

    Roots and ancestors remain pinned without delete sharing. Each operation pins
    descendant directories and opens the final file relative to its parent handle,
    rejecting reparses, aliases and files with multiple links at open. Files exclude other
    writers/replacement only during I/O, not for the broker's whole lifetime.
    Same-user host code is trusted: Windows sharing flags do not stop it from
    adding hardlinks after the check. This is not isolation from a hostile host.
    Explicit creation uses FILE_CREATE under a pinned existing parent and never
    replaces an existing object. No directory creation, delete, rename or durable
    transaction is supplied.
    Directory listings are not snapshots against concurrent trusted host edits.

    Only explicitly granted callbacks expose these methods to an LPAC worker.
    Disk I/O is serialized and drained on cancellation before releasing handles.
    The owner must await outstanding calls and aclose(); no host ACL is changed.
    """

    def __init__(
        self,
        grants: Mapping[str, _DirectoryGrant],
        *,
        max_file_bytes: int = 1024 * 1024,
        max_entries: int = 512,
    ):
        if sys.platform != "win32":
            raise RuntimeError("directory broker requires native Windows")
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1 and 4 MiB")
        if type(max_entries) is not int or not 0 < max_entries <= 4096:
            raise ValueError("max_entries must be between 1 and 4096")
        self._limit = max_file_bytes
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._closed = False
        self._pins = contextlib.ExitStack()
        self._roots: dict[str, tuple[_PinnedDirectory, bool]] = {}
        try:
            for name, grant in grants.items():
                if type(name) is not str or not name.isidentifier():
                    raise ValueError("directory resource names must be identifiers")
                if not isinstance(grant, _DirectoryGrant) or type(grant.writable) is not bool:
                    raise TypeError("directory grants require a path and boolean writable access")
                path = _local_path(grant.path)
                if path == Path(path.anchor):
                    raise ValueError("directory grants cannot authorize a drive root")
                parent = None
                for component in (*reversed(path.parents), path):
                    parent = self._pins.enter_context(_PinnedDirectory(component, parent=parent))
                assert parent is not None
                self._roots[name] = (parent, grant.writable)
        except BaseException:
            self.close()
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _root(self, name: str, *, write: bool = False) -> _PinnedDirectory:
        if self._closed:
            raise RuntimeError("directory broker is closed")
        if type(name) is not str or name not in self._roots:
            raise PermissionError("directory resource is not granted")
        root, writable = self._roots[name]
        if write and not writable:
            raise PermissionError("directory resource is read-only")
        return root

    def _descend(self, stack, root, parts):
        for part in parts:
            root = stack.enter_context(_PinnedDirectory(root.path / part, parent=root))
        return root

    async def list(self, name: str, path: str = "") -> list[dict[str, str]]:
        """List one directory; reparse entries are labeled, never traversed."""
        return await _finish_file_io(self._list, name, path)

    def _list(self, name, path):
        with self._lock, contextlib.ExitStack() as stack:
            root = self._root(name)
            parts = _relative_parts(path, allow_root=True)
            directory = self._descend(stack, root, parts)
            return _entries(directory, self._max_entries)

    async def read(self, name: str, path: str) -> bytes:
        """Read an existing regular file by a slash-separated relative path."""
        return await _finish_file_io(self._read, name, path)

    def _file(self, stack, name, path, *, write=False, create=False):
        root = self._root(name, write=write)
        parts = _relative_parts(path)
        parent = self._descend(stack, root, parts[:-1])
        fd = _open_pinned_file(
            _FileGrant(parent.path / parts[-1], writable=write),
            root_handle=parent.handle,
            reject_links=True,
            create=create,
        )
        stack.callback(os.close, fd)
        return fd

    def _read(self, name, path):
        with self._lock, contextlib.ExitStack() as stack:
            fd = self._file(stack, name, path)
            return self._read_fd(fd)

    def _read_fd(self, fd):
        if os.fstat(fd).st_size > self._limit:
            raise ValueError("file exceeds max_file_bytes")
        data = bytearray()
        while len(data) <= self._limit:
            chunk = os.read(fd, min(65536, self._limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError("file exceeds max_file_bytes")

    async def write(self, name: str, path: str, data: bytes) -> int:
        """Replace only an existing single-link file in a writable directory grant."""
        if type(data) is not bytes or len(data) > self._limit:
            raise ValueError("file data must be bytes within max_file_bytes")
        return await _finish_file_io(self._write, name, path, data)

    def _write(self, name, path, data):
        with self._lock, contextlib.ExitStack() as stack:
            fd = self._file(stack, name, path, write=True)
            return self._write_fd(fd, data)

    @staticmethod
    def _write_fd(fd, data):
        pending = memoryview(data)
        while pending:
            count = os.write(fd, pending)
            if count <= 0:
                raise OSError("file write made no progress")
            pending = pending[count:]
        os.ftruncate(fd, len(data))
        return len(data)

    async def create(self, name: str, path: str, data: bytes) -> int:
        """Create a new regular file under an existing writable granted directory.

        Existing files, links and directories are refused without truncation.
        """
        if type(data) is not bytes or len(data) > self._limit:
            raise ValueError("file data must be bytes within max_file_bytes")
        return await _finish_file_io(self._create, name, path, data)

    def _create(self, name, path, data):
        import msvcrt

        with self._lock, contextlib.ExitStack() as stack:
            fd = self._file(stack, name, path, write=True, create=True)
            try:
                return self._write_fd(fd, data)
            except BaseException:
                _mark_created_file_for_deletion(msvcrt.get_osfhandle(fd))
                raise

    async def update(self, name: str, path: str, expected: bytes, data: bytes) -> int:
        """Replace a file only if its bounded contents still match the prior read."""
        if any(type(value) is not bytes or len(value) > self._limit for value in (expected, data)):
            raise ValueError("file data must be bytes within max_file_bytes")
        return await _finish_file_io(self._update, name, path, expected, data)

    def _update(self, name, path, expected, data):
        with self._lock, contextlib.ExitStack() as stack:
            fd = self._file(stack, name, path, write=True)
            if self._read_fd(fd) != expected:
                raise ValueError("file changed since it was read; read again before editing")
            os.lseek(fd, 0, os.SEEK_SET)
            return self._write_fd(fd, data)

    def close(self):
        with self._lock:
            self._closed = True
            self._pins.close()
            self._roots.clear()

    async def aclose(self):
        await _finish_file_io(self.close)
