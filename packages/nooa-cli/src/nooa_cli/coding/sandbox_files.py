# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded coding file operations under one explicitly granted workspace.

Only relative slash-separated paths are accepted. Every directory is opened
without following links; files must be regular and have one link. Windows uses
the native pinned directory broker; Linux uses descriptor-relative O_NOFOLLOW
opens. The host and other processes running as the same user remain trusted.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from nooa.runtime.sandbox._lpac_directories import (
    _DirectoryBroker,
    _DirectoryGrant,
    _relative_parts,
)
from nooa.runtime.sandbox._lpac_files import _finish_file_io
from nooa.runtime.sandbox.errors import SandboxUnavailable

_SNAPSHOT_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".nooa", ".venv", "node_modules", "__pycache__"}
)


class SnapshotInfo(TypedDict):
    files: int
    total_bytes: int
    excluded: list[str]


@dataclass(frozen=True)
class FileChange:
    """Text observed and written by one checked operation, without a post-read."""

    bytes_written: int
    old_text: str | None
    new_text: str


def _create_snapshot_root(destination: Path) -> None:
    """Create, never adopt, a directory below a caller-controlled local parent."""
    if ".." in destination.parts:
        raise ValueError("snapshot destination cannot contain parent traversal")
    for ancestor in destination.parents:
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("snapshot destination ancestors cannot be links")
    # Windows Python gives 0700 a new restrictive DACL, which removes the LPAC
    # entry inherited from the already private runtime parent. Keep inheritance.
    destination.mkdir(
        mode=0o777 if sys.platform == "win32" else 0o700, parents=False, exist_ok=False
    )


