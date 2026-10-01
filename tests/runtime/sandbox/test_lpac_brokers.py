# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exact-file grants and pinned HTTPS, including real TLS and an LPAC worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpcore
import pytest

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.events import PythonOutput
from nooa.runtime.restrictions import DEFAULT_BLOCKED_MODULES, RestrictionsConfig
from nooa.runtime.sandbox import _windows_session as managed
from nooa.runtime.sandbox._lpac_files import _FileBroker, _FileGrant
from nooa.runtime.sandbox._lpac_http import _HttpsBroker, _HttpsEndpoint, _public_address
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy
from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = pytest.mark.timeout(180)
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows file handles and LPAC")


@windows
async def test_files_are_bounded_named_grants_not_worker_paths(tmp_path):
    source, output, secret = (tmp_path / name for name in ("source", "output", "secret"))
    source.write_bytes(b"read-only")
    output.write_bytes(b"old data")
    secret.write_bytes(b"private")
    async with _FileBroker(
        {"source": _FileGrant(source), "output": _FileGrant(output, writable=True)},
        max_file_bytes=16,
    ) as broker:
        assert await broker.read("source") == b"read-only"
        for name in ("../secret", str(secret), "missing"):
            with pytest.raises(PermissionError):
                await broker.read(name)
            with pytest.raises(PermissionError):
                await broker.write(name, b"bad")
        with pytest.raises(PermissionError, match="read-only"):
            await broker.write("source", b"bad")
        with pytest.raises(ValueError, match="max_file_bytes"):
            await broker.write("output", b"x" * 17)
        assert await broker.write("output", b"new") == 3
        assert await broker.read("output") == b"new"
        assert await broker.write("output", b"") == 0
        assert await broker.read("output") == b""
        with pytest.raises(OSError):
            os.replace(secret, output)
        with pytest.raises(OSError):
            output.open("wb").close()
        assert all(not os.get_inheritable(fd) for fd, _ in broker._files.values())
    assert source.read_bytes() == b"read-only"
    assert secret.read_bytes() == b"private"
    assert output.read_bytes() == b""
    with pytest.raises(RuntimeError, match="closed"):
        await broker.read("source")
    await broker.aclose()


@windows
async def test_oversized_existing_file_is_not_read(tmp_path):
    source = tmp_path / "large"
    source.write_bytes(b"x" * 20)
    async with _FileBroker({"large": _FileGrant(source)}, max_file_bytes=10) as broker:
        with pytest.raises(ValueError, match="max_file_bytes"):
            await broker.read("large")


@windows
def test_invalid_grants_do_not_create_or_truncate_files(tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"original")
    for path in (
        tmp_path / "missing",
        tmp_path,
        tmp_path / "file:stream",
        tmp_path / "NUL",
        tmp_path / "file.",
        tmp_path / "other" / ".." / "file",
        Path("relative.txt"),
        Path(r"\\server\share\file"),
        Path(r"\\?\C:\file"),
        Path(r"\\.\pipe\file"),
    ):
        with pytest.raises((OSError, ValueError)):
            _FileBroker({"target": _FileGrant(path, writable=True)})
    assert target.read_bytes() == b"original"
    assert not (tmp_path / "missing").exists()
    alias = tmp_path / "alias"
    os.link(target, alias)
    with pytest.raises(ValueError, match="hard-link"):
        _FileBroker({"target": _FileGrant(target, writable=True)})
    target.unlink()  # Failed setup retained no file handle.


@windows
def test_partial_grant_setup_closes_previously_opened_handles(tmp_path):
    source = tmp_path / "first"
    source.write_bytes(b"original")
    with pytest.raises(OSError):
        _FileBroker({"first": _FileGrant(source), "missing": _FileGrant(tmp_path / "missing")})
    source.unlink()


@windows
def test_junction_retarget_between_check_and_open_is_rejected(tmp_path, monkeypatch):
    from nooa.runtime.sandbox import _lpac_files

    approved, moved, outside = (tmp_path / name for name in ("approved", "moved", "outside"))
    approved.mkdir()
    outside.mkdir()
    file = approved / "file"
    file.write_bytes(b"approved")
    (outside / "file").write_bytes(b"private")
    open_native = _lpac_files._open_native_file

    def race(path, writable):
        approved.rename(moved)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(approved), str(outside)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return open_native(path, writable)

    monkeypatch.setattr(_lpac_files, "_open_native_file", race)
    with pytest.raises(OSError):
        _FileBroker({"file": _FileGrant(file, writable=True)})
    assert (outside / "file").read_bytes() == b"private"
    assert (moved / "file").read_bytes() == b"approved"
    approved.rmdir()  # Remove only the junction, never its target.


