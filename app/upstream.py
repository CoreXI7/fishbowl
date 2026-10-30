"""Talking to upstream services safely: openers that never follow redirects off-host, and helpers that only let
the expected types through from upstream JSON (it is attacker-controlled: a taken-over container can answer anything)."""
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

INSECURE = ssl.create_default_context()      # upstreams are on the LAN, often with self-signed certs
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE
UA = "fishbowl"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx comes back as an HTTPError. A service could otherwise send the dash to an
    internal address (and the integrations' API keys with it)."""
    def redirect_request(self, *args, **kwargs):
        return None


# ---------- an overall deadline for every outbound request ----------
# urllib's timeout limits each read, not the whole request: a service sending one byte every few seconds held a
# request thread (and its connection slot) for ever, and enough of them made the dashboard stop answering. Every
# outbound connection gets a deadline of DEADLINE_FACTOR x its timeout; at the deadline its socket is shut down, so
# the read in progress fails at once.
DEADLINE_FACTOR = 3
_watched = {}                      # connection -> monotonic deadline
_watch_cond = threading.Condition()
_reaper = []


def _reap():
    with _watch_cond:
        while True:
            now = time.monotonic()
            for sock, at in [(k, t) for k, t in _watched.items() if t <= now]:
                _watched.pop(sock, None)
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)   # (the plain socket's: stops an SSL read too)
                except (OSError, TypeError):
                    pass
            _watch_cond.wait(min(_watched.values()) - now if _watched else None)


def watch(conn):
    """Shut conn's socket down DEADLINE_FACTOR x its timeout from now (called once it's connected). Only the socket is
    kept here: urllib drops conn.sock once the headers are in, while the body is still being read from it, and the
    connection itself holds its last answer and headers (kept to the deadline, 50 of them used up the memory)."""
    if not isinstance(conn.timeout, (int, float)) or conn.sock is None:
        return
    with _watch_cond:
        _watched[conn.sock] = time.monotonic() + DEADLINE_FACTOR * conn.timeout
        if not _reaper:
            _reaper.append(threading.Thread(target=_reap, name="upstream-deadlines", daemon=True))
            _reaper[0].start()
        _watch_cond.notify()


class _HeaderBudget:
    """A response's file while its status line and headers are read: HEADERS_MAX bytes in all (http.client alone takes
    100 lines of 64 KB, ~6 MB an answer)."""
    def __init__(self, f, n):
        self.f, self.left = f, n

    def readline(self, limit=-1):
        line = self.f.readline(self.left + 1 if limit < 0 else min(limit, self.left + 1))
        self.left -= len(line)
        if self.left < 0:
            raise http.client.HTTPException("the answer's headers are too big")
        return line

    def __getattr__(self, name):
        return getattr(self.f, name)


HEADERS_MAX = 64 * 1024


class StrictResponse(http.client.HTTPResponse):
    """A response whose headers are at most HEADERS_MAX and whose chunk sizes must be plain hex: http.client takes "-1"
    as "read to the end" (CVE-2026-103995, still in 3.12.16), which bypassed every byte cap here and let one hostile
    upstream use up the memory."""
    def begin(self):
        real = self.fp
        self.fp = _HeaderBudget(real, HEADERS_MAX)
        try:
            super().begin()
        finally:
            if self.fp is not None:   # (begin() may have closed it: an answer without a body)
                self.fp = real

    def _read_next_chunk_size(self):
        line = self.fp.readline(1025)
        if len(line) > 1024:
            raise ValueError("chunk size line too long")
        size = line.split(b";", 1)[0].strip()
        if not re.fullmatch(rb"[0-9A-Fa-f]{1,15}", size):
            raise ValueError("bad chunk size")   # (http.client turns it into IncompleteRead)
        return int(size, 16)


class DeadlineHTTPConnection(http.client.HTTPConnection):
    # (no unwatch on close(): http.client closes the connection object as soon as the headers of an answer that "will
    # close" are in, and its body is still being read; the entry just lapses at the deadline)
    response_class = StrictResponse

    def connect(self):
        super().connect()
        watch(self)


class DeadlineHTTPSConnection(http.client.HTTPSConnection):
    response_class = StrictResponse

    def connect(self):
        super().connect()
        watch(self)


class _DeadlineHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(DeadlineHTTPConnection, req)


class _DeadlineHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(DeadlineHTTPSConnection, req, context=self._context)


NOFOLLOW = urllib.request.build_opener(_NoRedirect, _DeadlineHTTP, _DeadlineHTTPS(context=INSECURE))   # LAN upstreams
STRICT = urllib.request.build_opener(_NoRedirect, _DeadlineHTTP, _DeadlineHTTPS())                      # integrations


def fetch_same_host(url, base, name, limit, timeout=5):
    """(content type, body) of a GET, following at most 3 redirects and only while they stay on base's scheme and
    host:port. None if it fails or leaves that host."""
    origin = urllib.parse.urlparse(base)[:2]
    for _ in range(4):
        if urllib.parse.urlparse(url)[:2] != origin:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Host": name})   # (it can refuse a URL too)
            t0 = time.monotonic()
            with NOFOLLOW.open(req, timeout=timeout) as r:
                body = r.read(limit)
                cut = r.length or time.monotonic() - t0 >= DEADLINE_FACTOR * timeout * 0.9   # (no length: ran out of time)
                return None if cut else (r.headers.get("Content-Type", ""), body)   # (cut short: no half an icon)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if 300 <= e.code < 400 else None
            e.close()
            if not loc:
                return None
            url = urllib.parse.urljoin(url, loc)
        except Exception:
            return None
    return None


# ---------- upstream data: attacker-controlled, so only the expected types get through ----------

def num(v, default=0):
    """A number from an upstream JSON field. Digit strings count (Tautulli sends many numbers as strings);
    anything else becomes default, so the page never gets text where it expects a number."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return v if v == v and abs(v) != float("inf") else default
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,18}(\.\d{1,9})?", v.strip()):
        return float(v) if "." in v else int(v)
    return default


MB = 1 << 20
QBIT_MAX, API_MAX = 32 * MB, 8 * MB   # biggest answer read from qBittorrent (~15,000 torrents) / Seerr and Tautulli


JSON_MAX_VALUES = 1_000_000   # a 1,500-torrent qBittorrent list has ~100,000; each value costs ~60 bytes once parsed


def read_capped(r, limit):
    """A response body, but at most limit bytes: a hostile upstream could send gigabytes (the container has 256 MB)."""
    data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"answer bigger than {limit // MB} MB")
    return data


def read_json(r, limit):
    """Parsed JSON from a response, read with read_capped(). The byte cap alone doesn't bound memory (8 MB of "[],"
    parses into ~180 MB of objects), so an answer with more values than JSON_MAX_VALUES is refused before parsing
    (a cheap upper bound: every value but the first follows a comma, opens a bracket or is a key)."""
    data = read_capped(r, limit)
    if data.count(b",") + data.count(b"[") + data.count(b"{") > JSON_MAX_VALUES:
        raise ValueError("answer has too many values")
    return json.loads(data) if data else {}


_INVISIBLE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def text(v, limit=300):
    """A string from an upstream JSON field, clipped, without control or bidi characters (a torrent name with RLO could
    pass for another); anything else (lists, objects, numbers) becomes ""."""
    return _INVISIBLE.sub("", v[:limit * 2])[:limit] if isinstance(v, str) else ""