def _write_snapshot_file(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def _read_bounded(fd: int, limit: int) -> bytes:
    if os.fstat(fd).st_size > limit:
        raise ValueError("file exceeds max_file_bytes")
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise ValueError("file exceeds max_file_bytes")


def _write_all(fd: int, data: bytes) -> int:
    pending = memoryview(data)
    while pending:
        written = os.write(fd, pending)
        if written <= 0:
            raise OSError("file write made no progress")
        pending = pending[written:]
    os.ftruncate(fd, len(data))
    return len(data)


class _LinuxDirectoryBroker:
    """Descriptor-relative equivalent of the Windows directory tool operations."""

    def __init__(self, root: Path, *, max_file_bytes: int, max_entries: int):
        self._limit = max_file_bytes
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._closed = False
        self._root_fd = -1
        self._directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        # Check every root component, including its ancestors; Path.resolve()
        # would silently turn a symlink grant into authority over its target.
        with contextlib.ExitStack() as stack:
            fd = os.open(root.anchor, self._directory_flags)
            stack.callback(os.close, fd)
            for part in root.parts[1:]:
                fd = os.open(part, self._directory_flags, dir_fd=fd)
                stack.callback(os.close, fd)
            self._root_fd = os.dup(fd)
            os.set_inheritable(self._root_fd, False)

    def _directory(self, stack: contextlib.ExitStack, parts: tuple[str, ...]) -> int:
        if self._closed:
            raise RuntimeError("directory broker is closed")
        fd = self._root_fd
        for part in parts:
            fd = os.open(part, self._directory_flags, dir_fd=fd)
            stack.callback(os.close, fd)
        return fd

    def _file(
        self, stack: contextlib.ExitStack, path: str, *, write=False, create=False
    ) -> tuple[int, int, str]:
        parts = _relative_parts(path)
        parent = self._directory(stack, parts[:-1])
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_RDWR if write else os.O_RDONLY)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("directory tools require regular files")
        if info.st_nlink != 1:
            raise ValueError("directory tools cannot access hard-link aliases")
        return fd, parent, parts[-1]

    async def list(self, name: str, path: str = "") -> list[dict[str, str]]:
        return await _finish_file_io(self._list, path)

    def _list(self, path: str) -> list[dict[str, str]]:
        with self._lock, contextlib.ExitStack() as stack:
            fd = self._directory(stack, _relative_parts(path, allow_root=True))
            results = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(results) >= self._max_entries:
                        raise ValueError("directory exceeds max_entries")
                    kind = (
                        "reparse"
                        if entry.is_symlink()
                        else "directory"
                        if entry.is_dir(follow_symlinks=False)
                        else "file"
                    )
                    results.append({"name": entry.name, "kind": kind})
            return sorted(results, key=lambda entry: (entry["name"].casefold(), entry["name"]))

    async def read(self, name: str, path: str) -> bytes:
        return await _finish_file_io(self._read, path)

    def _read(self, path: str) -> bytes:
        with self._lock, contextlib.ExitStack() as stack:
            return _read_bounded(self._file(stack, path)[0], self._limit)

    async def create(self, name: str, path: str, data: bytes) -> int:
        return await _finish_file_io(self._create, path, data)

    def _create(self, path: str, data: bytes) -> int:
        with self._lock, contextlib.ExitStack() as stack:
            fd, parent, name = self._file(stack, path, write=True, create=True)
            try:
                return _write_all(fd, data)
            except BaseException:
                opened = os.fstat(fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                    os.unlink(name, dir_fd=parent)
                raise

    async def update(self, name: str, path: str, expected: bytes, data: bytes) -> int:
        return await _finish_file_io(self._update, path, expected, data)

    def _update(self, path: str, expected: bytes, data: bytes) -> int:
        with self._lock, contextlib.ExitStack() as stack:
            fd = self._file(stack, path, write=True)[0]
            if _read_bounded(fd, self._limit) != expected:
                raise ValueError("file changed since it was read; read again before editing")
            os.lseek(fd, 0, os.SEEK_SET)
            return _write_all(fd, data)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                os.close(self._root_fd)
                self._closed = True

    async def aclose(self) -> None:
        await _finish_file_io(self.close)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _encode(text: str, original: str = "") -> bytes:
    remaining = original.replace("\r\n", "")
    ending = (
        "\r\n" if "\r\n" in original and "\n" not in remaining and "\r" not in remaining else "\n"
    )
    return _normalize(text).replace("\n", ending).encode("utf-8")


class SandboxFiles:
    """UTF-8 files in one workspace, with bounded output and checked edits.

    ``write`` may create a file beneath an existing directory. Directory
    creation, deletion, renaming and arbitrary host paths are not exposed.
    Existing pure CRLF files keep CRLF when edited. All other files use LF.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 1024 * 1024,
        max_entries: int = 512,
    ):
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1 and 4 MiB")
        if type(max_entries) is not int or not 0 < max_entries <= 4096:
            raise ValueError("max_entries must be between 1 and 4096")
        root = Path(root).absolute()
        if root == Path(root.anchor) or ".." in root.parts:
            raise ValueError("workspace must be a directory below the filesystem root")
        self._root_path = root
        self._limit = max_file_bytes
        if sys.platform == "win32":
            self._store = _DirectoryBroker(
                {"workspace": _DirectoryGrant(root, writable=True)},
                max_file_bytes=max_file_bytes,
                max_entries=max_entries,
            )
        elif sys.platform == "linux":
            self._store = _LinuxDirectoryBroker(
                root, max_file_bytes=max_file_bytes, max_entries=max_entries
            )
        else:
            raise SandboxUnavailable("sandbox coding file tools require Linux or Windows")

    def _check_text(self, text: str) -> None:
        if type(text) is not str or len(text) > self._limit:
            raise ValueError("file text must be a string within max_file_bytes")

    def _check_data(self, data: bytes) -> None:
        if len(data) > self._limit:
            raise ValueError("file data exceeds max_file_bytes")

    async def list(self, path: str = "") -> list[dict[str, str]]:
        """List one workspace directory; links are labeled and never traversed."""
        return await self._store.list("workspace", path)

    async def read(self, path: str) -> str:
        """Read a bounded UTF-8 regular file by slash-separated relative path."""
        return (await self._store.read("workspace", path)).decode("utf-8")

    async def create(self, path: str, text: str) -> int:
        """Create a UTF-8 file beneath an existing parent; never overwrite."""
        return (await self.create_edit(path, text)).bytes_written

    async def create_edit(self, path: str, text: str) -> FileChange:
        """Create a file and return the exact text written for activity reporting."""
        self._check_text(text)
        data = _encode(text)
        self._check_data(data)
        count = await self._store.create("workspace", path, data)
        return FileChange(count, None, data.decode("utf-8"))

    async def write(self, path: str, text: str) -> int:
        """Write UTF-8, creating a missing file, with a check against concurrent edits."""
        return (await self.write_edit(path, text)).bytes_written

    async def write_edit(self, path: str, text: str) -> FileChange:
        """Write and report the checked prior contents and exact replacement."""
        self._check_text(text)
        try:
            before = await self._store.read("workspace", path)
        except FileNotFoundError:
            return await self.create_edit(path, text)
        original = before.decode("utf-8")
        data = _encode(text, original)
        self._check_data(data)
        count = await self._store.update("workspace", path, before, data)
        return FileChange(count, original, data.decode("utf-8"))

    async def replace(self, path: str, old: str, new: str) -> int:
        """Replace exactly one text occurrence; ambiguous, absent or stale edits fail."""
        return (await self.replace_edit(path, old, new)).bytes_written

    async def replace_edit(self, path: str, old: str, new: str) -> FileChange:
        """Replace text and report the exact before/after bytes as UTF-8 text."""
        self._check_text(old)
        self._check_text(new)
        if not old:
            raise ValueError("replacement old text must not be empty")
        before = await self._store.read("workspace", path)
        original = before.decode("utf-8")
        text, old, new = _normalize(original), _normalize(old), _normalize(new)
        if text.count(old) != 1:
            raise ValueError("replacement text must match exactly once")
        data = _encode(text.replace(old, new, 1), original)
        self._check_data(data)
        count = await self._store.update("workspace", path, before, data)
        return FileChange(count, original, data.decode("utf-8"))

    async def copy_snapshot(
        self,
        destination: Path,
        *,
        max_total_bytes: int = 16 * 1024 * 1024,
        max_entries: int = 512,
    ) -> SnapshotInfo:
        """Copy bounded binary workspace data to a newly owned command directory.

        The caller supplies a trusted destination beneath its private runtime;
        it must not exist. The caller also owns cleanup of partial copies on
        errors or cancellation. Files are read through the same pinned broker.
        Directory names .git, .nooa, .venv, node_modules and __pycache__ are
        excluded at every depth and their relative paths are returned. Other
        links, excessive depth/entries/bytes and unreadable files fail closed.
        Trusted host edits during copying can make this a mixed-time snapshot.
        """
        if type(max_total_bytes) is not int or not 0 < max_total_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_total_bytes must be between 1 and 64 MiB")
        if type(max_entries) is not int or not 0 < max_entries <= 4096:
            raise ValueError("max_entries must be between 1 and 4096")
        destination = Path(destination).absolute()
        if destination.is_relative_to(self._root_path):
            raise ValueError("snapshot destination must be outside the source workspace")
        await _finish_file_io(_create_snapshot_root, destination)
        info: SnapshotInfo = {"files": 0, "total_bytes": 0, "excluded": []}
        pending = [""]
        count = 0
        while pending:
            relative = pending.pop()
            for entry in await self._store.list("workspace", relative):
                count += 1
                if count > max_entries:
                    raise ValueError("snapshot exceeds max_entries")
                path = "/".join(part for part in (relative, entry["name"]) if part)
                parts = _relative_parts(path)
                if entry["kind"] == "reparse":
                    raise PermissionError(f"snapshot cannot follow links: {path}")
                target = destination.joinpath(*parts)
                if entry["kind"] == "directory":
                    if entry["name"] in _SNAPSHOT_EXCLUDED_DIRECTORIES:
                        info["excluded"].append(path)
                        continue
                    await _finish_file_io(target.mkdir, 0o777 if sys.platform == "win32" else 0o700)
                    pending.append(path)
                else:
                    data = await self._store.read("workspace", path)
                    if info["total_bytes"] + len(data) > max_total_bytes:
                        raise ValueError("snapshot exceeds max_total_bytes")
                    await _finish_file_io(_write_snapshot_file, target, data)
                    info["files"] += 1
                    info["total_bytes"] += len(data)
        info["excluded"].sort()
        return info

    def close(self) -> None:
        self._store.close()

    async def aclose(self) -> None:
        await self._store.aclose()

    async def __aenter__(self) -> SandboxFiles:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
