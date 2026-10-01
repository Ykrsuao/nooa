# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal fixed-endpoint HTTPS broker, not unrestricted sandbox networking."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import certifi
import httpcore
import httpx


@dataclass(frozen=True)
class _HttpsEndpoint:
    url: str
    address: str


def _public_address(address: str) -> str:
    ip = ipaddress.ip_address(address)
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and (
                ip not in ipaddress.ip_network("2000::/3")
                or ip.sixtofour
                or ip.teredo
                or ip.scope_id is not None
            )
        )
    ):
        raise ValueError("HTTPS endpoints require an explicit public unicast IP")
    return str(ip)


def _validate_endpoints(endpoints: Mapping[str, _HttpsEndpoint]) -> dict:
    validated = {}
    for name, endpoint in endpoints.items():
        if type(name) is not str or not name.isidentifier():
            raise ValueError("HTTPS resource names must be identifiers")
        if not isinstance(endpoint, _HttpsEndpoint):
            raise TypeError("HTTPS grants must declare an exact URL and IP")
        url = httpx.URL(endpoint.url)
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.fragment
            or not 0 < (url.port if url.port is not None else 443) <= 65535
        ):
            raise ValueError(
                "HTTPS endpoints must be absolute HTTPS URLs without credentials or fragments"
            )
        validated[name] = (url, _public_address(endpoint.address))
    return validated


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, host: str, port: int, address: str):
        self._host, self._port, self._address = host, port, address
        self._backend = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self._host or port != self._port or local_address is not None:
            raise PermissionError("HTTPS connection destination is not granted")
        # HTTPcore retains the original host for Host, SNI and certificate checking;
        # only the socket address changes. No hostname DNS lookup or proxy is used.
        return await self._backend.connect_tcp(
            self._address, port, timeout=timeout, socket_options=socket_options
        )


class _HttpsBroker:
    """GET only exact configured URLs using pinned public IPs and verified TLS.

    Workers supply resource names, never URLs, headers, request bodies or IPs.
    Redirects, proxies, cookies, environment credentials and compression are not
    enabled. DNS changes require a new trusted grant. This is not a URL filter for
    arbitrary HTTP clients, nor a grant of direct worker network access.
    """

    def __init__(
        self,
        endpoints: Mapping[str, _HttpsEndpoint],
        *,
        max_response_bytes: int = 1024 * 1024,
        timeout_s: float = 10,
        ca_file: Path | None = None,
    ):
        if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 4 MiB")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self._endpoints = _validate_endpoints(endpoints)
        self._limit = max_response_bytes
        self._timeout = timeout_s
        # Explicit CA input avoids SSL_CERT_FILE/SSL_CERT_DIR environment overrides.
        self._tls = ssl.create_default_context(cafile=str(ca_file) if ca_file else certifi.where())

    async def fetch(self, name: str) -> dict:
        """Fetch one approved resource, returning status, content type and raw bytes."""
        if type(name) is not str or name not in self._endpoints:
            raise PermissionError("HTTPS resource is not granted")
        url, address = self._endpoints[name]
        async with asyncio.timeout(self._timeout):
            async with httpcore.AsyncConnectionPool(
                ssl_context=self._tls,
                network_backend=_PinnedBackend(
                    url.raw_host.decode("ascii"), url.port or 443, address
                ),
                max_connections=1,
                max_keepalive_connections=0,
                http1=True,
                http2=False,
                retries=0,
            ) as pool:
                async with pool.stream(
                    "GET",
                    str(url),
                    headers={"Accept-Encoding": "identity", "Connection": "close"},
                    extensions={
                        "timeout": dict.fromkeys(
                            ("connect", "read", "write", "pool"), self._timeout
                        )
                    },
                ) as response:
                    if 300 <= response.status < 400:
                        raise PermissionError("HTTPS redirects are not permitted")
                    headers = {name.lower(): value for name, value in response.headers}
                    if any(
                        name.lower() == b"content-encoding" and value.lower() != b"identity"
                        for name, value in response.headers
                    ):
                        raise ValueError("HTTPS compressed responses are not permitted")
                    body = bytearray()
                    async for chunk in response.aiter_stream():
                        if len(body) + len(chunk) > self._limit:
                            raise ValueError("HTTPS response exceeds max_response_bytes")
                        body.extend(chunk)
                    return {
                        "status": response.status,
                        "content_type": headers.get(b"content-type", b"").decode("latin-1"),
                        "body": bytes(body),
                    }
