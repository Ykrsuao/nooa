# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Live named host directories, without host ACL edits or direct LPAC access."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nooa.runtime.sandbox._lpac_directories import _DirectoryBroker, _DirectoryGrant

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows directory handles and LPAC"),
    pytest.mark.timeout(180),
]


def _security(path):
    from ctypes import wintypes as w

    from nooa.runtime.sandbox._win_appcontainer import _check, _fn, _security

    get = _fn(
        _security,
        "GetFileSecurityW",
        w.BOOL,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    )
    size = w.DWORD()
    get(str(path), 7, None, 0, ctypes.byref(size))
    assert size.value
    data = ctypes.create_string_buffer(size.value)
    _check(get(str(path), 7, data, len(data), ctypes.byref(size)))
    return data.raw


async def test_live_nested_read_write_and_list_without_acl_changes(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    (root / "nested").mkdir()
    file = root / "nested/data.txt"
    file.write_bytes(b"old")
    before = {path: _security(path) for path in (root, root / "nested", file)}
    async with _DirectoryBroker(
        {"read": _DirectoryGrant(root), "write": _DirectoryGrant(root, writable=True)}
    ) as broker:
        assert await broker.list("read") == [{"name": "nested", "kind": "directory"}]
        assert await broker.list("read", "nested") == [{"name": "data.txt", "kind": "file"}]
        assert await broker.read("read", "nested/data.txt") == b"old"
        with pytest.raises(PermissionError, match="read-only"):
            await broker.write("read", "nested/data.txt", b"bad")
        assert await broker.write("write", "nested/data.txt", b"changed") == 7
        assert await broker.read("read", "nested/data.txt") == b"changed"
        # Host changes between calls remain visible, unlike staged input snapshots.
        file.write_bytes(b"live host update")
        (root / "new.txt").write_bytes(b"new")
        assert await broker.read("read", "nested/data.txt") == b"live host update"
        assert await broker.list("read") == [
            {"name": "nested", "kind": "directory"},
            {"name": "new.txt", "kind": "file"},
        ]
        assert await broker.write("write", "nested/data.txt", b"") == 0
        assert await broker.read("read", "nested/data.txt") == b""
        for directory, _ in broker._roots.values():
            assert not os.get_handle_inheritable(directory.handle)
        assert before == {path: _security(path) for path in before}
    assert before == {path: _security(path) for path in before}
    with pytest.raises(RuntimeError, match="closed"):
        await broker.read("read", "nested/data.txt")
    await broker.aclose()


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../secret",
        "nested/../secret",
        "/secret",
        "C:/secret",
        "C:secret",
        "\\secret",
        "nested\\file",
        "//host/file",
        "a//b",
        "a/",
        "file:stream",
        "NUL",
        "con.txt",
        "name.",
        "name ",
        "a\0b",
        "COM\u00b9",
        "/".join(["a"] * 33),
    ],
)
async def test_worker_paths_cannot_escape_or_alias_a_grant(tmp_path, path):
    async with _DirectoryBroker({"data": _DirectoryGrant(tmp_path, writable=True)}) as broker:
        with pytest.raises(ValueError):
            await broker.read("data", path)
        with pytest.raises(ValueError):
            await broker.write("data", path, b"bad")
        if path:
            with pytest.raises(ValueError):
                await broker.list("data", path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("path", [None, False, 1, [], {}])
async def test_non_string_worker_paths_are_refused(tmp_path, path):
    async with _DirectoryBroker({"data": _DirectoryGrant(tmp_path)}) as broker:
        with pytest.raises(TypeError):
            await broker.read("data", path)
        with pytest.raises(TypeError):
            await broker.list("data", path)


async def test_unknown_grants_missing_files_and_directories_do_not_create(tmp_path):
    async with _DirectoryBroker({"data": _DirectoryGrant(tmp_path, writable=True)}) as broker:
        for name in ("../data", str(tmp_path), "missing"):
            with pytest.raises(PermissionError, match="not granted"):
                await broker.read(name, "file")
        with pytest.raises(OSError):
            await broker.write("data", "missing", b"bad")
        with pytest.raises(OSError):
            await broker.write("data", "missing-dir/file", b"bad")
        with pytest.raises(OSError):
            await broker.read("data", "missing-dir")
    assert not list(tmp_path.iterdir())


async def test_file_and_listing_limits_fail_without_partial_output(tmp_path):
    (tmp_path / "large").write_bytes(b"oversized")
    (tmp_path / "small").write_bytes(b"old")
    async with _DirectoryBroker(
        {"data": _DirectoryGrant(tmp_path, writable=True)}, max_file_bytes=4, max_entries=1
    ) as broker:
        with pytest.raises(ValueError, match="max_file_bytes"):
            await broker.read("data", "large")
        with pytest.raises(ValueError, match="max_file_bytes"):
            await broker.write("data", "small", b"too large")
        with pytest.raises(ValueError, match="max_entries"):
            await broker.list("data")
    assert (tmp_path / "small").read_bytes() == b"old"


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_entries", 0),
        ("max_entries", True),
        ("max_entries", 4097),
        ("max_file_bytes", 0),
        ("max_file_bytes", True),
        ("max_file_bytes", 4 * 1024 * 1024 + 1),
    ],
)
def test_invalid_limits_do_not_pin_directories(option, value, tmp_path):
    with pytest.raises(ValueError):
        _DirectoryBroker({"data": _DirectoryGrant(tmp_path)}, **{option: value})
    tmp_path.rmdir()


