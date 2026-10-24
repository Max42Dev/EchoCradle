"""Worker-only network guard; SkinTokens' local bpy HTTP server remains reachable.

Loaded by isolated Python workers and their Python descendants through PYTHONPATH.
This is a download guard for trusted workers, not a hostile-code sandbox.
"""
from __future__ import annotations

import ipaddress
import socket


def _local(host: object) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
_bind = socket.socket.bind


def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
        raise OSError("Visual workers are cached-only: non-loopback network is disabled")
    return _connect(self, address)


def connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
        raise OSError("Visual workers are cached-only: non-loopback network is disabled")
    return _connect_ex(self, address)


def getaddrinfo(host, *args, **kwargs):
    if host in ("", "0.0.0.0", "::"):
        host = "::1" if host == "::" else "127.0.0.1"
    if host is not None and not _local(host):
        raise OSError("Visual workers are cached-only: external DNS is disabled")
    return _getaddrinfo(host, *args, **kwargs)


def bind(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        # Upstream SkinTokens uses 0.0.0.0 for its pickle-based bpy IPC server.
        if address[0] in ("", "0.0.0.0", "::"):
            host = "::1" if self.family == socket.AF_INET6 else "127.0.0.1"
            address = (host, *address[1:])
        if not _local(address[0]):
            raise OSError("Visual worker servers must bind to loopback")
    return _bind(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.socket.bind = bind
socket.getaddrinfo = getaddrinfo