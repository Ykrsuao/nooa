# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native bounded workspace tools; links and concurrent edits fail closed."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading

import pytest
from nooa_cli.coding.sandbox_files import SandboxFiles

pytestmark = pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="Windows and Linux sandbox files"
)


async def test_create_read_edit_and_write_preserve_crlf(tmp_path):
    (tmp_path / "nested").mkdir()
    target = tmp_path / "nested" / "source.py"
    target.write_bytes(b"first\r\nsecond\r\n")
    async with SandboxFiles(tmp_path) as files:
        assert await files.list() == [{"name": "nested", "kind": "directory"}]
        assert await files.read("nested/source.py") == "first\r\nsecond\r\n"
        await files.replace("nested/source.py", "first\n", "updated\n")
        assert target.read_bytes() == b"updated\r\nsecond\r\n"
        await files.write("nested/source.py", "whole\r\nfile\r\n")
        assert target.read_bytes() == b"whole\r\nfile\r\n"
        assert await files.write("new.py", "new\r\n") == 4
        assert await files.read("new.py") == "new\n"
        with pytest.raises(FileExistsError):
            await files.create("new.py", "overwrite")
    assert (tmp_path / "new.py").read_bytes() == b"new\n"
    with pytest.raises(RuntimeError, match="closed"):
        await files.read("new.py")
    await files.aclose()


@pytest.mark.parametrize(
    "path", ["../secret", "sub/../secret", "/secret", "C:/secret", "a\\b", "file:stream", "NUL", ""]
)
async def test_all_file_paths_remain_relative_to_grant(tmp_path, path):
    async with SandboxFiles(tmp_path) as files:
        for operation in (
            files.read(path),
            files.create(path, "bad"),
            files.write(path, "bad"),
            files.replace(path, "old", "bad"),
        ):
            with pytest.raises(ValueError):
                await operation
    assert list(tmp_path.iterdir()) == []


async def test_bounded_utf8_size_and_nonunique_replacements_fail_without_edits(tmp_path):
    (tmp_path / "file").write_bytes(b"a a\n")
    async with SandboxFiles(tmp_path, max_file_bytes=8) as files:
        for old in ("a", "missing", ""):
            with pytest.raises(ValueError):
                await files.replace("file", old, "b")
        with pytest.raises(ValueError, match="max_file_bytes"):
            await files.write("file", "\u4e2d" * 4)
        with pytest.raises(ValueError, match="max_file_bytes"):
            await files.create("new", "\u4e2d" * 4)
        with pytest.raises(OSError):
            await files.create("missing/new", "data")
    assert (tmp_path / "file").read_bytes() == b"a a\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["file"]