def test_invalid_roots_and_partial_setup_release_pins(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"original")
    for invalid in (
        root / "missing",
        root / "file",
        Path("relative"),
        Path(root.anchor),
        Path(r"\\server\share"),
        Path(r"\\?\C:\Windows"),
        Path(r"\\.\pipe\pipe"),
        root / "bad:stream",
        root / ".." / "other",
    ):
        with pytest.raises((OSError, ValueError)):
            _DirectoryBroker({"first": _DirectoryGrant(root), "bad": _DirectoryGrant(invalid)})
    root.rename(tmp_path / "unpinned")


async def test_grant_root_and_ancestors_cannot_be_replaced(tmp_path):
    ancestor = tmp_path / "ancestor"
    root = ancestor / "root"
    root.mkdir(parents=True)
    async with _DirectoryBroker({"data": _DirectoryGrant(root)}) as broker:
        for path in (root, ancestor):
            with pytest.raises(OSError):
                path.rename(path.with_name("moved"))
        assert await broker.list("data") == []
    ancestor.rename(tmp_path / "moved")


async def test_hardlinks_are_refused_for_reads_and_writes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    os.link(outside, root / "alias")
    async with _DirectoryBroker({"data": _DirectoryGrant(root, writable=True)}) as broker:
        with pytest.raises(ValueError, match="hard-link"):
            await broker.read("data", "alias")
        with pytest.raises(ValueError, match="hard-link"):
            await broker.write("data", "alias", b"bad")
    assert outside.read_bytes() == b"private"


def _junction(path, target):
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path), str(target)],
        check=True,
        capture_output=True,
        timeout=10,
    )


async def test_junctions_are_listed_but_never_followed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"private")
    junction = root / "junction"
    _junction(junction, outside)
    try:
        with pytest.raises(OSError):
            async with _DirectoryBroker({"data": _DirectoryGrant(junction)}):
                pass
        async with _DirectoryBroker({"data": _DirectoryGrant(root, writable=True)}) as broker:
            assert await broker.list("data") == [{"name": "junction", "kind": "reparse"}]
            with pytest.raises(OSError):
                await broker.list("data", "junction")
            with pytest.raises(OSError):
                await broker.read("data", "junction/file")
            with pytest.raises(OSError):
                await broker.write("data", "junction/file", b"bad")
        assert (outside / "file").read_bytes() == b"private"
    finally:
        junction.rmdir()


async def test_junction_swap_at_descendant_open_is_denied(tmp_path, monkeypatch):
    from nooa.runtime.sandbox import _lpac_directories as directories

    root, outside = tmp_path / "root", tmp_path / "outside"
    child = root / "child"
    child.mkdir(parents=True)
    outside.mkdir()
    (child / "file").write_bytes(b"approved")
    (outside / "file").write_bytes(b"private")
    native_open = directories._open_native_file
    async with _DirectoryBroker({"data": _DirectoryGrant(root, writable=True)}) as broker:

        def race(path, writable, **kwargs):
            if str(path) == "child":
                child.rename(root / "moved")
                _junction(child, outside)
            return native_open(path, writable, **kwargs)

        monkeypatch.setattr(directories, "_open_native_file", race)
        try:
            with pytest.raises(OSError):
                await broker.write("data", "child/file", b"bad")
            assert (outside / "file").read_bytes() == b"private"
            assert (root / "moved/file").read_bytes() == b"approved"
        finally:
            if child.is_junction():
                child.rmdir()


async def test_leaf_write_excludes_rename_and_concurrent_writes(tmp_path, monkeypatch):
    from nooa.runtime.sandbox import _lpac_directories as directories

    root = tmp_path / "root"
    root.mkdir()
    target = root / "file"
    target.write_bytes(b"old")
    write = directories.os.write
    observed = []

    def race(fd, data):
        for change in (
            lambda: target.rename(tmp_path / "moved"),
            lambda: target.write_bytes(b"concurrent"),
        ):
            with pytest.raises(OSError):
                change()
            observed.append(True)
        return write(fd, data)

    async with _DirectoryBroker({"data": _DirectoryGrant(root, writable=True)}) as broker:
        monkeypatch.setattr(directories.os, "write", race)
        assert await broker.write("data", "file", b"new") == 3
    assert observed == [True] * 2
    assert target.read_bytes() == b"new"


