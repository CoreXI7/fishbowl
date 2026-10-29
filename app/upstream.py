"""Talking to upstream services safely: openers that never follow redirects off-host, and helpers that only let
the expected types through from upstream JSON (it is attacker-controlled: a taken-over container can answer anything)."""
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

INSECURE = ssl.create_default_context()      # upstreams are on the LAN, often with self-signed certs
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE
UA = "homelab-dash"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx comes back as an HTTPError. A service could otherwise send the dash to an
    internal address (and the integrations' API keys with it)."""
    def redirect_request(self, *args, **kwargs):
        return None


NOFOLLOW = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=INSECURE))   # LAN upstreams
STRICT = urllib.request.build_opener(_NoRedirect)                                                    # integrations


def fetch_same_host(url, base, name, limit, timeout=5):
    """(content type, body) of a GET, following at most 3 redirects and only while they stay on base's scheme and
    host:port. None if it fails or leaves that host."""
    origin = urllib.parse.urlparse(base)[:2]
    for _ in range(4):
        if urllib.parse.urlparse(url)[:2] != origin:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Host": name})
        try:
            with NOFOLLOW.open(req, timeout=timeout) as r:
                return r.headers.get("Content-Type", ""), r.read(limit)
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


def read_capped(r, limit):
    """A response body, but at most limit bytes: a hostile upstream could send gigabytes (the container has 256 MB)."""
    data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"answer bigger than {limit // MB} MB")
    return data


def text(v, limit=300):
    """A string from an upstream JSON field, clipped; anything else (lists, objects, numbers) becomes ""."""
    return v[:limit] if isinstance(v, str) else ""
