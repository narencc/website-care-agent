"""Outbound URL policy for intake URLs, crawling and browser subrequests.

Only http(s) destinations whose every resolved address is public are allowed.
Loopback, private, link-local (including cloud metadata), multicast and
reserved addresses are blocked, unless the origin is explicitly listed in
FELUDA_APPROVED_TEST_ORIGINS (the local demo site, approved test sites).

Known limit: the browser resolves DNS itself, so a host that changes its
answer between our check and the browser's connection (DNS rebinding) is only
caught on its next request. Production capture should run behind an egress
proxy that enforces the same policy at connection time.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .settings import app_settings


class BlockedURL(ValueError):
    pass


_dns_cache: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL_S = 60.0


def origin_of(url: str) -> str:
    p = urlsplit(url)
    port = p.port
    default = {"http": 80, "https": 443}.get(p.scheme)
    netloc = (p.hostname or "").lower()
    if port and port != default:
        netloc += f":{port}"
    return f"{p.scheme}://{netloc}"


def _resolve(host: str) -> list[str]:
    now = time.monotonic()
    hit = _dns_cache.get(host)
    if hit and now - hit[0] < _DNS_TTL_S:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"cannot resolve host {host!r}") from e
    addrs = sorted({info[4][0] for info in infos})
    _dns_cache[host] = (now, addrs)
    return addrs


def _is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def is_approved_test_origin(url: str) -> bool:
    return origin_of(url) in app_settings().approved_test_origins


def check_url(url: str, *, resolve: bool = True) -> str:
    """Return the URL if policy allows fetching it, else raise BlockedURL."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {p.scheme!r} is not allowed")
    if not p.hostname:
        raise BlockedURL("URL has no host")
    if p.username or p.password:
        raise BlockedURL("credentials in URLs are not allowed")
    if is_approved_test_origin(url):
        return url
    host = p.hostname.lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost") or host.endswith(".internal"):
        raise BlockedURL(f"host {host!r} is not allowed")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(str(literal)):
            raise BlockedURL(f"address {host} is not public")
        return url
    if resolve:
        for addr in _resolve(host):
            if not _is_public(addr):
                raise BlockedURL(f"host {host!r} resolves to non-public address {addr}")
    return url


def normalize_site_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise BlockedURL("website URL is required")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlsplit(raw)
    path = p.path or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")


def same_origin(a: str, b: str) -> bool:
    return origin_of(a) == origin_of(b)


def guarded_get(url: str, *, max_redirects: int = 5, timeout: float = 15.0, headers: dict | None = None) -> httpx.Response:
    """GET following redirects manually so every hop passes the policy."""
    current = url
    hdrs = {"User-Agent": "FeludaCareAgent/0.1 (+verification; read-only)"}
    hdrs.update(headers or {})
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            check_url(current)
            resp = client.get(current, headers=hdrs)
            if resp.is_redirect and "location" in resp.headers:
                current = urljoin(current, resp.headers["location"])
                continue
            return resp
    raise BlockedURL(f"too many redirects from {url}")