@windows
async def test_unicode_file_handles_support_non_bmp_names(tmp_path):
    path = tmp_path / "\u6587\u4ef6-\U0001f4c4.txt"
    path.write_bytes(b"original")
    async with _FileBroker({"data": _FileGrant(path, writable=True)}) as broker:
        assert await broker.read("data") == b"original"
        await broker.write("data", b"changed")
    assert path.read_bytes() == b"changed"


@windows
@pytest.mark.parametrize("value", [0, -1, 4 * 1024 * 1024 + 1, True, 1.5])
def test_invalid_file_budgets_are_refused(value):
    with pytest.raises(ValueError):
        _FileBroker({}, max_file_bytes=value)


@windows
async def test_cancellation_drains_file_io_before_close(tmp_path, monkeypatch):
    import nooa.runtime.sandbox._lpac_files as files

    path = tmp_path / "output"
    path.write_bytes(b"old")
    broker = _FileBroker({"output": _FileGrant(path, writable=True)})
    started, release = threading.Event(), threading.Event()
    write = files.os.write

    def delayed(fd, data):
        started.set()
        assert release.wait(5)
        return write(fd, data)

    monkeypatch.setattr(files.os, "write", delayed)
    task = asyncio.create_task(broker.write("output", b"new"))
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
    assert path.read_bytes() == b"new"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "2001::1",
        "localhost",
        "2130706433",
        "0x7f000001",
        "2606:4700:4700::1111%eth0",
    ],
)
def test_non_public_and_transition_addresses_are_refused(address):
    with pytest.raises(ValueError):
        _public_address(address)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/",
        "file:///etc/passwd",
        "/relative",
        "https://user:secret@example.test/",
        "https://example.test/#fragment",
        "https://example.test:0/",
        "https://example.test:65536/",
    ],
)
def test_unsafe_endpoint_urls_are_refused(url):
    with pytest.raises(ValueError):
        _HttpsBroker({"data": _HttpsEndpoint(url, "93.184.216.34")})


@pytest.mark.parametrize("value", [0, -1, 4 * 1024 * 1024 + 1, True, 1.5])
def test_invalid_http_budgets_are_refused(value):
    with pytest.raises(ValueError):
        _HttpsBroker({}, max_response_bytes=value)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_unbounded_http_timeouts_are_refused(value):
    with pytest.raises(ValueError):
        _HttpsBroker({}, timeout_s=value)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory = tmp_path_factory.mktemp("broker-tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "allowed.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("allowed.test")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
async def https_server(certificates, monkeypatch):
    cert, key = certificates
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    requests, connections, sni, peers = [], [], [], []
    context.set_servername_callback(lambda sock, name, ctx: sni.append(name))
    tasks = set()
    slow_started, release = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        peers.append(reader)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            path = request.split(b" ", 2)[1]
            if path == b"/slow":
                slow_started.set()
                await release.wait()
            if path == b"/redirect":
                response = b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n"
            elif path == b"/large":
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 128\r\n\r\n" + b"x" * 128
            elif path == b"/compressed":
                response = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 0\r\n\r\n"
            elif path == b"/duplicate-encoding":
                response = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Encoding: identity\r\nContent-Length: 0\r\n\r\n"
            else:
                response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nSet-Cookie: token=server\r\nContent-Length: 8\r\n\r\napproved"
            writer.write(response)
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    connect = httpcore.AnyIOBackend.connect_tcp

    async def redirect(self, host, target_port, **kwargs):
        connections.append((host, target_port))
        assert host == "93.184.216.34" and target_port == port
        # Test-only routing: the production backend still chose the approved public IP.
        return await connect(self, "127.0.0.1", target_port, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", redirect)
    getaddrinfo = socket.getaddrinfo

    def no_hostname_dns(host, *args, **kwargs):
        assert host in ("127.0.0.1", b"127.0.0.1")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_hostname_dns)
    try:
        yield SimpleNamespace(
            cert=cert,
            port=port,
            requests=requests,
            connections=connections,
            sni=sni,
            slow_started=slow_started,
            peers=peers,
        )
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tasks)


def _https(server, path="/data", **kwargs):
    broker = _HttpsBroker(
        {"data": _HttpsEndpoint(f"https://allowed.test:{server.port}{path}", "93.184.216.34")},
        ca_file=server.cert,
        **kwargs,
    )
    # Exercise Python 3.13's stricter CA validation on both supported runtimes.
    broker._tls.verify_flags |= ssl.VERIFY_X509_STRICT
    return broker


