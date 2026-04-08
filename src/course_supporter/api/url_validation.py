"""URL validation utilities for preventing SSRF attacks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import anyio
from fastapi import HTTPException

# Private/reserved IP ranges that must be blocked
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


async def validate_webhook_url(url: str) -> str:
    """Validate a webhook URL against SSRF risks.

    Checks:
    - Scheme must be https (or http for local dev)
    - Hostname must not resolve to private/reserved IPs
    - Hostname must not be in blocked list (localhost, metadata endpoints)

    DNS resolution runs in a thread to avoid blocking the event loop.

    Args:
        url: The webhook URL to validate.

    Returns:
        The normalized URL if valid.

    Raises:
        HTTPException: 422 if the URL is invalid or targets a blocked address.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=422,
            detail="Invalid webhook URL format.",
        )

    if parsed.scheme not in ("https", "http"):
        raise HTTPException(
            status_code=422,
            detail="Webhook URL must use https scheme.",
        )

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=422,
            detail="Webhook URL must include a hostname.",
        )

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Webhook URL hostname '{hostname}' is not allowed.",
        )

    # Resolve hostname in a thread to avoid blocking the event loop
    try:
        addr_infos = await anyio.to_thread.run_sync(
            lambda: socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        )
    except socket.gaierror:
        raise HTTPException(  # noqa: B904
            status_code=422,
            detail=f"Cannot resolve webhook URL hostname '{hostname}'.",
        )

    for addr_info in addr_infos:
        ip = ipaddress.ip_address(addr_info[4][0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise HTTPException(
                    status_code=422,
                    detail="Webhook URL must not target private/internal addresses.",
                )

    return url
