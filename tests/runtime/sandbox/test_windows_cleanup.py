# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded staging cleanup preserves Windows path and retry semantics."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows staging cleanup", allow_module_level=True)

from nooa.runtime.sandbox import _windows_cleanup as cleanup  # noqa: E402
from nooa.runtime.sandbox._appcontainer import _AppContainerPython  # noqa: E402


def test_unicode_and_long_paths_are_removed(tmp_path):
    root = tmp_path / "隔离 runtime"
    target = root / ("nested" * 12) / ("long" * 30) / "文件.txt"
    assert len(str(target)) > 260
    extended = Path("\\\\?\\" + str(target))
    extended.parent.mkdir(parents=True)
    extended.write_bytes(b"staged dependency")

    cleanup._remove_tree(root)

    assert not root.exists()


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 6])
def test_junctions_at_partition_boundaries_preserve_targets(tmp_path, depth):
    import _winapi

    root = tmp_path / "runtime"
    parent = root.joinpath(*[f"level-{i}" for i in range(depth)])
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep.txt"
    canary.write_bytes(b"external data")
    _winapi.CreateJunction(str(outside), str(parent / "junction"))

    cleanup._remove_tree(root)

    assert not root.exists()
    assert canary.read_bytes() == b"external data"


def test_empty_tree_is_removed(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    cleanup._remove_tree(root)

    assert not root.exists()


def test_file_deletions_overlap_with_bounded_workers(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    for index in range(16):
        (root / f"file-{index}").write_bytes(b"data")
    unlink = os.unlink
    lock = threading.Lock()
    overlap = threading.Event()
    active = peak = completed = 0

    def tracked_unlink(path, *args, **kwargs):
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                overlap.set()
        try:
            assert overlap.wait(5), "cleanup never dispatched overlapping deletions"
            return unlink(path, *args, **kwargs)
        finally:
            with lock:
                active -= 1
                completed += 1

    with monkeypatch.context() as patch:
        patch.setattr(cleanup.os, "unlink", tracked_unlink)
        cleanup._remove_tree(root)

    assert 2 <= peak <= 4
    assert active == 0 and completed == 16
    assert not root.exists()


def test_failed_delete_joins_workers_and_can_be_retried(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    for index in range(16):
        (root / f"file-{index}").write_bytes(b"data")
    unlink = os.unlink
    lock = threading.Lock()
    successful_worker = threading.Event()
    active = completed = 0

    def failing_unlink(path, *args, **kwargs):
        nonlocal active, completed
        with lock:
            active += 1
        try:
            if Path(path).name == "file-0":
                assert successful_worker.wait(5), "other cleanup workers did not run"
                raise PermissionError("synthetic locked dependency")
            result = unlink(path, *args, **kwargs)
            successful_worker.set()
            return result
        finally:
            with lock:
                active -= 1
                completed += 1

    with monkeypatch.context() as patch:
        patch.setattr(cleanup.os, "unlink", failing_unlink)
        with pytest.raises(PermissionError, match="synthetic locked dependency"):
            cleanup._remove_tree(root)
        assert active == 0
        assert completed >= 2
        assert (root / "file-0").read_bytes() == b"data"

    cleanup._remove_tree(root)
    assert not root.exists()


def _owner(root):
    owner = _AppContainerPython.__new__(_AppContainerPython)
    owner.root = root
    owner._parent = root.parent
    owner._lock = threading.Lock()
    owner._closed = False
    owner._lease = None
    owner._profile = None
    return owner


def test_ordinary_close_partial_failure_remains_retryable(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    locked = root / "locked"
    locked.write_bytes(b"retry me")
    (root / "removable").write_bytes(b"remove me")
    owner = _owner(root)
    unlink = os.unlink

    def fail_locked(path, *args, **kwargs):
        if Path(path).name == "locked":
            raise PermissionError("synthetic locked dependency")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup.os, "unlink", fail_locked)
        with pytest.raises(PermissionError, match="synthetic locked dependency"):
            owner.close()
    assert not owner._closed
    assert locked.read_bytes() == b"retry me"
    assert not (root / "removable").exists()

    owner.close()
    assert owner._closed and not root.exists()
    owner.close()


@pytest.mark.parametrize("substitution", ["parent", "junction"])
def test_ordinary_close_refuses_substituted_root(tmp_path, substitution):
    import _winapi

    root = tmp_path / "runtime"
    owner = _owner(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep"
    canary.write_bytes(b"external data")
    if substitution == "parent":
        owner.root = outside / "replacement"
        owner.root.mkdir()
        canary = owner.root / "keep"
        canary.write_bytes(b"external data")
    else:
        _winapi.CreateJunction(str(outside), str(root))
    try:
        with pytest.raises(ValueError, match="substituted LPAC staging root"):
            owner.close()
        assert not owner._closed
        assert canary.read_bytes() == b"external data"
    finally:
        if substitution == "junction":
            root.rmdir()