async def test_https_pins_ip_retains_tls_host_and_ignores_environment(https_server, monkeypatch):
    server = https_server
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "must-not-be-used.pem")
    broker = _https(server)
    for _ in range(2):
        assert await broker.fetch("data") == {
            "status": 200,
            "content_type": "text/plain",
            "body": b"approved",
        }
    assert server.connections == [("93.184.216.34", server.port)] * 2
    assert server.sni == ["allowed.test"] * 2
    assert len(server.requests) == 2
    for request in server.requests:
        assert request.startswith(b"GET /data HTTP/1.1\r\n")
        assert f"Host: allowed.test:{server.port}".encode() in request
        assert b"cookie:" not in request.lower() and b"authorization:" not in request.lower()
    with pytest.raises(PermissionError):
        await broker.fetch("https://127.0.0.1/private")
    assert len(server.connections) == 2


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/redirect", "redirects"),
        ("/large", "max_response_bytes"),
        ("/compressed", "compressed"),
        ("/duplicate-encoding", "compressed"),
    ],
)
async def test_https_rejects_redirects_and_oversized_or_encoded_bodies(https_server, path, message):
    broker = _https(https_server, path, max_response_bytes=16)
    with pytest.raises((ValueError, PermissionError), match=message):
        await broker.fetch("data")
    assert len(https_server.requests) == 1 and len(https_server.connections) == 1


async def test_https_still_verifies_certificate_hostname(https_server):
    broker = _HttpsBroker(
        {"data": _HttpsEndpoint(f"https://wrong.test:{https_server.port}/data", "93.184.216.34")},
        ca_file=https_server.cert,
    )
    with pytest.raises(httpcore.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await broker.fetch("data")
    assert not https_server.requests


async def test_https_does_not_take_trust_roots_from_environment(https_server, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(https_server.cert))
    broker = _HttpsBroker(
        {"data": _HttpsEndpoint(f"https://allowed.test:{https_server.port}/data", "93.184.216.34")}
    )
    with pytest.raises(httpcore.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await broker.fetch("data")
    assert not https_server.requests


async def test_https_timeout_and_cancellation_close_request(https_server):
    server = https_server
    with pytest.raises((TimeoutError, httpcore.ReadTimeout)):
        await _https(server, "/slow", timeout_s=0.1).fetch("data")
    server.slow_started.clear()
    task = asyncio.create_task(_https(server, "/slow").fetch("data"))
    await asyncio.wait_for(server.slow_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(3):
        while not all(peer.at_eof() or peer.exception() for peer in server.peers):
            await asyncio.sleep(0.01)
    assert (await _https(server).fetch("data"))["body"] == b"approved"


@pytest.fixture
def managed_https(https_server, monkeypatch):
    def broker(endpoints, **kwargs):
        # Only the test CA changes. Keep real policy admission, TLS verification,
        # IP pinning, transport and session ownership on the production path.
        result = _HttpsBroker(endpoints, ca_file=https_server.cert, **kwargs)
        result._tls.verify_flags |= ssl.VERIFY_X509_STRICT
        return result

    monkeypatch.setattr(managed, "_HttpsBroker", broker)
    return https_server


def _managed_https_agent(owner, *cells):
    # Reach the native socket denial, not the optional Python import guard.
    backend = owner.strategy(
        config=CodeActConfig(
            max_retries=10,
            restrictions=RestrictionsConfig(blocked_modules=DEFAULT_BLOCKED_MODULES - {"socket"}),
        )
    )

    class Demo(Agent, llm=FakeLLMClient()):
        @strategy(backend)
        async def compute(self) -> str:
            """Read the granted HTTPS resource."""
            ...

    return Demo(
        llm=FakeLLMClient(
            scripted_responses=[
                LLMResponse(
                    raw_response=None,
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id=f"c{i}", name="execute_python", arguments=json.dumps({"code": cell})
                        )
                    ],
                )
                for i, cell in enumerate(cells)
            ]
        )
    )


@windows
async def test_managed_https_agent_allowed_request_and_enforced_denials(managed_https, monkeypatch):
    server = managed_https
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "must-not-be-used.pem")
    endpoints = {
        name: _HttpsEndpoint(f"https://allowed.test:{server.port}/{name}", "93.184.216.34")
        for name in ("data", "redirect", "large")
    }
    endpoints["wrong"] = _HttpsEndpoint(f"https://wrong.test:{server.port}/data", "93.184.216.34")
    owner = _WindowsSandboxSession(_WindowsSandboxPolicy(https=endpoints, max_response_bytes=16))
    async with owner:
        root = owner._runtime.root
        agent = _managed_https_agent(
            owner,
            "import socket\nsocket.socket()",
            "await self.fetch_https('missing')",
            "await self.fetch_https('https://127.0.0.1/private')",
            "await self.fetch_https('data', url='https://127.0.0.1/private')",
            "await self.fetch_https('redirect')",
            "await self.fetch_https('large')",
            "await self.fetch_https('wrong')",
            "response = await self.fetch_https('data')\n"
            "assert response['status'] == 200\n"
            "assert response['content_type'] == 'text/plain'\n"
            "return_result(response['body'].decode())",
        )
        assert await agent.compute() == "approved"
        errors = [
            str(event.error)
            for event in agent.event_manager.values()
            if isinstance(event, PythonOutput) and event.error
        ]
        assert len(errors) == 7, errors
        for error, expected in zip(
            errors,
            (
                "10013",
                "not granted",
                "not granted",
                "url",
                "redirects",
                "max_response_bytes",
                "CERTIFICATE_VERIFY_FAILED",
            ),
            strict=True,
        ):
            assert expected in error
        assert len(server.connections) == 4
        assert server.sni == ["allowed.test", "allowed.test", "wrong.test", "allowed.test"]
        assert [request.split(b" ", 2)[1] for request in server.requests] == [
            b"/redirect",
            b"/large",
            b"/data",
        ]
        for request in server.requests:
            assert f"Host: allowed.test:{server.port}".encode() in request
            assert b"authorization:" not in request.lower() and b"cookie:" not in request.lower()
        context = "\n".join(str(message["content"]) for message in agent.llm.last_messages)
        assert "Windows LPAC" in context and "Response limit: 16 bytes" in context
        assert "direct internet sockets are denied" in context
        assert not owner._executors and not owner._active
    assert not root.exists() and owner._https is None


