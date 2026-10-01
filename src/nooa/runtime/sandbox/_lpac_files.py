# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal exact-file broker. Grants pin file objects, not directory path prefixes."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nooa.runtime.sandbox._appcontainer import _input_name


@dataclass(frozen=True)
class _FileGrant:
    path: Path
    writable: bool = False


def _open_native_file(
    path: Path,
    writable: bool,
    *,
    directory: bool = False,
    root_handle: int | None = None,
) -> int:
    """Reject reparse traversal during the kernel open, before any target is accessed."""
    import ctypes
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import CloseHandle, _fn

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", w.ULONG),
            ("RootDirectory", w.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", w.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("StatusOrPointer", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    nt_path = str(path) if root_handle is not None else "\\??\\" + str(path)
    length = len(nt_path.encode("utf-16-le"))
    if length + 2 > 65535:
        raise ValueError("file grant path is too long")
    buffer = ctypes.create_unicode_buffer(nt_path)
    name = UnicodeString(length, length + 2, ctypes.cast(buffer, w.LPWSTR))
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        root_handle,
        ctypes.pointer(name),
        0x40 | 0x1000,
        None,
        None,  # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE
    )
    ntdll = ctypes.WinDLL("ntdll")
    create = _fn(
        ntdll,
        "NtCreateFile",
        ctypes.c_long,
        ctypes.POINTER(w.HANDLE),
        w.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        w.ULONG,
        w.ULONG,
        w.ULONG,
        w.ULONG,
        ctypes.c_void_p,
        w.ULONG,
    )
    error_code = _fn(ntdll, "RtlNtStatusToDosError", w.ULONG, ctypes.c_long)
    handle, status_block = w.HANDLE(), IoStatusBlock()
    status = create(
        ctypes.byref(handle),
        0x80000000 | 0x100000 | (0x40000000 if writable else 0),
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        0,
        3 if directory else 1,  # Directory contents may change, but no rename/delete sharing.
        1,
        (0x1 if directory else 0x40) | 0x20 | 0x00200000,
        None,
        0,  # Synchronous, open reparse itself; OBJ_DONT_REPARSE rejects traversal.
    )
    if status < 0:
        if handle.value is not None:
            CloseHandle(handle)
        raise ctypes.WinError(error_code(status))
    if handle.value is None:
        raise OSError("native file open returned no handle")
    return handle.value


def _local_path(value: Path) -> Path:
    """Require an unambiguous absolute path on a fixed local drive."""
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import _fn, _kernel

    path = Path(value)
    if (
        not path.is_absolute()
        or len(path.drive) != 2
        or path.drive[1] != ":"
        or not path.drive[0].isascii()
        or not path.drive[0].isalpha()
    ):
        raise ValueError("file grants require absolute local drive paths")
    for part in path.parts[1:]:
        _input_name(part)
    drive_type = _fn(_kernel, "GetDriveTypeW", w.UINT, w.LPCWSTR)
    if drive_type(path.anchor) != 3:  # DRIVE_FIXED, never a remote/mapped drive.
        raise ValueError("file grants require a fixed local drive")
    return path


def _verify_handle_path(handle: int, path: Path) -> None:
    """Refuse aliases using the opened object's final name, never a pre-open resolve."""
    import ctypes
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import _check, _fn, _kernel

    final_name = _fn(
        _kernel, "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD
    )
    compare = _fn(
        _kernel,
        "CompareStringOrdinal",
        ctypes.c_int,
        w.LPCWSTR,
        ctypes.c_int,
        w.LPCWSTR,
        ctypes.c_int,
        w.BOOL,
    )
    size = final_name(handle, None, 0, 0)
    _check(size)
    buffer = ctypes.create_unicode_buffer(size + 1)
    length = final_name(handle, buffer, len(buffer), 0)
    _check(length)
    if (
        length >= len(buffer)
        or compare(buffer.value.removeprefix("\\\\?\\"), -1, str(path), -1, True) != 2
    ):
        raise ValueError("file grant resolved to a different path")


