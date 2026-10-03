# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native network capability selection; public HTTPS acceptance is explicitly live."""

from __future__ import annotations

import json
import socket
import sys
import textwrap

import pytest

from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._windows_context import _render_windows_policy
from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

native = pytest.mark.skipif(sys.platform != "win32", reason="Windows LPAC")

_TOKEN_CAPABILITIES = textwrap.dedent("""
    import ctypes as c, json
    from ctypes import wintypes as w
    a = c.WinDLL('advapi32', use_last_error=True)
    k = c.WinDLL('kernel32', use_last_error=True)
    a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    a.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.LocalFree.argtypes = [c.c_void_p]
    token, size = w.HANDLE(), w.DWORD()
    assert a.OpenProcessToken(w.HANDLE(-1), 8, c.byref(token))
    try:
        a.GetTokenInformation(token, 30, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        assert a.GetTokenInformation(token, 30, buffer, size.value, c.byref(size))
        count = w.DWORD.from_buffer(buffer).value
        class Entry(c.Structure):
            _fields_ = [('sid', c.c_void_p), ('attributes', w.DWORD)]
        class Groups(c.Structure):
            _fields_ = [('count', w.DWORD), ('entries', Entry * count)]
        result = []
        for entry in Groups.from_buffer(buffer).entries:
            text = w.LPWSTR()
            assert a.ConvertSidToStringSidW(entry.sid, c.byref(text))
            try:
                result.append(text.value)
            finally:
                k.LocalFree(c.cast(text, c.c_void_p))
        print(json.dumps(result))
    finally:
        k.CloseHandle(token)
""")


def _json(runtime, source, **kwargs):
    result = runtime.run(source, **kwargs)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", (), []])
def test_network_permission_requires_a_boolean(value):
    with pytest.raises(TypeError, match="network must be a bool"):
        WindowsSandboxPolicy(network=value)
    with pytest.raises(TypeError, match="network must be a bool"):
        _AppContainerPython(network=value)


def test_policy_explains_enabled_network_without_promising_loopback():
    assert WindowsSandboxPolicy().network is False
    disabled = _render_windows_policy(WindowsSandboxPolicy())
    enabled = _render_windows_policy(WindowsSandboxPolicy(network=True))
    assert "direct internet sockets are denied" in disabled
    assert "direct internet and private-network sockets are enabled" in enabled
    assert "Windows firewall and AppContainer loopback restrictions still apply" in enabled
    assert "direct internet sockets are denied" not in enabled


@native
@pytest.mark.parametrize("network", [False, True])
def test_worker_receives_only_selected_native_capabilities(network):
    from nooa.runtime.sandbox._win_appcontainer import _sid_text

    with _AppContainerPython(network=network) as runtime:
        capabilities = set(_json(runtime, _TOKEN_CAPABILITIES))
        assert runtime._profile is not None
        registry_sid = _sid_text(runtime._profile.registry_read_sid)
        expected = {registry_sid}
        if network:
            # Well-known capability SIDs, not application-defined name hashes.
            expected.update({"S-1-15-3-1", "S-1-15-3-3"})
        assert capabilities == expected


@native
def test_default_worker_cannot_open_an_internet_socket():
    with _AppContainerPython() as runtime:
        result = _json(
            runtime,
            "import socket, json\n"
            "try:\n"
            "    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):\n"
            "        result = 'allowed'\n"
            "except OSError as exc:\n"
            "    result = exc.winerror\n"
            "print(json.dumps(result))",
        )
    assert result == 10013  # WSAEACCES from the default AppContainer token.


@native
def test_network_permission_preserves_windows_loopback_restriction():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            accepted, _ = server.accept()
            accepted.close()
        with _AppContainerPython(network=True) as runtime:
            result = _json(
                runtime,
                "import socket, json\n"
                "try:\n"
                f"    with socket.create_connection({server.getsockname()!r}, timeout=2):\n"
                "        result = 'allowed'\n"
                "except OSError as exc:\n"
                "    result = {'error': type(exc).__name__, 'winerror': exc.winerror}\n"
                "print(json.dumps(result))",
            )
    # With network capabilities Windows can drop loopback traffic rather than
    # reject socket creation. Neither case is permission to reach the listener.
    assert result in (
        {"error": "PermissionError", "winerror": 10013},
        {"error": "TimeoutError", "winerror": None},
        {"error": "TimeoutError", "winerror": 10060},
    )


@native
def test_network_capability_failure_releases_profile_and_staging(monkeypatch):
    from nooa.runtime.sandbox import _win_appcontainer as native_api

    derive = native_api._capability_sid
    owners = []

    def unavailable(name):
        if name == "privateNetworkClientServer":
            raise OSError("network capability unavailable")
        return derive(name)

    monkeypatch.setattr(native_api, "_capability_sid", unavailable)
    with pytest.raises(OSError, match="network capability unavailable"):
        _AppContainerPython(network=True, _retain=owners.append)
    assert len(owners) == 1
    owner = owners[0]
    assert owner._closed and not owner.root.exists()
    assert not owner._profile._created and not owner._profile.sid


@native
@pytest.mark.integration
@pytest.mark.parametrize("managed", [False, True], ids=["stdlib", "managed-session"])
async def test_enabled_worker_fetches_public_https(managed):
    # No inherited proxy settings, credentials, API keys or local model gateway.
    source = textwrap.dedent("""
        import json, urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('https://example.com/', timeout=15) as response:
            body = response.read(65536)
            print(json.dumps({'status': response.status, 'example': b'Example Domain' in body}))
    """)
    if managed:
        async with WindowsSandboxSession(WindowsSandboxPolicy(network=True)) as owner:
            result = _json(owner._runtime, source, timeout_s=25)
    else:
        with _AppContainerPython(network=True) as runtime:
            result = _json(runtime, source, timeout_s=25)
    assert result == {"status": 200, "example": True}