async def test_hardlinks_never_read_or_modified(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    os.link(outside, root / "alias")
    async with SandboxFiles(root) as files:
        for operation in (files.read("alias"), files.write("alias", "bad")):
            with pytest.raises(ValueError, match="hard-link"):
                await operation
        with pytest.raises(OSError):
            await files.create("alias", "bad")
    assert outside.read_bytes() == b"secret"


async def test_directory_links_listed_but_not_traversed(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    link = root / "link"
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
            timeout=10,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        async with SandboxFiles(root) as files:
            assert await files.list() == [{"name": "link", "kind": "reparse"}]
            for operation in (
                files.read("link/secret"),
                files.write("link/secret", "bad"),
                files.create("link/new", "bad"),
            ):
                with pytest.raises(OSError):
                    await operation
        with pytest.raises(OSError):
            SandboxFiles(link)
        assert (outside / "secret").read_bytes() == b"private"
        assert not (outside / "new").exists()
    finally:
        if sys.platform == "win32":
            link.rmdir()
        else:
            link.unlink()


async def test_concurrent_host_edit_is_not_silently_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"original")
    async with SandboxFiles(tmp_path) as files:
        update = files._store.update

        async def concurrent(name, path, expected, data):
            target.write_bytes(b"host update")
            return await update(name, path, expected, data)

        monkeypatch.setattr(files._store, "update", concurrent)
        with pytest.raises(ValueError, match="changed since"):
            await files.replace("file", "original", "agent edit")
    assert target.read_bytes() == b"host update"


async def test_edit_report_is_the_checked_write_without_a_racy_post_read(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"before\r\n")
    async with SandboxFiles(tmp_path) as files:
        update = files._store.update

        async def host_update_after_commit(name, path, expected, data):
            count = await update(name, path, expected, data)
            target.write_bytes(b"host update")
            return count

        monkeypatch.setattr(files._store, "update", host_update_after_commit)
        change = await files.replace_edit("file", "before", "after")
        assert change.old_text == "before\r\n"
        assert change.new_text == "after\r\n"
        assert change.bytes_written == 7
    assert target.read_bytes() == b"host update"


async def test_failed_new_file_write_rolls_back_partial_creation(tmp_path, monkeypatch):
    write = os.write
    called = False

    def fail_after_partial(fd, data):
        nonlocal called
        if called:
            raise OSError("simulated disk failure")
        called = True
        return write(fd, data[:2])

    async with SandboxFiles(tmp_path) as files:
        monkeypatch.setattr(os, "write", fail_after_partial)
        with pytest.raises(OSError, match="simulated disk failure"):
            await files.create("new", "partial content")
        assert await files.list() == []
    assert not (tmp_path / "new").exists()


async def test_cancellation_drains_create_before_releasing_workspace(tmp_path, monkeypatch):
    files = SandboxFiles(tmp_path)
    started, release = threading.Event(), threading.Event()
    write = os.write

    def delayed(fd, data):
        started.set()
        assert release.wait(5)
        return write(fd, data)

    monkeypatch.setattr(os, "write", delayed)
    create = asyncio.create_task(files.create("file", "content"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        create.cancel()
        close = asyncio.create_task(files.aclose())
        await asyncio.sleep(0.01)
        assert not create.done() and not close.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await create
        await close
    finally:
        release.set()
        await files.aclose()
    assert (tmp_path / "file").read_bytes() == b"content"


async def test_snapshot_copies_binary_and_reports_documented_exclusions(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "binary").write_bytes(b"\x00\xff\xfe\x80")
    (root / "file").write_bytes(b"text\r\n")
    for name in (".git", ".nooa", ".venv", "node_modules", "__pycache__"):
        (root / name).mkdir()
        (root / name / "ignored").write_bytes(b"excluded")
    target = tmp_path / "snapshot"
    async with SandboxFiles(root) as files:
        info = await files.copy_snapshot(target)
    assert info == {
        "files": 2,
        "total_bytes": 10,
        "excluded": [".git", ".nooa", ".venv", "__pycache__", "node_modules"],
    }
    assert (target / "nested" / "binary").read_bytes() == b"\x00\xff\xfe\x80"
    assert (target / "file").read_bytes() == b"text\r\n"
    assert sorted(path.name for path in target.iterdir()) == ["file", "nested"]


async def test_snapshot_refuses_limits_existing_destination_and_nested_destination(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "first").write_bytes(b"1234")
    (root / "second").write_bytes(b"5678")
    async with SandboxFiles(root) as files:
        with pytest.raises(ValueError, match="max_total_bytes"):
            await files.copy_snapshot(tmp_path / "small", max_total_bytes=5)
        with pytest.raises(ValueError, match="max_entries"):
            await files.copy_snapshot(tmp_path / "few", max_entries=1)
        existing = tmp_path / "existing"
        existing.mkdir()
        (existing / "mine").write_bytes(b"keep")
        with pytest.raises(FileExistsError):
            await files.copy_snapshot(existing)
        with pytest.raises(ValueError, match="outside"):
            await files.copy_snapshot(root / "nested")
        assert not (root / "nested").exists()
    assert (existing / "mine").read_bytes() == b"keep"


async def test_snapshot_refuses_hardlinked_files(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    os.link(outside, root / "alias")
    async with SandboxFiles(root) as files:
        with pytest.raises(ValueError, match="hard-link"):
            await files.copy_snapshot(tmp_path / "snapshot")
    assert not list((tmp_path / "snapshot").iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux O_NOFOLLOW/FIFO semantics")
async def test_linux_file_symlinks_and_special_files_fail_without_blocking(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "fifo")
    async with SandboxFiles(root) as files:
        with pytest.raises(OSError):
            await files.read("link")
        with pytest.raises(ValueError, match="regular"):
            await files.read("fifo")
        with pytest.raises((PermissionError, ValueError)):
            await files.copy_snapshot(tmp_path / "snapshot")
