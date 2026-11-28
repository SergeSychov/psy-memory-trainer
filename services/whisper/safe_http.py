"""Исходящий HTTP(S) с защитой от SSRF.

Сервис whisper стоит во внутренней сети рядом с n8n. Ссылку присылает пользователь,
поэтому любой исходящий запрос по ней идёт только через этот модуль:

* схема только https, порт только 443 (иное задаёт Policy, и только в тестах);
* DNS резолвится здесь, проверяются ВСЕ адреса. Один запрещённый адрес закрывает хост;
* соединение идёт на уже проверенный IP (пиннинг). Имя хоста остаётся только для
  заголовка Host, SNI и проверки сертификата, повторного резолва нет (DNS rebinding);
* редиректы не следуются автоматически: максимум 5, каждый заново разбирается,
  резолвится и проверяется;
* в логи уходит только короткий хеш URL и причина, сам URL и имя файла не пишутся.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("psy.whisper.source")

MAX_REDIRECTS = 5
MAX_URL_LEN = 2048
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "psy-whisper/1"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

_BLOCKED_V4 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, metadata облаков
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/128",
        "::1/128",
        "::/96",  # IPv4-compatible (устар.)
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",  # Teredo
        "2001:2::/48",
        "2001:db8::/32",
        "fc00::/7",  # ULA
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (устар.)
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_BLOCKED_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa")
_HOST_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class SourceError(Exception):
    """Ошибка источника. code уходит клиенту как {"error": code}, reason — только в лог."""

    def __init__(self, code: str, status: int, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.reason = reason or code


@dataclass(frozen=True)
class Policy:
    """Строгая политика по умолчанию. Ослабляется только в тестах."""

    allow_http: bool = False
    ports: frozenset[int] | None = frozenset({443})  # None — любой порт
    allow_ips: frozenset[str] = frozenset()  # адреса, исключённые из блок-листа
    resolver: Callable[[str, int], list[str]] | None = None
    ssl_context: ssl.SSLContext | None = None
    max_redirects: int = MAX_REDIRECTS


STRICT = Policy()


def url_tag(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:8]


def _fail(code: str, status: int, reason: str, url: str = "") -> SourceError:
    log.warning("source reject reason=%s tag=%s", reason, url_tag(url) if url else "-")
    return SourceError(code, status, reason)


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True, если адрес нельзя использовать как внешний. Не-публичное считается запрещённым."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:  # ::ffff:a.b.c.d
            return is_blocked_ip(ip.ipv4_mapped)
        if ip in _NAT64:  # 64:ff9b::a.b.c.d
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _SIX_TO_FOUR:  # 2002:aabb:ccdd::
            return is_blocked_ip(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
        if any(ip in net for net in _BLOCKED_V6):
            return True
    else:
        if any(ip in net for net in _BLOCKED_V4):
            return True
    return not ip.is_global


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # для Host/SNI (ASCII, нижний регистр)
    port: int
    path: str  # путь + query

    @property
    def tls(self) -> bool:
        return self.scheme == "https"


def parse_target(url: str, policy: Policy = STRICT) -> Target:
    """Разбор и проверка схемы, порта, userinfo и имени. Сеть не трогает."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise _fail("unsupported_link", 400, "bad_url")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise _fail("unsupported_link", 400, "control_chars", url)
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise _fail("unsupported_link", 400, "bad_url", url) from None
    scheme = parts.scheme.lower()
    if scheme != "https" and not (scheme == "http" and policy.allow_http):
        raise _fail("unsupported_link", 400, "scheme", url)
    if "@" in parts.netloc:
        raise _fail("unsupported_link", 400, "userinfo", url)
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise _fail("unsupported_link", 400, "no_host", url)
    literal = _parse_ip(host)
    if literal is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _fail("unsupported_link", 400, "bad_host", url) from None
        if (
            host == "localhost"
            or host.endswith(_BLOCKED_NAME_SUFFIXES)
            or len(host) > 253
            or not all(_HOST_LABEL.match(label) for label in host.split("."))
        ):
            raise _fail("unsupported_link", 400, "blocked_name", url)
    else:
        host = str(literal)
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if policy.ports is not None and effective_port not in policy.ports:
        raise _fail("unsupported_link", 400, "port", url)
    safe = "/%:@!$&'()*+,;=-._~"
    path = urllib.parse.quote(parts.path or "/", safe=safe)
    if parts.query:
        path += "?" + urllib.parse.quote(parts.query, safe=safe + "?")
    return Target(scheme, host, effective_port, path)