def _open_pinned_file(
    grant: _FileGrant, *, root_handle: int | None = None, reject_links: bool = False
) -> int:
    """Open without truncation, validate that handle, then transfer ownership to an fd."""
    import msvcrt
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import CloseHandle, _fn, _kernel

    path = _local_path(grant.path)
    file_type = _fn(_kernel, "GetFileType", w.DWORD, w.HANDLE)
    # Relative opens use only the final component under an already pinned parent.
    handle = (
        _open_native_file(path, grant.writable)
        if root_handle is None
        else _open_native_file(Path(path.name), grant.writable, root_handle=root_handle)
    )
    try:
        if file_type(handle) != 1:  # FILE_TYPE_DISK
            raise ValueError("file grants require regular disk files")
        _verify_handle_path(handle, path)
        fd = msvcrt.open_osfhandle(
            handle, os.O_BINARY | (os.O_RDWR if grant.writable else os.O_RDONLY)
        )
        handle = None
        try:
            import stat

            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (
                info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("file grants require non-reparse regular files")
            if (grant.writable or reject_links) and info.st_nlink != 1:
                raise ValueError("file grants cannot have hard-link aliases")
            os.set_inheritable(fd, False)
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        if handle is not None:
            CloseHandle(handle)


async def _finish_file_io(callback, *args):
    # A cancelled to_thread await does not cancel the disk operation. Drain it
    # before releasing ownership or reporting cancellation to the broker caller.
    task = asyncio.create_task(asyncio.to_thread(callback, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class _FileBroker:
    """Bounded read/replace operations on explicitly named, existing local files.

    No worker-supplied paths, file creation, deletion or directory traversal.
    Grants hold non-inheritable handles and exclude other writers/replacement.
    Writes are not atomic or durable transactions. Disk I/O runs in a thread and
    is drained on cancellation, not forcibly interrupted by the cell deadline.
    The owner must await calls and aclose(); the broker is not shipped to LPAC.
    """

    def __init__(self, grants: Mapping[str, _FileGrant], *, max_file_bytes: int = 1024 * 1024):
        if sys.platform != "win32":
            raise RuntimeError("file broker requires native Windows")
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1 and 4 MiB")
        self._limit = max_file_bytes
        self._lock = threading.Lock()
        self._closed = False
        self._files: dict[str, tuple[int, bool]] = {}
        try:
            for name, grant in grants.items():
                if type(name) is not str or not name.isidentifier():
                    raise ValueError("file resource names must be identifiers")
                if not isinstance(grant, _FileGrant) or type(grant.writable) is not bool:
                    raise TypeError("file grants must declare a path and boolean writable access")
                self._files[name] = (_open_pinned_file(grant), grant.writable)
        except BaseException:
            self.close()
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _file(self, name: str, *, write: bool = False) -> int:
        if self._closed:
            raise RuntimeError("file broker is closed")
        if type(name) is not str or name not in self._files:
            raise PermissionError("file resource is not granted")
        fd, writable = self._files[name]
        if write and not writable:
            raise PermissionError("file resource is read-only")
        return fd

    async def read(self, name: str) -> bytes:
        """Read the currently granted file as a bounded byte snapshot."""
        return await _finish_file_io(self._read, name)

    def _read(self, name):
        with self._lock:
            fd = self._file(name)
            if os.fstat(fd).st_size > self._limit:
                raise ValueError("file exceeds max_file_bytes")
            os.lseek(fd, 0, os.SEEK_SET)
            data = bytearray()
            while len(data) <= self._limit:
                chunk = os.read(fd, min(65536, self._limit + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise ValueError("file exceeds max_file_bytes")

    async def write(self, name: str, data: bytes) -> int:
        """Replace an explicitly writable file, never create or reopen a path."""
        if type(data) is not bytes or len(data) > self._limit:
            raise ValueError("file data must be bytes within max_file_bytes")
        return await _finish_file_io(self._write, name, data)

    def _write(self, name, data):
        with self._lock:
            fd = self._file(name, write=True)
            os.lseek(fd, 0, os.SEEK_SET)
            pending = memoryview(data)
            while pending:
                count = os.write(fd, pending)
                if count <= 0:
                    raise OSError("file write made no progress")
                pending = pending[count:]
            os.ftruncate(fd, len(data))
            return len(data)

    def close(self):
        with self._lock:
            self._closed = True
            for fd, _ in self._files.values():
                os.close(fd)
            self._files.clear()

    async def aclose(self):
        await _finish_file_io(self.close)
