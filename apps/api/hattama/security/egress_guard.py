"""In-process egress guard for tests/smoke runs: any non-loopback connection raises.

Defence in depth only — production isolation is the network policy (deploy/compose.yaml internal network).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager


class EgressBlockedError(ConnectionError):
    pass


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@contextmanager
def block_external_egress(attempts: list[str] | None = None) -> Iterator[list[str]]:
    attempts = attempts if attempts is not None else []
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def check(address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host.startswith("/"):  # unix socket
            return
        if not _is_loopback(host):
            attempts.append(str(host))
            raise EgressBlockedError(f"external connection blocked: {host}")

    def connect(self, address):  # type: ignore[no-untyped-def]
        check(address)
        return orig_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        check(address)
        return orig_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host is not None and not _is_loopback(host):
            attempts.append(str(host))
            raise EgressBlockedError(f"DNS lookup blocked: {host}")
        return orig_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = orig_getaddrinfo  # type: ignore[assignment]
