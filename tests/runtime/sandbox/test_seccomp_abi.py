# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Seccomp socket policy remains closed across alternate Linux syscall ABIs."""

from __future__ import annotations

import ctypes
import errno
import mmap
import multiprocessing
import platform
import socket
import struct
import sys

import pytest

from nooa.runtime.sandbox import guards


def _evaluate_filter(*, arch: int, syscall: int, domain: int) -> int:
    """Execute classic BPF over the kernel's public seccomp_data layout."""
    data = struct.pack("IIQ6Q", syscall, arch, 0, domain, 0, 0, 0, 0, 0)
    program = tuple(struct.iter_unpack("HBBI", guards._build_no_inet_filter()))
    accumulator = 0
    pc = 0
    while pc < len(program):
        opcode, yes, no, constant = program[pc]
        if opcode == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            accumulator = struct.unpack_from("I", data, constant)[0]
        elif opcode == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += yes if accumulator == constant else no
        elif opcode == 0x45:  # BPF_JMP | BPF_JSET | BPF_K
            pc += yes if accumulator & constant else no
        elif opcode == 0x06:  # BPF_RET | BPF_K
            return constant
        else:
            raise AssertionError(f"unexpected BPF opcode: {opcode}")
        pc += 1
    raise AssertionError("filter did not return")


@pytest.mark.parametrize("arch,number", [(0xC000003E, 41), (0xC00000B7, 198)])
@pytest.mark.parametrize("domain", [socket.AF_INET, socket.AF_INET6, 1])
def test_native_socket_policy(monkeypatch, arch, number, domain):
    monkeypatch.setattr(guards, "_AUDIT_ARCH", arch)
    monkeypatch.setattr(guards, "_NR_SOCKET", number)
    expected = 0x7FFF0000 if domain == 1 else 0x00050000 | errno.EACCES
    assert _evaluate_filter(arch=arch, syscall=number, domain=domain) == expected
    assert _evaluate_filter(arch=arch, syscall=number + 1, domain=domain) == 0x7FFF0000


@pytest.mark.parametrize(
    "arch,number", [(0x40000003, 102), (0x40000003, 359), (0xC000003E, 0x40000029)]
)
def test_alternate_socket_abis_cannot_bypass_network_policy(monkeypatch, arch, number):
    monkeypatch.setattr(guards, "_AUDIT_ARCH", 0xC000003E)
    monkeypatch.setattr(guards, "_NR_SOCKET", 41)
    assert _evaluate_filter(arch=arch, syscall=number, domain=socket.AF_INET) == (
        0x00050000 | errno.EACCES
    )


def _native_alternate_abi_probe(connection):
    # int 0x80 enters the 32-bit ABI from a 64-bit process. socket's three
    # arguments are integers, so no 32-bit pointers or compatibility libs are
    # needed. Preserve the System V ABI's callee-saved RBX register.
    code = bytes.fromhex("53 b8 67010000 bb 02000000 b9 01000000 31d2 cd80 5b c3")
    executable = mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
    executable.write(code)
    function = ctypes.CFUNCTYPE(ctypes.c_int)(
        ctypes.addressof(ctypes.c_char.from_buffer(executable))
    )
    libc = ctypes.CDLL(None, use_errno=True)
    guards.apply_seccomp_no_inet()
    foreign_result = function()
    ctypes.set_errno(0)
    x32_result = libc.syscall(0x40000029, socket.AF_INET, socket.SOCK_STREAM, 0)
    connection.send((foreign_result, x32_result, ctypes.get_errno()))
    connection.close()


@pytest.mark.sandbox
@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64", reason="native Linux x86_64 ABI"
)
def test_kernel_denies_32bit_and_x32_socket_syscalls():
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    worker = context.Process(target=_native_alternate_abi_probe, args=(send,))
    worker.start()
    send.close()
    try:
        assert receive.poll(10), "seccomp probe did not finish"
        assert receive.recv() == (-errno.EACCES, -1, errno.EACCES)
        worker.join(10)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(10)
        receive.close()