def resolve_safe(target: Target, policy: Policy = STRICT) -> list[str]:
    """Резолвит имя и проверяет ВСЕ адреса. Возвращает уникальные разрешённые IP."""
    literal = _parse_ip(target.host)
    if literal is not None:
        addrs = [str(literal)]
    else:
        resolver = policy.resolver or default_resolver
        try:
            addrs = list(dict.fromkeys(resolver(target.host, target.port)))
        except (OSError, UnicodeError):
            raise _fail("download_failed", 502, "dns_failed") from None
    if not addrs:
        raise _fail("download_failed", 502, "dns_empty")
    result: list[str] = []
    for addr in addrs:
        ip = _parse_ip(addr)
        if ip is None:
            raise _fail("unsupported_link", 400, "bad_address")
        if str(ip) not in policy.allow_ips and is_blocked_ip(ip):
            raise _fail("unsupported_link", 400, "blocked_address")
        result.append(str(ip))
    return result


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pin = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pin, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, ctx: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._pin = ip
        self._ctx = ctx

    def connect(self) -> None:
        raw = socket.create_connection((self._pin, self.port), self.timeout)
        try:
            # SNI и проверка сертификата идут по имени, TCP — на закреплённый IP.
            self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class SafeResponse:
    """Ответ без автоматических редиректов. Читать кусками, закрывать."""

    def __init__(
        self,
        conn: http.client.HTTPConnection,
        resp: http.client.HTTPResponse,
        url: str,
        deadline: float | None,
    ) -> None:
        self._conn = conn
        self._resp = resp
        self.url = url
        self.status = resp.status
        self.headers = resp.headers
        self._deadline = deadline

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()

    @property
    def content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is not None and raw.strip().isdigit():
            return int(raw.strip())
        return None

    def filename(self) -> str | None:
        return filename_from_disposition(self.headers.get("Content-Disposition"))

    def read(self, size: int) -> bytes:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise SourceError("timeout", 504, "deadline")
        try:
            return self._resp.read(size)
        except (TimeoutError, socket.timeout):
            raise SourceError("timeout", 504, "read_timeout") from None
        except (OSError, http.client.HTTPException):
            raise SourceError("download_failed", 502, "read_failed") from None

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self) -> "SafeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    star = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.IGNORECASE)
    if star:
        try:
            return clean_filename(urllib.parse.unquote(star.group(2).strip().strip('"'), encoding=star.group(1) or "utf-8"))
        except (LookupError, UnicodeError):
            pass
    plain = re.search(r'filename\s*=\s*(?:"([^"]*)"|([^;]+))', value, re.IGNORECASE)
    if plain:
        return clean_filename((plain.group(1) or plain.group(2) or "").strip())
    return None


def clean_filename(name: str) -> str | None:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if ord(c) >= 0x20 and ord(c) != 0x7F).strip()
    return name[:255] or None


def _connect_pinned(
    target: Target, ips: list[str], policy: Policy, timeout: float
) -> http.client.HTTPConnection:
    ctx = policy.ssl_context or ssl.create_default_context()
    last: Exception | None = None
    for ip in ips:
        conn: http.client.HTTPConnection
        if target.tls:
            conn = _PinnedHTTPS(target.host, target.port, ip, timeout, ctx)
        else:
            conn = _PinnedHTTP(target.host, target.port, ip, timeout)
        try:
            conn.connect()
            return conn
        except (OSError, ssl.SSLError) as exc:
            last = exc
            conn.close()
    if isinstance(last, TimeoutError):
        raise SourceError("timeout", 504, "connect_timeout")
    raise SourceError("download_failed", 502, "connect_failed")


def _remaining(deadline: float | None, timeout: float) -> float:
    if deadline is None:
        return timeout
    left = deadline - time.monotonic()
    if left <= 0:
        raise SourceError("timeout", 504, "deadline")
    return max(0.5, min(timeout, left))


def open_url(
    url: str,
    policy: Policy = STRICT,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    deadline: float | None = None,
) -> SafeResponse:
    """GET с пиннингом IP и ручными редиректами. Возвращает только ответ 200/206."""
    current = url
    for hop in range(policy.max_redirects + 1):
        target = parse_target(current, policy)
        ips = resolve_safe(target, policy)
        wait = _remaining(deadline, timeout)
        conn = _connect_pinned(target, ips, policy, wait)
        try:
            req_headers = {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            req_headers.update(headers or {})
            conn.request("GET", target.path, headers=req_headers)
            resp = conn.getresponse()
        except (TimeoutError, socket.timeout):
            conn.close()
            raise SourceError("timeout", 504, "read_timeout") from None
        except (OSError, http.client.HTTPException):
            conn.close()
            raise SourceError("download_failed", 502, "request_failed") from None
        status = resp.status
        if status in REDIRECT_STATUSES:
            location = resp.getheader("Location")
            resp.close()
            conn.close()
            if hop >= policy.max_redirects:
                raise _fail("download_failed", 502, "too_many_redirects", url)
            if not location:
                raise _fail("download_failed", 502, "redirect_no_location", url)
            current = urllib.parse.urljoin(current, location.strip())
            continue
        if status in (200, 206):
            return SafeResponse(conn, resp, current, deadline)
        resp.close()
        conn.close()
        if status in (401, 403, 404, 410):
            raise _fail("not_public", 403, f"http_{status}", url)
        if status == 429:
            raise _fail("download_failed", 429, "rate_limited", url)
        raise _fail("download_failed", 502, f"http_{status}", url)
    raise _fail("download_failed", 502, "too_many_redirects", url)  # pragma: no cover


def read_json(
    url: str,
    policy: Policy = STRICT,
    *,
    timeout: float = 30.0,
    deadline: float | None = None,
    max_bytes: int = 1024 * 1024,
) -> dict:
    import json

    with open_url(url, policy, headers={"Accept": "application/json"}, timeout=timeout, deadline=deadline) as resp:
        body = b""
        while len(body) <= max_bytes:
            chunk = resp.read(65536)
            if not chunk:
                break
            body += chunk
        if len(body) > max_bytes:
            raise _fail("download_failed", 502, "json_too_large", url)
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise _fail("download_failed", 502, "json_invalid", url) from None
    if not isinstance(value, dict):
        raise _fail("download_failed", 502, "json_shape", url)
    return value


__all__ = [
    "HTML_TYPES",
    "MAX_REDIRECTS",
    "Policy",
    "STRICT",
    "SafeResponse",
    "SourceError",
    "Target",
    "is_blocked_ip",
    "open_url",
    "parse_target",
    "read_json",
    "resolve_safe",
    "url_tag",
]