async def test_cancellation_and_close_drain_pending_disk_io(tmp_path, monkeypatch):
    from nooa.runtime.sandbox import _lpac_directories as directories

    target = tmp_path / "file"
    target.write_bytes(b"old")
    broker = _DirectoryBroker({"data": _DirectoryGrant(tmp_path, writable=True)})
    started, release = threading.Event(), threading.Event()
    write = directories.os.write

    def delayed(fd, data):
        started.set()
        assert release.wait(5)
        return write(fd, data)

    monkeypatch.setattr(directories.os, "write", delayed)
    task = asyncio.create_task(broker.write("data", "file", b"new"))
    close = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        close = asyncio.create_task(broker.aclose())
        await asyncio.sleep(0.02)
        assert not task.done() and not close.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await close
    finally:
        release.set()
        await asyncio.gather(task, *([close] if close else []), return_exceptions=True)
        await broker.aclose()
    assert target.read_bytes() == b"new"
    target.unlink()
    tmp_path.rmdir()


async def test_unicode_names_and_multi_page_enumeration(tmp_path):
    names = [f"{index:04}-" + "x" * 100 for index in range(400)]
    names.append("\u6587\u4ef6-\U0001f4c4.txt")
    for name in names:
        (tmp_path / name).write_bytes(b"data")
    async with _DirectoryBroker({"data": _DirectoryGrant(tmp_path)}) as broker:
        expected = [{"name": name, "kind": "file"} for name in sorted(names)]
        assert await broker.list("data") == expected
        assert await broker.list("data") == expected  # Enumeration restarts each call.
        assert await broker.read("data", names[-1]) == b"data"


async def test_real_lpac_agent_uses_only_named_broker_operations(tmp_path):
    from nooa import Agent, strategy
    from nooa.events import PythonOutput, ResultStatus
    from nooa.runtime.sandbox._appcontainer import _AppContainerPython
    from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
    from nooa.runtime.sandbox._lpac_runtime import stage_framework
    from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

    root = tmp_path / "documents"
    root.mkdir()
    source_root, output_root = root / "source", root / "output"
    source_root.mkdir()
    output_root.mkdir()
    source, output = source_root / "source.txt", output_root / "output.txt"
    source.write_bytes(b"approved")
    output.write_bytes(b"")
    with _AppContainerPython(workspace_access="read") as runtime:
        stage_framework(runtime)
        backend = _LpacCodeActStrategy(runtime, tools=("list_files", "read", "write"))
        async with _DirectoryBroker(
            {
                "source": _DirectoryGrant(source_root),
                "output": _DirectoryGrant(output_root, writable=True),
            }
        ) as broker:

            class Demo(Agent, llm=FakeLLMClient()):
                async def list_files(self, name: str) -> list[dict[str, str]]:
                    return await broker.list(name)

                async def read(self, name: str, path: str) -> bytes:
                    return await broker.read(name, path)

                async def write(self, name: str, path: str, data: bytes) -> int:
                    return await broker.write(name, path, data)

                @strategy(backend)
                async def compute(self) -> str:
                    """Copy the granted document."""
                    ...

            cells = [
                f"open({str(source)!r}, 'rb').read()",
                "await self.read('source', '../outside')",
                "await self.write('source', 'source.txt', b'bad')",
                "assert len(await self.list_files('source')) == 1\n"
                "await self.write('output', 'output.txt', await self.read('source', 'source.txt'))\n"
                "return_result((await self.read('output', 'output.txt')).decode())",
            ]
            agent = Demo(
                llm=FakeLLMClient(
                    scripted_responses=[
                        LLMResponse(
                            raw_response=None,
                            content="",
                            finish_reason="tool_calls",
                            tool_calls=[
                                ToolCall(
                                    id=f"c{i}",
                                    name="execute_python",
                                    arguments=json.dumps({"code": cell}),
                                )
                            ],
                        )
                        for i, cell in enumerate(cells)
                    ]
                )
            )
            assert await agent.compute() == "approved"
            errors = [
                e.error
                for e in agent.event_manager.values()
                if isinstance(e, PythonOutput) and e.execution_status is ResultStatus.ERROR
            ]
            assert len(errors) == 3, errors
            assert "PermissionError" in errors[0]
            assert "plain Windows filenames" in errors[1]
            assert "read-only" in errors[2]
    assert source.read_bytes() == output.read_bytes() == b"approved"