@windows
@pytest.mark.parametrize("deadline", ["broker", "https"])
async def test_managed_https_deadlines_cancellation_and_new_calls(managed_https, deadline):
    server = managed_https
    policy = _WindowsSandboxPolicy(
        https={
            name: _HttpsEndpoint(f"https://allowed.test:{server.port}/{name}", "93.184.216.34")
            for name in ("slow", "data")
        },
        broker_timeout_s=0.2 if deadline == "broker" else 0,
        https_timeout_s=0.2 if deadline == "https" else 10,
    )
    owner = _WindowsSandboxSession(policy)
    async with owner:
        root = owner._runtime.root
        agent = _managed_https_agent(owner, "await self.fetch_https('slow')", "return_result('ok')")
        assert await agent.compute() == "ok"
        errors = [
            event.error
            for event in agent.event_manager.values()
            if isinstance(event, PythonOutput) and event.error
        ]
        assert len(errors) == 1 and "Timeout" in str(errors[0])
        assert server.slow_started.is_set() and not owner._executors
        server.slow_started.clear()
        task = asyncio.create_task(
            _managed_https_agent(owner, "await self.fetch_https('slow')").compute()
        )
        try:
            await asyncio.wait_for(server.slow_started.wait(), 90)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not owner._executors and not owner._active
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(3):
            while not all(peer.at_eof() or peer.exception() for peer in server.peers):
                await asyncio.sleep(0.01)
        assert (
            await _managed_https_agent(
                owner, "return_result((await self.fetch_https('data'))['body'].decode())"
            ).compute()
            == "approved"
        )
    assert not root.exists() and owner._https is None


@pytest.fixture(scope="module")
def framework():
    from nooa.runtime.sandbox._appcontainer import _AppContainerPython
    from nooa.runtime.sandbox._lpac_runtime import stage_framework

    with _AppContainerPython() as runtime:
        stage_framework(runtime)
        yield runtime


@windows
async def test_real_lpac_worker_uses_brokers_without_direct_file_or_network_grants(
    framework,
    https_server,
    tmp_path,
):
    from nooa.runtime.sandbox._lpac import _LpacExecutor

    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"original")
    output.write_bytes(b"")
    async with _FileBroker(
        {"source": _FileGrant(source), "output": _FileGrant(output, writable=True)}
    ) as files:
        http = _https(https_server)
        executor = _LpacExecutor(
            framework,
            tools={"read": files.read, "write": files.write, "fetch": http.fetch},
            startup_timeout_s=60,
        )
        try:
            result = await executor.run_cell(
                "data = await self.fetch('data')\n"
                "await self.write('output', data['body'])\n"
                "(await self.read('source'), await self.read('output'))"
            )
            assert result.success, result.error
            assert result.returned_value == (b"original", b"approved")
            for code, message in (
                (f"open({str(source)!r}, 'rb').read()", "PermissionError"),
                ("__import__('socket').socket()", "10013"),
                ("await self.write('source', b'bad')", "read-only"),
                (f"await self.read({str(source)!r})", "not granted"),
                ("await self.fetch('https://127.0.0.1/private')", "not granted"),
            ):
                result = await executor.run_cell(code)
                assert not result.success
                assert message in str(result.error)
            assert len(https_server.requests) == 1
        finally:
            await executor.aclose()
    assert output.read_bytes() == b"approved"
