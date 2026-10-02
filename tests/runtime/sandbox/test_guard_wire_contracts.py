# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Portable guard dispatch and wire contracts without installing OS restrictions."""

import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import assert_type
from unittest.mock import Mock, call

import pytest

from nooa.runtime.sandbox import guards, wire
from nooa.runtime.sandbox.config import LandlockRule


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_linux_probes_and_installers_reject_other_platforms(monkeypatch, platform):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(guards, "_ARCH", "x86_64")
    monkeypatch.setattr(guards, "_NR_SECCOMP", 317)
    libc = Mock(side_effect=AssertionError("No foreign-platform syscall is allowed"))
    fork = Mock(side_effect=AssertionError("No foreign-platform fork is allowed"))
    monkeypatch.setattr(guards, "_libc", libc)
    monkeypatch.setattr(guards.os, "fork", fork, raising=False)
    assert guards.landlock_abi() == 0
    assert guards.seccomp_supported() is False
    with pytest.raises(OSError, match="only available on Linux"):
        guards.apply_landlock([])
    with pytest.raises(OSError, match="only available on Linux"):
        guards.apply_seccomp_no_inet()
    libc.assert_not_called()
    fork.assert_not_called()


def test_rlimits_reject_windows_even_with_a_resource_module(monkeypatch):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform="win32"))
    resource = Mock()
    monkeypatch.setitem(sys.modules, "resource", resource)
    with pytest.raises(OSError, match="not available on Windows"):
        guards.apply_rlimits(max_memory_mb=1, max_cpu_seconds=1)
    assert resource.mock_calls == []


@pytest.mark.parametrize("hard", [-1, 1024])
def test_posix_rlimits_preserve_headroom_and_existing_hard_caps(monkeypatch, hard):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guards, "_self_vmsize_bytes", lambda: 4096)
    resource = Mock(RLIMIT_AS=1, RLIMIT_CPU=2, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (0, hard)
    monkeypatch.setitem(sys.modules, "resource", resource)
    guards.apply_rlimits(max_memory_mb=2, max_cpu_seconds=2000)
    memory = 4096 + 2 * 1024 * 1024 if hard == -1 else hard
    cpu = 2000 if hard == -1 else hard
    assert resource.setrlimit.call_args_list == [call(1, (memory, memory)), call(2, (cpu, cpu))]
    resource.reset_mock()
    guards.apply_rlimits(max_memory_mb=0, max_cpu_seconds=-1)
    resource.getrlimit.assert_not_called()
    resource.setrlimit.assert_not_called()


@pytest.mark.parametrize(
    "exited,code,expected", [(True, 0, True), (True, 1, False), (False, 0, False)]
)
def test_linux_seccomp_probe_waits_for_child_status(monkeypatch, exited, code, expected):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guards, "_NR_SECCOMP", 317)
    fork = Mock(return_value=123)
    waitpid = Mock(return_value=(123, 42))
    monkeypatch.setattr(guards.os, "fork", fork, raising=False)
    monkeypatch.setattr(guards.os, "waitpid", waitpid)
    monkeypatch.setattr(guards.os, "WIFEXITED", lambda status: exited, raising=False)
    monkeypatch.setattr(guards.os, "WEXITSTATUS", lambda status: code, raising=False)
    assert guards.seccomp_supported() is expected
    fork.assert_called_once_with()
    waitpid.assert_called_once_with(123, 0)


def test_linux_seccomp_install_still_sets_no_new_privs_first(monkeypatch):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guards, "_NR_SECCOMP", 317)
    operations = Mock()
    monkeypatch.setattr(guards, "_install_no_new_privs", operations.no_new_privs)
    monkeypatch.setattr(guards, "_build_no_inet_filter", lambda: b"filter")
    monkeypatch.setattr(guards, "_seccomp_install", operations.install)
    guards.apply_seccomp_no_inet()
    assert operations.mock_calls == [call.no_new_privs(), call.install(b"filter")]


def test_linux_landlock_still_adds_rules_and_closes_descriptors(monkeypatch):
    monkeypatch.setattr(guards, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guards, "landlock_abi", lambda: 3)
    libc = Mock()
    libc.syscall.side_effect = [100, 0, 0]
    monkeypatch.setattr(guards, "_libc", lambda: libc)
    monkeypatch.setattr(guards, "_is_dir", lambda fd: True)
    no_new_privs = Mock()
    monkeypatch.setattr(guards, "_install_no_new_privs", no_new_privs)
    opened = Mock(return_value=101)
    closed = Mock()
    monkeypatch.setattr(guards.os, "open", opened)
    monkeypatch.setattr(guards.os, "close", closed)
    monkeypatch.setattr(guards.os, "O_PATH", 0x200000, raising=False)
    monkeypatch.setattr(guards.os, "O_CLOEXEC", 0x80000, raising=False)
    guards.apply_landlock([LandlockRule("workspace", write=True)])
    opened.assert_called_once_with("workspace", 0x280000)
    assert closed.call_args_list == [call(101), call(100)]
    assert [c.args[0] for c in libc.syscall.call_args_list] == [444, 445, 446]
    no_new_privs.assert_called_once_with()


@pytest.mark.parametrize("data", [None, bytearray(b"invalid"), "invalid"])
def test_wire_rejects_nonbytes_packer_results(monkeypatch, data):
    monkeypatch.setattr(wire.msgpack, "packb", lambda *args, **kwargs: data)
    with pytest.raises(TypeError, match="msgpack did not return bytes"):
        wire.Codec().dumps({"value": 1})


def test_wire_keeps_bytes_contract_and_does_not_require_numpy(monkeypatch):
    monkeypatch.setattr(wire, "sys", SimpleNamespace(modules={}))
    codec = wire.Codec()
    data = assert_type(codec.dumps({"value": (1, 2)}), bytes)
    assert codec.loads(data) == {"value": (1, 2)}
    with pytest.raises(TypeError, match="cannot serialize"):
        codec.dumps(object())


def test_dataclass_types_are_not_encoded_as_instances_with_numpy_loaded():
    import numpy as np

    @dataclass
    class Point:
        x: int

    codec = wire.Codec({wire.type_key(Point): Point})
    assert codec.loads(codec.dumps(Point(2))) == Point(2)
    with pytest.raises(TypeError, match="cannot serialize"):
        codec.dumps(Point)
    assert codec.loads(codec.dumps(np.int64(2))) == 2
