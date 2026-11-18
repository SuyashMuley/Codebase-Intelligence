"""Never send a user's key, or private code, to an internal address.

Provider addresses come from a fixed catalogue, so a user cannot choose a host. This is the second
line of defence: an address is used only if it is https, on the catalogue's host list, has no
credentials or odd port, and every IP its name resolves to is a public one (so a poisoned or
rebound DNS answer pointing at 127.0.0.1, a cloud metadata address or a private network is
refused). HTTP clients are also built with redirects off, so an allowed host cannot bounce the
request somewhere else.
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from app.core.errors import InvalidProviderConfig, ProviderAddressRefused
from app.services.providers.catalog import allowed_hosts

Resolver = Callable[[str], list[str]]


def _system_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


# Replaceable in tests, which must not depend on DNS.
resolver: Resolver = _system_resolver


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is still loopback
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """Raise ProviderAddressRefused unless this URL is safe to call with a user's key."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise ProviderAddressRefused("That provider address is not allowed.") from None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or host not in allowed_hosts()
    ):
        raise ProviderAddressRefused("That provider address is not allowed.")
    try:
        addresses = resolver(host)
    except OSError:
        raise ProviderAddressRefused("Could not look up the provider's address.") from None
    if not addresses or not all(is_public_ip(a) for a in addresses):
        raise ProviderAddressRefused(
            "The provider's address resolves to a private or internal network, so it was refused."
        )


def require_valid(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidProviderConfig(message)
