# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Win32 primitives for the internal LPAC launcher. No shared ACLs are changed."""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes as w
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nooa._win_job import ProcessJob

if sys.platform != "win32":
    raise ImportError("AppContainer requires Windows")

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_security = ctypes.WinDLL("advapi32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)
_kernelbase = ctypes.WinDLL("kernelbase", use_last_error=True)
_ptr = ctypes.c_void_p
_size = ctypes.c_size_t
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x2000D


def _fn(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype = result
    fn.argtypes = args
    return fn


CloseHandle = _fn(_kernel, "CloseHandle", w.BOOL, w.HANDLE)
LocalFree = _fn(_kernel, "LocalFree", _ptr, _ptr)
FreeSid = _fn(_security, "FreeSid", _ptr, _ptr)
_CreateProfile = _fn(
    _userenv,
    "CreateAppContainerProfile",
    ctypes.c_long,
    w.LPCWSTR,
    w.LPCWSTR,
    w.LPCWSTR,
    _ptr,
    w.DWORD,
    ctypes.POINTER(_ptr),
)
_DeleteProfile = _fn(_userenv, "DeleteAppContainerProfile", ctypes.c_long, w.LPCWSTR)
_ConvertSid = _fn(
    _security,
    "ConvertSidToStringSidW",
    w.BOOL,
    _ptr,
    ctypes.POINTER(w.LPWSTR),
)
_OpenToken = _fn(_security, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
_GetTokenInfo = _fn(
    _security,
    "GetTokenInformation",
    w.BOOL,
    w.HANDLE,
    ctypes.c_int,
    _ptr,
    w.DWORD,
    ctypes.POINTER(w.DWORD),
)
_GetCurrentProcess = _fn(_kernel, "GetCurrentProcess", w.HANDLE)
_ConvertSD = _fn(
    _security,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    w.BOOL,
    w.LPCWSTR,
    w.DWORD,
    ctypes.POINTER(_ptr),
    ctypes.POINTER(w.DWORD),
)
_GetDacl = _fn(
    _security,
    "GetSecurityDescriptorDacl",
    w.BOOL,
    _ptr,
    ctypes.POINTER(w.BOOL),
    ctypes.POINTER(_ptr),
    ctypes.POINTER(w.BOOL),
)
_GetSacl = _fn(
    _security,
    "GetSecurityDescriptorSacl",
    w.BOOL,
    _ptr,
    ctypes.POINTER(w.BOOL),
    ctypes.POINTER(_ptr),
    ctypes.POINTER(w.BOOL),
)
_SetNamedSecurity = _fn(
    _security,
    "SetNamedSecurityInfoW",
    w.DWORD,
    w.LPWSTR,
    ctypes.c_int,
    w.DWORD,
    _ptr,
    _ptr,
    _ptr,
    _ptr,
)
_InitializeAttributes = _fn(
    _kernel,
    "InitializeProcThreadAttributeList",
    w.BOOL,
    _ptr,
    w.DWORD,
    w.DWORD,
    ctypes.POINTER(_size),
)
_UpdateAttribute = _fn(
    _kernel,
    "UpdateProcThreadAttribute",
    w.BOOL,
    _ptr,
    w.DWORD,
    _size,
    _ptr,
    _size,
    _ptr,
    _ptr,
)
_DeleteAttributes = _fn(_kernel, "DeleteProcThreadAttributeList", None, _ptr)
_CreateProcess = _fn(
    _kernel,
    "CreateProcessW",
    w.BOOL,
    w.LPCWSTR,
    w.LPWSTR,
    _ptr,
    _ptr,
    w.BOOL,
    w.DWORD,
    _ptr,
    w.LPCWSTR,
    _ptr,
    _ptr,
)
_ResumeThread = _fn(_kernel, "ResumeThread", w.DWORD, w.HANDLE)
_TerminateProcess = _fn(_kernel, "TerminateProcess", w.BOOL, w.HANDLE, w.UINT)
_Wait = _fn(_kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
_ExitCode = _fn(_kernel, "GetExitCodeProcess", w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD))
_GetLengthSid = _fn(_security, "GetLengthSid", w.DWORD, _ptr)
_SidArray = ctypes.POINTER(_ptr)
_DeriveCapability = _fn(
    _kernelbase,
    "DeriveCapabilitySidsFromName",
    w.BOOL,
    w.LPCWSTR,
    ctypes.POINTER(_SidArray),
    ctypes.POINTER(w.DWORD),
    ctypes.POINTER(_SidArray),
    ctypes.POINTER(w.DWORD),
)


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _hresult(code: int) -> None:
    if code < 0:
        raise OSError(f"AppContainer operation failed (HRESULT 0x{code & 0xFFFFFFFF:08x})")


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", _ptr), ("attributes", w.DWORD)]


def _sid_text(sid) -> str:
    text = w.LPWSTR()
    _check(_ConvertSid(sid, ctypes.byref(text)))
    try:
        value = text.value
        if value is None:
            raise OSError("SID conversion returned no text")
        return value
    finally:
        LocalFree(ctypes.cast(text, _ptr))


def _current_user_sid() -> str:
    token = w.HANDLE()
    _check(_OpenToken(_GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        size = w.DWORD()
        _GetTokenInfo(token, 1, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        _check(_GetTokenInfo(token, 1, data, size.value, ctypes.byref(size)))
        return _sid_text(_SidAndAttributes.from_buffer(data).sid)
    finally:
        CloseHandle(token)


def _registry_read_sid():
    groups, capabilities = _SidArray(), _SidArray()
    group_count, capability_count = w.DWORD(), w.DWORD()
    try:
        _check(
            _DeriveCapability(
                "registryRead",
                ctypes.byref(groups),
                ctypes.byref(group_count),
                ctypes.byref(capabilities),
                ctypes.byref(capability_count),
            )
        )
        if capability_count.value != 1:
            raise OSError("registryRead must resolve to exactly one capability SID")
        size = _GetLengthSid(capabilities[0])
        _check(size)
        return ctypes.create_string_buffer(ctypes.string_at(capabilities[0], size))
    finally:
        for values, count in ((groups, group_count.value), (capabilities, capability_count.value)):
            if values:
                for i in range(count):
                    LocalFree(values[i])
                LocalFree(ctypes.cast(values, _ptr))


class Profile:
    """Own exactly one newly-created profile; never reuse/delete another profile."""

    def __init__(self, *, name: str | None = None):
        if name is not None and (
            not isinstance(name, str)
            or not name.startswith("nooa.lpac.")
            or len(name) != len("nooa.lpac.") + 32
            or any(character not in "0123456789abcdef" for character in name[len("nooa.lpac.") :])
        ):
            raise ValueError("profile name must be a NOOA LPAC name with a lowercase UUID")
        self.name = name if name is not None else "nooa.lpac." + uuid.uuid4().hex
        self.sid = _ptr()
        self._created = False
        _hresult(
            _CreateProfile(
                self.name, self.name, "NOOA isolated Python", None, 0, ctypes.byref(self.sid)
            )
        )
        self._created = True
        try:
            self.sid_text = _sid_text(self.sid)
            self.user_sid = _current_user_sid()
            # LPAC needs OS registry ACEs for DLL initialization. This does not
            # grant network access or access to arbitrary user registry keys.
            self.registry_read_sid = _registry_read_sid()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._created:
            # Keep ownership on failure so the caller can retry cleanup.
            _hresult(_DeleteProfile(self.name))
            self._created = False
        if self.sid:
            FreeSid(self.sid)
            self.sid = _ptr()

    def grant_owned_directory(self, root: Path, path: Path, *, writable: bool = False):
        """Set a protected ACL only on a verified directory in our private tree."""
        root, path = root.absolute(), path.absolute()
        if path.resolve() != path or not path.is_relative_to(root) or not path.is_dir():
            raise ValueError("ACL target must be a real directory inside the owned tree")
        mask = "0x1301bf" if writable else "0x1200a9"  # modify vs read/execute, never WRITE_DAC
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.user_sid})(A;OICI;{mask};;;{self.sid_text})"
        if writable:
            sddl += "S:(ML;OICI;NW;;;LW)"
        descriptor = _ptr()
        _check(_ConvertSD(sddl, 1, ctypes.byref(descriptor), None))
        try:
            present, defaulted = w.BOOL(), w.BOOL()
            dacl, sacl = _ptr(), _ptr()
            _check(
                _GetDacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
                )
            )
            flags = 0x80000004  # protected DACL
            if writable:
                flags |= 0x10  # LABEL_SECURITY_INFORMATION, not the entire audit SACL
                _check(
                    _GetSacl(
                        descriptor,
                        ctypes.byref(present),
                        ctypes.byref(sacl),
                        ctypes.byref(defaulted),
                    )
                )
            error = _SetNamedSecurity(str(path), 1, flags, None, None, dacl, sacl)
            if error:
                raise ctypes.WinError(error)
        finally:
            LocalFree(descriptor)


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", _ptr),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", _StartupInfo), ("attributes", _ptr)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", w.HANDLE),
        ("thread", w.HANDLE),
        ("pid", w.DWORD),
        ("tid", w.DWORD),
    ]


class _Capabilities(ctypes.Structure):
    _fields_ = [
        ("sid", _ptr),
        ("capabilities", _ptr),
        ("count", w.DWORD),
        ("reserved", w.DWORD),
    ]


class SuspendedProcess:
    """Create suspended in LPAC AND its job, with three inherited stdio handles."""

    def __init__(
        self,
        profile: Profile,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        handles: list[int],
        *,
        job: ProcessJob,
    ):
        import subprocess

        if not profile._created:
            raise RuntimeError("AppContainer profile is closed")
        if len(handles) != 3 or len(set(handles)) != 3:
            raise ValueError("exactly three distinct standard handles are required")
        self._info = _ProcessInfo()
        capability = _SidAndAttributes(ctypes.cast(profile.registry_read_sid, _ptr), 4)
        attributes = [
            (
                0x20009,
                _Capabilities(profile.sid, ctypes.cast(ctypes.pointer(capability), _ptr), 1, 0),
            ),
            (0x20002, (w.HANDLE * 3)(*handles)),
            (0x2000F, w.DWORD(1)),  # LPAC: opt out of ALL APPLICATION PACKAGES
            (0x2000E, w.DWORD(1)),  # token-level child process restriction
            # Kernel assignment is part of creation, even if the parent dies
            # before CreateProcessW returns. Never retry without this attribute.
            (_PROC_THREAD_ATTRIBUTE_JOB_LIST, (w.HANDLE * 1)(job._creation_handle())),
        ]
        size = _size()
        _InitializeAttributes(None, len(attributes), 0, ctypes.byref(size))
        if ctypes.get_last_error() != 122:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        _check(_InitializeAttributes(buffer, len(attributes), 0, ctypes.byref(size)))
        try:
            for key, value in attributes:
                _check(
                    _UpdateAttribute(
                        buffer, 0, key, ctypes.byref(value), ctypes.sizeof(value), None, None
                    )
                )
            startup = _StartupInfoEx()
            startup.info.cb = ctypes.sizeof(startup)
            startup.info.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.info.hStdInput, startup.info.hStdOutput, startup.info.hStdError = handles
            startup.attributes = ctypes.cast(buffer, _ptr)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0"
            )
            _check(
                _CreateProcess(
                    argv[0],
                    command,
                    None,
                    None,
                    True,
                    0x80000 | 0x08000000 | 0x00000400 | 0x00000004,
                    environment,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(self._info),
                )
            )
        except BaseException:
            self.close()
            raise
        finally:
            _DeleteAttributes(buffer)

    @property
    def pid(self) -> int:
        return self._info.pid

    def resume(self):
        if _ResumeThread(self._info.thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, milliseconds: int) -> int | None:
        result = _Wait(self._info.process, milliseconds)
        if result == 258:
            return None
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = w.DWORD()
        _check(_ExitCode(self._info.process, ctypes.byref(code)))
        return code.value

    def close(self):
        if self._info.process:
            if self.wait(0) is None:
                # Closing the job may have started termination without signaling
                # the process handle yet. TerminateProcess then returns ACCESS_DENIED.
                terminated = _TerminateProcess(self._info.process, 1)
                error = ctypes.get_last_error() if not terminated else 0
                if self.wait(5000) is None:
                    if error:
                        raise ctypes.WinError(error)
                    raise TimeoutError("AppContainer process did not terminate")
            CloseHandle(self._info.process)
            self._info.process = None
        if self._info.thread:
            CloseHandle(self._info.thread)
            self._info.thread = None
