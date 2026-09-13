"""Destination screening, NOT connection-destination enforcement.

Do not use successful screening as permission to call route.continue_().
Production connections are owned by SafeFetchBroker's retained-IP connector;
Chromium only renders inert HTML offline.
"""

import ipaddress
import re
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .errors import BrowserError, BrowserFailure

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_INTERNAL = ("localhost", "localdomain", "local", "internal", "lan", "home", "test", "invalid", "onion", "arpa")
Resolver = Callable[[str, int], Iterable[str]]


def public_address(value: str) -> bool:
    try:
        if type(value) is not str or "%" in value:
            return False
        address = ipaddress.ip_address(value)
        # IPv4-mapped/translation/tunnel addresses complicate destination
        # interpretation. This first boundary refuses them entirely.
        return (address.is_global and not address.is_multicast and not address.is_reserved
                and not (address.version == 4 and any(address in ipaddress.ip_network(net) for net in
                    ("192.0.0.0/24", "192.88.99.0/24")))
                and not (address.version == 6 and (address not in ipaddress.ip_network("2000::/3")
                    or address in ipaddress.ip_network("2001::/23")
                    or address in ipaddress.ip_network("3fff::/20")))
                and not (isinstance(address, ipaddress.IPv6Address) and
                         (address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo
                          or address in ipaddress.ip_network("64:ff9b::/96")
                          or address in ipaddress.ip_network("64:ff9b:1::/48"))))
    except ValueError:
        return False


def canonical_url(value: str, *, max_chars: int = 2048) -> str:
    """Conservative ASCII HTTP(S), canonical IP/domain syntax, ports 80/443."""
    deny = BrowserError(BrowserFailure.DESTINATION)
    if (type(value) is not str or not value or len(value) > max_chars
            or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or "\\" in value):
        raise deny
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in ("http", "https") or not host
                or parts.username is not None or parts.password is not None
                or "%" in parts.netloc or "@" in parts.netloc
                or parts.netloc.endswith(":") or host.endswith(".")):
            raise deny
        port = parts.port
        if port not in (None, 80 if parts.scheme == "http" else 443):
            raise deny
        host = host.lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (len(host) > 253 or len(labels) < 2
                    or any(not _LABEL.fullmatch(label) for label in labels)
                    or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
                    or any(host == item or host.endswith("." + item) for item in _INTERNAL)):
                raise deny
            authority = host
        else:
            if not public_address(str(address)) or host != str(address):
                raise deny
            authority = f"[{address}]" if address.version == 6 else str(address)
        # Redundant default ports are omitted. Fragments do not cause HTTP I/O.
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        raise deny from None


def display_url(value: str, *, max_chars: int = 2048) -> str:
    """Metadata omits path, query and fragment, all of which can carry secrets."""
    if value == "about:blank":
        return value
    try:
        parts = urlsplit(canonical_url(value, max_chars=max_chars))
        return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    except BrowserError:
        return "[redacted]"


def system_resolver(host: str, port: int) -> Iterable[str]:
    return tuple(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))


class BrowserNetworkPolicy:
    """Screen every request/redirect. DNS checking is supplementary only.

    This interface has no resolver deadline or connection authority. The
    production broker wraps system resolution in its owned deadline worker
    and connects exact retained validated IPs instead.
    """

    def __init__(self, resolver: Resolver = system_resolver):
        self._resolver = resolver

    def screen_request(self, url: str, method: str = "GET") -> str:
        if method not in ("GET", "HEAD"):
            raise BrowserError(BrowserFailure.DESTINATION)
        normalized = canonical_url(url)
        parts = urlsplit(normalized)
        host = parts.hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            try:
                addresses = tuple(self._resolver(host, 80 if parts.scheme == "http" else 443))
            except Exception:
                raise BrowserError(BrowserFailure.DNS) from None
            if not addresses or any(not public_address(item) for item in addresses):
                raise BrowserError(BrowserFailure.DESTINATION)
        return normalized
