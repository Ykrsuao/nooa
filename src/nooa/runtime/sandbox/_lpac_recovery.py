# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in, host-private ownership records for internal LPAC runtimes.

Only committed records in an explicitly selected recovery directory authorize
recovery. Exclusive, non-inherited file handles are the liveness test, not PIDs.
Other same-user host code is trusted; LPAC code cannot change this ledger.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from ctypes import wintypes as w
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from nooa.runtime.sandbox import _win_appcontainer as native
from nooa.runtime.sandbox._lpac_files import _FileGrant, _open_pinned_file

_ENTRY = re.compile(r"runtime-[0-9a-f]{32}\Z")
_MAX_RECORD = 16384
_ntdll = ctypes.WinDLL("ntdll")


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", w.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", w.ULONG),
        ("root", w.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", w.ULONG),
        ("security", ctypes.c_void_p),
        ("quality", ctypes.c_void_p),
    ]


class _IoStatus(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("written", w.FILETIME),
        ("volume", w.DWORD),
        ("size_high", w.DWORD),
        ("size_low", w.DWORD),
        ("links", w.DWORD),
        ("index_high", w.DWORD),
        ("index_low", w.DWORD),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _Acl(ctypes.Structure):
    _fields_ = [
        ("revision", w.BYTE),
        ("reserved", w.BYTE),
        ("size", w.USHORT),
        ("count", w.USHORT),
        ("reserved2", w.USHORT),
    ]


_NtCreateFile = native._fn(
    _ntdll,
    "NtCreateFile",
    ctypes.c_long,
    ctypes.POINTER(w.HANDLE),
    w.ULONG,
    ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatus),
    ctypes.c_void_p,
    w.ULONG,
    w.ULONG,
    w.ULONG,
    w.ULONG,
    ctypes.c_void_p,
    w.ULONG,
)
_DosError = native._fn(_ntdll, "RtlNtStatusToDosError", w.ULONG, ctypes.c_long)
_GetFileInfo = native._fn(
    native._kernel, "GetFileInformationByHandle", w.BOOL, w.HANDLE, ctypes.POINTER(_FileInfo)
)
_SetFileInfo = native._fn(
    native._kernel,
    "SetFileInformationByHandle",
    w.BOOL,
    w.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    w.DWORD,
)
_CreateDirectory = native._fn(
    native._kernel, "CreateDirectoryW", w.BOOL, w.LPCWSTR, ctypes.POINTER(_SecurityAttributes)
)
_GetSecurityInfo = native._fn(
    native._security,
    "GetSecurityInfo",
    w.DWORD,
    w.HANDLE,
    ctypes.c_int,
    w.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
)
_GetControl = native._fn(
    native._security,
    "GetSecurityDescriptorControl",
    w.BOOL,
    ctypes.c_void_p,
    ctypes.POINTER(w.USHORT),
    ctypes.POINTER(w.DWORD),
)
_GetAce = native._fn(
    native._security,
    "GetAce",
    w.BOOL,
    ctypes.c_void_p,
    w.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
)


class _Directory:
    """Pin a non-reparse directory, excluding rename/deletion by other handles."""

    def __init__(self, path: Path, *, delete: bool = False, security: bool = False):
        text = "\\??\\" + str(path)
        length = len(text.encode("utf-16-le"))
        if length + 2 > 65535:
            raise ValueError("recovery path is too long")
        buffer = ctypes.create_unicode_buffer(text)
        name = _UnicodeString(length, length + 2, ctypes.cast(buffer, w.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), None, ctypes.pointer(name), 0x1040, None, None
        )
        handle, status = w.HANDLE(), _IoStatus()
        result = _NtCreateFile(
            ctypes.byref(handle),
            0x100080 | (0x10000 if delete else 0) | (0x20000 if security else 0),
            ctypes.byref(attributes),
            ctypes.byref(status),
            None,
            0,
            3,
            1,
            0x200021,
            None,
            0,
        )
        if result < 0:
            if handle.value is not None:
                native.CloseHandle(handle)
            raise ctypes.WinError(_DosError(result))
        if handle.value is None:
            raise OSError("directory open returned no handle")
        self.handle: int | None = handle.value
        self.path = path

    def identity(self) -> list[int]:
        info = _FileInfo()
        native._check(_GetFileInfo(self.handle, ctypes.byref(info)))
        return [info.volume, (info.index_high << 32) | info.index_low]

    def delete_empty(self) -> None:
        disposition = ctypes.c_ubyte(1)
        native._check(_SetFileInfo(self.handle, 4, ctypes.byref(disposition), 1))

    def close(self) -> None:
        if self.handle is not None:
            native.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _private_directory(path: Path, user: str) -> None:
    descriptor = ctypes.c_void_p()
    native._check(
        native._ConvertSD(
            f"O:{user}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{user})",
            1,
            ctypes.byref(descriptor),
            None,
        )
    )
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        if not _CreateDirectory(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS; validate instead of changing its ACL.
                raise ctypes.WinError(error)
    finally:
        native.LocalFree(descriptor)


def _verify_private(directory: _Directory, user: str) -> None:
    owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
    error = _GetSecurityInfo(
        directory.handle,
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = w.USHORT(), w.DWORD()
        native._check(_GetControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        if native._sid_text(owner) != user or not control.value & 0x1000 or not dacl:
            raise PermissionError("recovery directory must have a protected owner-only ACL")
        entries = []
        for index in range(ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents.count):
            ace = ctypes.c_void_p()
            native._check(_GetAce(dacl, index, ctypes.byref(ace)))
            assert ace.value is not None
            header = (w.BYTE * 4).from_address(ace.value)
            mask = w.DWORD.from_address(ace.value + 4).value
            if header[0] != 0 or header[1] != 3 or mask != 0x1F01FF:
                raise PermissionError("recovery directory has an unexpected access rule")
            entries.append(native._sid_text(ctypes.c_void_p(ace.value + 8)))
        if sorted(entries) != sorted([user, "S-1-5-18"]):
            raise PermissionError("recovery directory grants access to other identities")
    finally:
        native.LocalFree(descriptor)


def _store(directory: Path) -> tuple[Path, contextlib.ExitStack]:
    from nooa.runtime.sandbox._appcontainer import _input_name

    path = Path(directory)
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":":
        raise ValueError("recovery directory must be an absolute local-drive path")
    for part in path.parts[1:]:
        _input_name(part)
    drive_type = native._fn(native._kernel, "GetDriveTypeW", w.UINT, w.LPCWSTR)
    if drive_type(path.anchor) != 3 or path == Path(path.anchor):
        raise ValueError("recovery directory must be on a fixed local drive")
    pins = contextlib.ExitStack()
    try:
        for parent in reversed(path.parents):
            pins.enter_context(_Directory(parent))
        user = native._current_user_sid()
        _private_directory(path, user)
        _verify_private(pins.enter_context(_Directory(path, security=True)), user)
        return path, pins
    except BaseException:
        pins.close()
        raise


def _lease(path: Path, *, create: bool = False, wait_s: float = 0) -> int:
    if create:
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            pass
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return _open_pinned_file(_FileGrant(path, writable=True))
        except OSError as exc:
            if exc.winerror != 32 or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def _store_lock(store: Path) -> Iterator[None]:
    fd = _lease(store / "recovery.lock", create=True, wait_s=30)
    try:
        yield
    finally:
        os.close(fd)


def _write_record(entry: Path, record: dict[str, Any]) -> None:
    temporary = entry / f"owner-{uuid.uuid4().hex}.tmp"
    data = json.dumps(record, ensure_ascii=True).encode("ascii")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(entry / "owner.json")


def _read_record(entry: Path) -> dict[str, Any]:
    fd = _open_pinned_file(_FileGrant(entry / "owner.json"))
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(fd).st_nlink != 1:
            raise ValueError("recovery record must not have hard-link aliases")
        data = stream.read(_MAX_RECORD + 1)
    if len(data) > _MAX_RECORD:
        raise ValueError("recovery record exceeds its byte limit")
    record = json.loads(data)
    if (
        type(record) is not dict
        or set(record) != {"version", "entry", "identity", "profile"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["entry"] != entry.name
        or type(record["identity"]) is not list
        or len(record["identity"]) != 2
        or any(type(value) is not int or value < 0 for value in record["identity"])
        or (
            record["profile"] is not None
            and record["profile"] != "nooa.lpac." + entry.name.removeprefix("runtime-")
        )
    ):
        raise ValueError("invalid LPAC ownership record")
    return record


def _remove_pinned_tree(directory: _Directory) -> None:
    from nooa.runtime.sandbox._appcontainer import _long_path

    target = _long_path(directory.path)

    # rmtree on Windows unlinks junctions. Keep the root pinned until its final
    # removal, then delete that exact directory through the owned native handle.
    def on_error(function, path, exc):
        if function is os.rmdir and Path(path) == target and exc.winerror == 32:
            directory.delete_empty()
            return
        raise exc

    shutil.rmtree(target, onexc=on_error)


def _delete_profile(name: str) -> None:
    result = native._DeleteProfile(name)
    if result & 0xFFFFFFFF not in (0, 0x80070002, 0x80070003):
        native._hresult(result)


class _RuntimeLease:
    def __init__(self, store, pins, entry, fd, record):
        self.store = store
        self._pins = pins
        self._entry = entry
        self._fd = fd
        self._record = record
        self.root = entry.path / "payload"

    @property
    def profile_name(self) -> str:
        return "nooa.lpac." + self._entry.path.name.removeprefix("runtime-")

    @classmethod
    def create(cls, directory: Path):
        store, pins = _store(directory)
        fd = None
        entry = None
        try:
            with _store_lock(store):
                path = store / ("runtime-" + uuid.uuid4().hex)
                path.mkdir()
                entry = pins.enter_context(_Directory(path, delete=True))
                fd = _lease(path / "lease", create=True)
                root = path / "payload"
                root.mkdir()
                with _Directory(root) as payload:
                    identity = payload.identity()
                record = {"version": 1, "entry": path.name, "identity": identity, "profile": None}
                _write_record(path, record)
            return cls(store, pins, entry, fd, record)
        except BaseException:
            if fd is not None:
                os.close(fd)
            try:
                if entry is not None:
                    _remove_pinned_tree(entry)
            finally:
                pins.close()
            raise

    def record_profile(self, name: str) -> None:
        if self._fd is None or self._record["profile"] is not None:
            raise RuntimeError("profile registration requires an uncommitted live lease")
        if name != self.profile_name:
            raise ValueError("LPAC profile name must match its ownership entry")
        record = {**self._record, "profile": name}
        _write_record(self._entry.path, record)
        self._record = record

    def _cleanup_locked(self, close_profile: Callable[[], None]) -> None:
        with contextlib.ExitStack() as stack:
            try:
                root = stack.enter_context(_Directory(self.root, delete=True))
            except FileNotFoundError:
                root = None
            if root is not None and root.identity() != self._record["identity"]:
                raise ValueError("LPAC payload directory identity changed")
            close_profile()
            if root is not None:
                _remove_pinned_tree(root)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        _remove_pinned_tree(self._entry)
        self._pins.close()

    def cleanup(self, close_profile: Callable[[], None]) -> None:
        with _store_lock(self.store):
            self._cleanup_locked(close_profile)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._pins.close()


@dataclass
class _RecoveryReport:
    recovered: list[str] = field(default_factory=list)
    active: list[str] = field(default_factory=list)
    unclaimed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def recover_orphans(directory: Path) -> _RecoveryReport:
    """Reclaim committed, unlocked entries only; preserve errors for later retry."""
    store, pins = _store(directory)
    report = _RecoveryReport()
    with pins, _store_lock(store):
        for path in sorted(store.iterdir()):
            if not _ENTRY.fullmatch(path.name):
                continue
            lease = None
            entry_pins = contextlib.ExitStack()
            fd = None
            try:
                try:
                    fd = _lease(path / "lease")
                except OSError as exc:
                    if exc.winerror == 32:
                        report.active.append(path.name)
                        continue
                    raise
                entry = entry_pins.enter_context(_Directory(path, delete=True))
                record = _read_record(path)
                if record["profile"] is None:
                    report.unclaimed.append(path.name)
                    continue
                lease = _RuntimeLease(store, entry_pins, entry, fd, record)
                fd = None
                lease._cleanup_locked(partial(_delete_profile, record["profile"]))
                report.recovered.append(path.name)
            except (OSError, ValueError, TypeError) as exc:
                report.errors[path.name] = str(exc)
            finally:
                if lease is not None:
                    lease.release()
                if fd is not None:
                    os.close(fd)
                entry_pins.close()
    return report
