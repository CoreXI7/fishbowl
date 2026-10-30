"""Fishbowl: a glance dashboard for a self-hosted server.

Stdlib only (Python 3.11+). Settings are edited on the /settings page (stored in the data folder),
optionally seeded from config.toml; secrets can also come from environment variables.
Services are read from Nginx Proxy Manager's proxy_host/*.conf (mounted read-only), so new NPM
hosts show up automatically. Hardware/security stats come from the optional root collector
(see ../collector), which writes a JSON file this app reads.
"""
import gzip
import hashlib
import hmac
import http.client
import http.cookiejar
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings as cfgstore             # noqa: E402  (settings.py: load / validate / save)
import auth                             # noqa: E402  (auth.py: Fishbowl's own sign-in)
from hostinfo import (_live_prev, cpu_model, cpu_mhz, cpu_topology, cpu_usage,  # noqa: E402
                      disk_io, hwmon)
from upstream import (API_MAX, INSECURE, MB, NOFOLLOW, QBIT_MAX, STRICT, UA, _NoRedirect,  # noqa: E402
                      fetch_same_host, num, read_capped, read_json, text)

DEMO = cfgstore.DEMO
HW_FILE = os.environ.get("HW_FILE", "/hw/hw.json")
REQ_DIR = os.environ.get("REQ_DIR", "/req")
INFRA = cfgstore.infra()
NPM_DIR = INFRA["npm_dir"]
TRUSTED_PROXY = INFRA["trusted_proxy"]      # e.g. "nginx" or "nginx 10.250.81.3": only those hosts may talk to us
ALLOWED_ORIGINS = set(INFRA["origins"])
USER_HEADER = os.environ.get("USER_HEADER", "")   # e.g. Remote-User: logged only if your proxy sets (overwrites) it
LISTEN = os.environ.get("LISTEN", "")
# Hosts the dashboard can't reach directly (e.g. on a network only the proxy shares) are checked through the proxy,
# the way visitors reach them: "host" or "host/path" (path instead of /, useful when / sits behind the proxy's login).
# Set on the settings page (Services → Advanced), or by env CHECK_VIA_PROXY="a.example.com b.example.com/health",
# which then wins and locks the field.
VIA_PROXY_ENV = os.environ.get("CHECK_VIA_PROXY", "").split()
_cfg_lock = threading.Lock()

# Who may see the dashboard. AUTH=login (the default): Fishbowl's own sign-in (auth.py). AUTH=proxy: your reverse
# proxy's sign-in (Authelia, Authentik, ...) protects it and Fishbowl's own is off. Anything else counts as login
# (fail closed). The demo has neither: it holds no secrets and saves nothing.
_auth_env = os.environ.get("AUTH", "login").strip().lower() or "login"
AUTH = "off" if DEMO else ("proxy" if _auth_env == "proxy" and TRUSTED_PROXY else "login")
if _auth_env not in ("login", "proxy") and not DEMO:
    print(f"WARNING: AUTH={_auth_env[:20]!r} isn't login or proxy: using login (Fishbowl's own sign-in)", flush=True)
if _auth_env == "proxy" and not TRUSTED_PROXY and not DEMO:   # nothing would stand in front: never open
    print("WARNING: AUTH=proxy needs TRUSTED_PROXY (else anyone could skip the proxy's sign-in): using login", flush=True)
auth.configure(cfgstore.DATA_DIR)
ACCOUNT, SESSIONS, THROTTLE = auth.Account(), auth.Sessions(), auth.Throttle()
_pending = {}                 # sha256(pre-sign-in token) -> {"t", "keep", "tries", "dev", "epoch"}: the password was right, a code is due
_pending_totp = {}            # sha256(session token) -> {"t", "secret", "epoch"}: an authenticator being set up, not confirmed yet
_recent_fails = {}            # trusted-device hash -> [(time, "password" | "code")]: typos in that browser, forgiven at its next sign-in
_session_tries = {}           # sha256(session token) -> (wrong tries, last): re-confirming the password in Settings
SESSION_FAILS = 10            # wrong tries a signed-in browser gets before it's signed out
_pending_lock = threading.RLock()
_try_lock = threading.Lock()  # counting a try, and using up a code, happen one at a time
PENDING_FOR = 300             # seconds to type the code after the password
CODE_USED = "That code was just used. Wait for the next one in your app (a new one every 30 seconds)."
TOO_LATE = {"error": "The password or two-step sign-in changed meanwhile: reload the page (sign in again if asked) and "
                     "try again.", "restart": True}
# Reached without a session: the sign-in page and what it needs, the health check
PUBLIC_GET = {"/login", "/healthz", "/favicon.ico", "/style.css", "/themes.css", "/fonts.css", "/api/auth/state"}
# (and /static/ and every /js/ app script: see gate())
PUBLIC_POST = {"/api/login", "/api/login/code", "/api/setup-account", "/api/logout"}
FORWARD_HEADERS = ("X-Forwarded-For", "X-Real-IP", "X-Forwarded-Proto", "Forwarded")   # a proxy in front adds these
# "Your own network" for passwords over plain HTTP: private ranges, loopback, link-local. Spelled out: Python's
# is_private also counts TEST-NETs, 6to4/Teredo, 0.0.0.0, 240/4 and more.
OWN_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                                                   "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128")]
_NEXT_RE = re.compile(r"/(?:settings)?(?:\?[A-Za-z0-9_=&-]{1,100})?")


def safe_next(n):
    """Where to go after signing in: one of our own pages, with a plain query (?wall, ?security=fw); else "/"."""
    return n if isinstance(n, str) and _NEXT_RE.fullmatch(n) else "/"


def fmt_wait(sec):
    return f"{sec // 60} min {sec % 60} s" if sec >= 60 else f"{sec} s"


PROXY_HEADERS = ("X-Real-IP", "X-Forwarded-Proto")


def gap_hint(gaps):
    """What the proxy setup is missing, in a sentence or two (the sign-in page shows it and its refusals start with
    it), or "". The one place this is worded."""
    if "untrusted" in gaps:
        return ("The dashboard sits behind a proxy it wasn't told about, so it can't see where you connect from, and "
                "signing in is refused. Set TRUSTED_PROXY to the proxy's container name in docker-compose.yml.")
    what = {"X-Real-IP": "Your reverse proxy doesn't send X-Real-IP, so the dashboard can't tell visitors apart: they "
                         "share one count of wrong tries, and signing in over plain HTTP is refused.",
            "X-Forwarded-Proto": "Your reverse proxy doesn't send X-Forwarded-Proto, so the dashboard can't tell whether "
                                 "you use https, and signing in from outside your network is refused."}
    heads = [g for g in gaps if g in PROXY_HEADERS]
    if heads:
        return " ".join(what[g] for g in heads) + " See \"Signing in\" in docs/SETTINGS.md for the line to add."
    return ""


def local_name(host):
    """A Host no website can own: an IP address, localhost, a name without dots, or a LAN-only suffix. Over plain HTTP
    with ORIGIN unset, only these may sign in or change anything: a website can point its own name at your dashboard's
    address (DNS rebinding) and pass the Origin check with it."""
    h = (host or "").strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return bool(h) and ("." not in h.rstrip(".") or h.rstrip(".").endswith((".lan", ".local", ".home.arpa", ".internal", ".localhost")))


def apply_settings():
    """(Re)build everything that depends on settings. Called at start and after every save from the GUI."""
    global ED, STORE, SITE, SKIP_HOSTS, EXTRA_SERVICES, KNOWN_IPS, KNOWN, QBIT, SEERR, TAUTULLI, HOME_IP, SECRETS, qb, VIA_PROXY
    ED, STORE = cfgstore.load(), cfgstore.load_secrets()
    SITE = ED["site"]
    SKIP_HOSTS = set(ED["skip_hosts"])
    EXTRA_SERVICES = ED["services"]                     # [{name, url}] for hosts not in NPM
    KNOWN_IPS = ED["known_ips"]
    KNOWN = known_matcher(KNOWN_IPS)
    VIA_PROXY = dict((e.split("/", 1)[0], "/" + e.split("/", 1)[1] if "/" in e else "/") for e in VIA_PROXY_ENV or ED["via_proxy"])

    def integration(name):
        url, _ = cfgstore.url_of(name, ED)
        secrets = [cfgstore.secret(field, STORE)[0] for field, _env in cfgstore.INTEGRATIONS[name][1]]
        return {"url": url, "secrets": secrets, "on": bool(url and all(secrets)) or DEMO}

    QBIT, SEERR, TAUTULLI = integration("qbittorrent"), integration("seerr"), integration("tautulli")
    _answered.clear()                                    # new addresses/keys: the last tries don't count any more
    HOME_IP = os.environ.get("HOME_IP") or ED["qbittorrent"]["home_ip"]   # real WAN IP; qbit showing it = VPN leak
    SECRETS = sorted({v for i in (QBIT, SEERR, TAUTULLI) for v in i["secrets"] if len(v) >= 4}, key=len, reverse=True)
    qb = Qbit()                                          # fresh login with the (possibly new) credentials
    with _lock:
        _cache.clear()
    _icons.clear()
    _npm["sig"] = _hw["key"] = _sd["key"] = None       # skip list / known IPs may have changed: re-read them


_proxy_ip = {"ips": set(), "at": float("-inf")}


def scrub(text):
    """Remove anything secret from text that may reach the browser or logs (error messages can echo URLs)."""
    text = str(text)
    for v in SECRETS:
        text = text.replace(v, "***").replace(urllib.parse.quote_plus(v), "***").replace(urllib.parse.quote(v, safe=""), "***")
    text = re.sub(r"(?i)(api_?key|token|pass(word)?|secret)=[^&\s'\"]*", r"\1=***", text)
    return re.sub(r"(https?://[^\s'\"?]*)\?[^\s'\"]*", r"\1?…", text)[:300]


def log_safe(text, limit=300):
    """Text for the container log: clipped, with control and other unprintable characters shown as \\xNN,
    so a request path or header can't put terminal escape sequences or fake lines into the log."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text)[:limit])


def proxy_ips():
    """Every address of the trusted reverse proxy: TRUSTED_PROXY may list several names or IPs (Docker's DNS answers
    a container's name with its address on one shared network only, so a proxy on two networks may need its second
    address listed). Looked up at most once a minute (addresses change when it's recreated); for a name whose lookup
    fails, the last good answer is kept."""
    if time.monotonic() - _proxy_ip["at"] > 60:
        ips = set()
        for entry in TRUSTED_PROXY.split():
            try:
                found = {a[4][0] for a in socket.getaddrinfo(entry, None)}
                _proxy_ip.setdefault("by", {})[entry] = found
            except OSError:
                found = _proxy_ip.get("by", {}).get(entry, set())
            ips |= found
        _proxy_ip.update(ips=ips, at=time.monotonic())
    return _proxy_ip["ips"]


_gw = {"t": float("-inf"), "ip": None}


def default_gateway():
    """The container's default gateway (from /proc/net/route, looked up once a minute), or None. Every request comes
    from it with Docker's userland proxy or rootless Docker/Podman: then visitors can't be told apart."""
    if time.monotonic() - _gw["t"] > 60:
        _gw["t"], _gw["ip"] = time.monotonic(), None
        try:
            with open("/proc/net/route") as f:
                for line in f.read(65536).splitlines()[1:]:
                    p = line.split()
                    if len(p) > 2 and p[1] == "00000000" and p[2] != "00000000":
                        _gw["ip"] = socket.inet_ntoa(int(p[2], 16).to_bytes(4, "little"))
                        break
        except (OSError, ValueError):
            pass
    return _gw["ip"]


def allowed_ip(ip, local=None):
    """May this client talk to us at all? Always when no proxy is configured (LAN use) or in the demo, and when the
    connection comes from the dash itself (its source address = the address it arrived on: the health check of the
    dash's own proxy host connects to dash:8000)."""
    return not TRUSTED_PROXY or DEMO or ip == "127.0.0.1" or ip == local or ip in proxy_ips()


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: https://cdn.jsdelivr.net https://image.tmdb.org; "
       "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")



# ---------- services (from NPM + config) ----------

def parse_npm_conf(path):
    """One NPM proxy host file -> {name, upstream, auth}, or None if it isn't a plain proxy host."""
    try:
        with open(path) as f:
            txt = f.read()
    except OSError:
        return None
    name = re.search(r"server_name\s+([^\s;]+)", txt)
    server = re.search(r'set \$server\s+"?([^";]+)"?;', txt)
    port = re.search(r"set \$port\s+(\d+);", txt)
    scheme = re.search(r"set \$forward_scheme\s+(\w+);", txt)
    if not (name and server and port):
        return None
    return {
        "name": name.group(1),
        "upstream": f"{scheme.group(1) if scheme else 'http'}://{server.group(1)}:{port.group(1)}",
        # behind an SSO proxy: its auth_request block, or the include snippet Authelia's NPM guide uses
        "auth": bool(re.search(r"auth_request\s+/(authelia|authentik|outpost)|include\s+\S*(authelia|authentik)\S*\s*;", txt)),
        "login": has_login(txt),   # any sign-in in front of / (for the Setup check)
        # NPM's "Cache Assets": caches .css/.js/.png/... for 30 minutes whatever the app says (cookies and
        # Cache-Control ignored), so one visitor's images or a "sign in first" answer could reach everyone
        "caches": bool(re.search(r"^\s*include\s+conf\.d/include/assets\.conf\s*;", txt, re.M)),
    }


def has_login(txt):
    """Does this proxy host put a sign-in in front of / ? Comments and "off" don't count (quoted or not), and a
    directive counts at server level or in `location /` / `location ^~ /` (not in location /admin, nor `location = /`,
    which only covers the bare /). A `location /` that switches it off wins over server level. An include counts only
    if it's an auth-request snippet (Authelia's NPM guide: authelia-authrequest.conf; authelia-location.conf only defines
    the endpoint); an NPM access list counts when it asks for a user and password (auth_basic). Linear in the file."""
    # quoted text becomes a neutral word (so a brace or # inside quotes means nothing; "off" stays off), comments go
    txt = re.sub(r"""("(?:\\.|[^"\\])*(?:"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z))|#[^\n]*""",   # an unclosed quote runs to the end
                 lambda m: ("off" if m.group(1)[1:-1].strip("\"' ") == "off" else "quoted") if m.group(1) else "", txt)
    parts = re.split(r"([{};])", txt)
    stack, other, nloc = [], 0, 0          # blocks open; how many aren't server / location /; how many are locations
    server, root, root_off = False, False, False
    for head, sep in zip(parts[0::2], parts[1::2]):
        head = " ".join(head.split())
        if sep == "{":
            is_loc = head.startswith("location")
            fine = head.startswith("server") or (is_loc and re.fullmatch(r"location (\^~\s*)?/", head) is not None)
            stack.append((is_loc, fine))
            other += not fine
            nloc += is_loc
            continue
        if sep == "}":
            if stack:
                is_loc, fine = stack.pop()
                other -= not fine
                nloc -= is_loc
            continue
        if other:
            continue                               # another path, or limit_except/if/map: not for every request to /
        locs = nloc > 0
        word, _, arg = head.partition(" ")
        arg = arg.strip().strip("\"'")
        if word in ("auth_request", "auth_basic") and arg == "off":
            root_off = root_off or bool(locs)
            continue
        on = ((word == "auth_request" and arg) or (word == "auth_basic" and arg)
              or (word == "include" and re.search(r"auth[-_]?request", arg)))
        if on:
            root, server = (True, server) if locs else (root, True)
    return root or (server and not root_off)


_npm = {"sig": None, "hosts": [], "conf": [], "n_npm": 0}   # last host list (+ every parsed .conf, hidden ones too) + the folder state


def read_npm_hosts():
    """Every service to show: NPM's proxy hosts plus [[services]] from the settings, minus skipped ones.
    The .conf files are only re-read when one was added, removed or edited (checked by name + mtime)."""
    try:
        sig = tuple(sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(NPM_DIR) if e.name.endswith(".conf")))
    except OSError:
        sig = ()
    if sig != _npm["sig"]:
        conf = [h for h in (parse_npm_conf(os.path.join(NPM_DIR, n)) for n, _ in sig) if h]
        hosts = [h for h in conf if h["name"] not in SKIP_HOSTS]
        n_npm = len(hosts)
        for s in EXTRA_SERVICES:
            if s.get("name") and s.get("url") and s["name"] not in SKIP_HOSTS:
                hosts.append({"name": s["name"], "upstream": s["url"].rstrip("/"), "auth": bool(s.get("auth"))})
        hosts.sort(key=lambda h: (h["name"].split(".", 1)[-1], h["name"]))   # group by domain, then name
        _npm.update(sig=sig, hosts=hosts, conf=conf, n_npm=n_npm)
    return _npm["hosts"]


def check(host):
    """Is this service answering? Any HTTP reply below 500 counts as up (a login page is fine)."""
    r = check_once(host)
    if not r["up"] and r["code"] is None:   # retry once: socket-activated apps (e.g. Cockpit) wake slowly
        r = check_once(host, timeout=10)
    return r


class _SNIConnection(http.client.HTTPSConnection):
    """HTTPS to the proxy container while presenting the site's own name (proxies refuse unknown names)."""
    def __init__(self, host, sni, **kw):
        super().__init__(host, **kw)
        self.sni = sni

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.sni)


def check_via_proxy(host, path, timeout):
    t0 = time.monotonic()
    conn = _SNIConnection((TRUSTED_PROXY.split() or ["nginx"])[0], host["name"], port=443, timeout=timeout, context=INSECURE)
    try:
        conn.request("GET", path, headers={"Host": host["name"], "User-Agent": UA})
        code = conn.getresponse().status
    except Exception as e:
        return {"name": host["name"], "auth": host["auth"], "up": False, "code": None, "ms": None, "err": type(e).__name__}
    finally:
        conn.close()
    return {"name": host["name"], "auth": host["auth"], "up": code < 500, "code": code,
            "ms": round((time.monotonic() - t0) * 1000), "err": None}


def check_once(host, timeout=5):
    """One GET / straight to the service's upstream (what NPM forwards to), timed. A redirect counts as an answer
    (it isn't followed: wherever it points, the service itself is up)."""
    path = VIA_PROXY.get(host["name"])   # one lookup: a save from the settings page may swap the dict meanwhile
    if path:
        return check_via_proxy(host, path, timeout)
    t0 = time.monotonic()
    req = urllib.request.Request(host["upstream"] + "/", method="GET", headers={"User-Agent": UA, "Host": host["name"]})
    try:
        with NOFOLLOW.open(req, timeout=timeout) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code                     # 401/403/404 still means the app answered
    except Exception as e:
        return {"name": host["name"], "auth": host["auth"], "up": False, "code": None, "ms": None, "err": type(e).__name__}
    ms = round((time.monotonic() - t0) * 1000)
    return {"name": host["name"], "auth": host["auth"], "up": code < 500, "code": code, "ms": ms, "err": None}


_cache = {}                 # key -> (time it was computed, value)
_lock = threading.Lock()    # guards _cache and _key_locks
_key_locks = {}             # key -> lock held while that key is being recomputed


_answered = {}   # integration API path -> (did the last fetch work, when): the Setup check says if one stopped answering


def cached(key, ttl, fn):
    """fn(), reused for ttl seconds. When several requests miss at the same moment (the wall display plus a
    phone, say), only the first one runs fn; the others wait for it and share its result instead of each
    checking every service again."""
    def fresh():
        hit = _cache.get(key)
        return hit if hit and time.monotonic() - hit[0] < ttl else None

    with _lock:
        hit = fresh()
        if hit:
            return hit[1]
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _lock:
            hit = fresh()           # someone else refreshed it while we waited
        if hit:
            return hit[1]
        try:
            val = fn()
        except Exception:
            if key in FEATURE:
                _answered[key] = (False, time.time())
            raise
        if key in FEATURE:
            _answered[key] = (True, time.time())
        with _lock:
            _cache[key] = (time.monotonic(), val)
        return val


_icons = {}


def favicon(name):
    """Fetch a service's own favicon via its upstream (only for known hosts; no arbitrary URLs)."""
    if name in _icons:
        return _icons[name]
    host = next((h for h in read_npm_hosts() if h["name"] == name), None)
    if not host:
        return None
    base = host["upstream"]
    cands = []
    page = fetch_same_host(base + "/", base, name, 200_000)      # every fetch stays on the service's own upstream
    if page:
        html = page[1].decode("utf-8", "replace")
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            if re.search(r'rel=["\'][^"\']*icon', tag, re.I):
                href = re.search(r'href=["\']([^"\']+)', tag, re.I)
                if href and not href.group(1).startswith("data:"):
                    cands.append(urllib.parse.urljoin(base + "/", href.group(1)))
    cands.append(base + "/favicon.ico")
    result = None
    for url in cands:
        got = fetch_same_host(url, base, name, 512_000)
        ctype = got[0].split(";")[0] if got else ""
        if ctype.startswith("image/") and ctype != "image/svg+xml" and got[1]:
            result = (got[1], ctype)
            break
    _icons[name] = result
    return result


def services():
    """/api/services: check every service in parallel (cached 25 s, so this runs at most that often)."""
    hosts = read_npm_hosts()
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(check, hosts))
    return {"checked": int(time.time()), "services": results}




def known_matcher(entries):
    """Known-IP entries split for is_known(): full addresses in canonical form (IPv6 has many spellings), and
    prefixes ("203.0.113." / "2001:db8:"), lower-cased."""
    full, prefixes = set(), []
    for k in entries:
        try:
            full.add(str(ipaddress.ip_address(k)))
        except ValueError:
            prefixes.append(k.lower())
            if ":" in k and "::" not in k:   # also in exploded form: "2001:db8:0:" must match sshd's "2001:db8::5"
                prefixes.append(":".join(g.zfill(4) for g in k.lower().split(":")[:-1]) + ":")
    return full, tuple(prefixes)


def is_address(v):
    """A full IPv4/IPv6 address (not a prefix, not anything else)."""
    try:
        return isinstance(v, str) and bool(ipaddress.ip_address(v))
    except ValueError:
        return False


def is_known(ip):
    """Is this IP on your known list (an exact address, or inside one of the prefixes)?"""
    if not isinstance(ip, str) or not ip:
        return False
    full, prefixes = KNOWN
    try:
        addr = ipaddress.ip_address(ip)
        canon, exploded = str(addr), addr.exploded
    except ValueError:
        canon = exploded = ""
    return canon in full or ip.lower().startswith(prefixes) or bool(canon and (canon.startswith(prefixes) or exploded.startswith(prefixes)))


ACKS_FILE = os.path.join(cfgstore.DATA_DIR, "acks.json")   # logins you marked "looks fine" on the dashboard


def login_key(item):
    return hashlib.sha256(f"{item.get('time')}|{item.get('user')}|{item.get('ip')}".encode()).hexdigest()[:20]


def load_acks():
    try:
        return set(json.load(open(ACKS_FILE)))
    except Exception:
        return set()


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


_hw = {"key": None, "data": None}   # last parsed hw.json + the (hw.json, acks.json) mtimes it came from
SEC_DETAIL_FILE = os.environ.get("SEC_DETAIL_FILE", os.path.join(os.path.dirname(HW_FILE), "sec-detail.json"))
_sd = {"key": None, "data": None}   # the same for sec-detail.json (+ hw.json: which logins the card shows)


def read_hw():
    """The collector's latest hw.json (None if there's no collector), with every SSH/Authelia login marked
    known (IP on your list) and acked (you clicked "looks fine"). Only re-parsed when either file changed."""
    key = (_mtime(HW_FILE), _mtime(ACKS_FILE))
    if key[0] is None:
        return None
    if key != _hw["key"]:
        try:
            with open(HW_FILE) as f:
                hw = json.load(f)
        except Exception:
            return None
        sec = hw.get("security") or {}
        acks = load_acks()
        for item in (sec.get("ssh") or {}).get("logins", []) + (sec.get("authelia") or {}).get("recent", []):
            item["known"] = is_known(item.get("ip", ""))
            item["acked"] = login_key(item) in acks
        _hw.update(key=key, data=hw)
    hw = _hw["data"]
    hw["stale"] = time.time() - hw.get("updated", 0) > 60   # collector stopped writing
    return hw




def security_detail():
    """/api/security/detail: what the Security & backups detail views show, from the collector's sec-detail.json
    (rewritten every minute). Read-only on purpose: no parameters, no actions. IPs are marked known (on your list)
    and SSH logins acked (you clicked "looks fine"), like on the card. Re-parsed only when either file changed."""
    key = (_mtime(SEC_DETAIL_FILE), _mtime(ACKS_FILE), _mtime(HW_FILE))
    if key[0] is None:
        return {"missing": True}
    if key != _sd["key"]:
        try:
            with open(SEC_DETAIL_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {"missing": True, "error": "the collector's sec-detail.json can't be read"}
        acks = load_acks()
        # on_card = the card lists this login too, so its "Looks fine" button can clear it (the card shows the newest few)
        card = {login_key(l) for l in (((read_hw() or {}).get("security") or {}).get("ssh") or {}).get("logins", [])}
        ssh = d.get("ssh") or {}
        for item in ssh.get("logins", []):
            k = login_key({"time": item.get("raw"), "user": item.get("user"), "ip": item.get("ip")})
            item.update(known=is_known(item.get("ip", "")), acked=k in acks, on_card=k in card)
        auth = d.get("authelia") or {}
        for item in ssh.get("failed_ips", []) + ssh.get("sources", []) + auth.get("events", []) + (auth.get("fail_by_ip") or []):
            item["known"] = is_known(item.get("ip") or item.get("key") or "")
        _sd.update(key=key, data=d)
    return {**_sd["data"], "stale": time.time() - _sd["data"].get("updated", 0) > 180}


def ack_logins():
    """Mark every SSH login currently flagged as unknown as reviewed. Keys come from the server's own data,
    never from the browser, so a request can only acknowledge logins that really happened."""
    hw = read_hw() or {}
    logins = ((hw.get("security") or {}).get("ssh") or {}).get("logins", [])
    new = {login_key(l) for l in logins if not l.get("known")}
    if DEMO:
        return {"ok": True, "acked": len(new)}
    acks = load_acks() | new
    keep = sorted(acks)[-500:]                     # bounded: plenty for 7 days of logins
    tmp = ACKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(keep, f)
    os.replace(tmp, ACKS_FILE)
    return {"ok": True, "acked": len(new)}


def add_known_ips(ip=None, everything=False):
    """Put IPs on the known list from the SSH detail view: one (ip), or every unknown one (everything=True, "Add all").
    Only IPs that really logged in over SSH (the server's own data, never failed attempts) can be added,
    so a forged request can't trust an attacker."""
    detail = demo.security_detail() if DEMO else security_detail()
    ssh = detail.get("ssh") or {}
    # only what the detail view shows (its parser is the strict one), and only full addresses, never a prefix
    seen = {x.get("ip") for x in ssh.get("logins", []) + ssh.get("sources", []) if is_address(x.get("ip"))}
    if everything:
        want = sorted(i for i in seen if isinstance(i, str) and i and not is_known(i))
    elif not isinstance(ip, str) or ip not in seen:
        raise cfgstore.Invalid("that IP hasn't logged in over SSH in the last 7 days")
    else:
        want = [] if is_known(ip) else [ip]
    if not want or DEMO:
        return {"ok": True, "added": want}
    with _cfg_lock:
        new = cfgstore.validate({**ED, "known_ips": ED["known_ips"] + want}, ED)
        cfgstore.save(new, STORE)
        apply_settings()
    return {"ok": True, "added": want}


def system():
    """/api/system: CPU, memory, temps from the host's /proc and /sys (mounted into the container),
    plus everything the root collector reports (drives, GPU, security, backups, the usage breakdown)."""
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    load = open("/proc/loadavg").read().split()[:3]
    cpus = cpu_usage()
    return {
        "cpu": cpus.get("cpu"), "cores": [v for k, v in cpus.items() if k != "cpu"], "cpu_model": cpu_model(),
        "load": [float(x) for x in load], "uptime": float(open("/proc/uptime").read().split()[0]),
        "mem_total": mem["MemTotal"], "mem_avail": mem["MemAvailable"],
        "swap_total": mem.get("SwapTotal", 0), "swap_free": mem.get("SwapFree", 0),
        "temps": hwmon(), "hw": read_hw(),
    }


# ---------- live panel (/api/live, polled every second while the "live" window is open) ----------



def live():
    """/api/live: what the terminal `temps`-style live window shows, refreshed every second: every thread's clock and
    load grouped by CCD, temperatures, memory, disk I/O; package power + GPU come from the root collector."""
    hw = read_hw() or {}
    names = {os.path.basename(d["dev"]): d["name"] for d in hw.get("disks") or [] if d.get("dev")}
    cpus = cpu_usage(_live_prev)
    topo, mhz = cpu_topology(), cpu_mhz()
    threads = [{"use": cpus.get(f"cpu{i}", 0.0), "mhz": mhz[i] if i < len(mhz) else None,
                "group": topo[i]["group"] if i < len(topo) else 0, "core": topo[i]["core"] if i < len(topo) else i,
                "smt": topo[i]["smt"] if i < len(topo) else False}
               for i in range(len(cpus) - 1)]
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    return {
        "cpu": cpus.get("cpu"), "cpu_model": cpu_model(), "threads": threads, "temps": hwmon(),
        "mem_total": mem["MemTotal"], "mem_avail": mem["MemAvailable"],
        "load": [float(x) for x in open("/proc/loadavg").read().split()[:3]],
        "power": hw.get("power"), "gpu": hw.get("gpu"), "io": disk_io(names),
    }




# ---------- qBittorrent ----------

class Qbit:
    """qBittorrent Web API client. Keeps the login cookie and logs in again when it expires (403)."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)
        self.lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.rid, self.state = 0, {}        # sync/maindata position + hash -> the torrent fields the page shows

    def login(self):
        user, pw = QBIT["secrets"]
        data = urllib.parse.urlencode({"username": user, "password": pw}).encode()
        req = urllib.request.Request(QBIT["url"] + "/api/v2/auth/login", data=data, headers={"Referer": QBIT["url"]})
        with self.op.open(req, timeout=10) as r:
            read_capped(r, 64 * 1024)

    def get(self, path):
        with self.lock:
            for attempt in (1, 2):
                try:
                    with self.op.open(QBIT["url"] + path, timeout=10) as r:
                        return read_json(r, QBIT_MAX)
                except urllib.error.HTTPError as e:
                    if e.code == 403 and attempt == 1:
                        self.login()
                        continue
                    raise

    def torrents(self):
        """Every torrent, from sync/maindata (the feed qBittorrent's own web UI uses): the first call gets the full
        list, later ones only what changed since, which are merged in. Starts over whenever qBittorrent says
        full_update (e.g. after it restarted). Only the fields the page shows are kept."""
        with self.sync_lock:
            d = self.get(f"/api/v2/sync/maindata?rid={self.rid}")
            if not isinstance(d, dict):
                raise ValueError("unexpected answer from qBittorrent's sync API")
            if d.get("full_update") or not self.rid:
                self.state = {}
            for h, fields in (d.get("torrents") or {}).items():
                if isinstance(fields, dict):         # typed and clipped as they're stored: the map stays small
                    self.state.setdefault(h, {}).update((k, text(v) if k in SLIM_TEXT else num(v))
                                                        for k, v in fields.items() if k in SLIM_KEEP)
            for h in d.get("torrents_removed") or []:
                if isinstance(h, str):
                    self.state.pop(h, None)
            if len(self.state) > MAX_TORRENTS:       # a hostile qBittorrent adding torrents forever: start over
                self.rid, self.state = 0, {}
                raise ValueError(f"qBittorrent reports more than {MAX_TORRENTS} torrents")
            self.rid = d["rid"] if type(d.get("rid")) is int else 0
            return list(self.state.values())


SLIM_TEXT = ("name", "state", "category")
SLIM_NUM = ("progress", "dlspeed", "upspeed", "eta", "size", "num_seeds", "num_leechs", "ratio", "added_on", "uploaded")
SLIM_KEEP = frozenset(SLIM_TEXT + SLIM_NUM)
MAX_TORRENTS = 20_000


def qbit():
    """/api/qbit: speeds, the VPN check (external IP vs your home IP) and the torrent lists, trimmed to what the page shows."""
    t = qb.get("/api/v2/transfer/info")
    torrents = qb.torrents()
    torrents = torrents if isinstance(torrents, list) else []
    slim = [{**{k: text(x.get(k)) for k in SLIM_TEXT}, **{k: num(x.get(k)) for k in SLIM_NUM}}
            for x in torrents if isinstance(x, dict)]
    dl_states = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "checkingDL", "pausedDL", "stoppedDL"}
    downloading = [x for x in slim if x["state"] in dl_states]
    downloading.sort(key=lambda x: (-x["dlspeed"], -x["progress"]))
    seeding = [x for x in slim if x["upspeed"] > 0 and x["state"] not in dl_states]
    seeding.sort(key=lambda x: -x["upspeed"])
    t = t if isinstance(t, dict) else {}
    ext = text(t.get("last_external_address_v4"), 64)
    return {
        "connection": text(t.get("connection_status"), 40),
        "external_ip": ext,
        "vpn_check": bool(HOME_IP),
        "vpn_leak": bool(HOME_IP and ext == HOME_IP),
        "dl_speed": num(t.get("dl_info_speed")), "up_speed": num(t.get("up_info_speed")),
        "dl_total": num(t.get("dl_info_data")), "up_total": num(t.get("up_info_data")),
        "total": len(slim),
        "downloading": downloading, "seeding": seeding[:30], "seeding_active": len(seeding),
    }


# ---------- Plex via Tautulli ----------

def tautulli(cmd, **params):
    q = urllib.parse.urlencode({"apikey": TAUTULLI["secrets"][0], "cmd": cmd, **params})
    with STRICT.open(f"{TAUTULLI['url']}/api/v2?{q}", timeout=10) as r:
        return read_json(r, API_MAX)["response"]["data"]


PLEX_TEXT = ("session_key", "friendly_name", "full_title", "title", "grandparent_title", "year", "media_type", "state",
             "transcode_decision", "video_decision", "audio_decision", "stream_video_full_resolution",
             "video_full_resolution", "product", "player", "platform", "location")
PLEX_NUM = ("parent_media_index", "media_index", "progress_percent", "bandwidth", "duration", "view_offset")
PLEX_FLAG = ("transcode_hw_encoding", "transcode_hw_decoding")


def plex():
    """/api/plex: who's watching what, from Tautulli (so no Plex token is needed here)."""
    d = tautulli("get_activity")
    sessions = []
    d = d if isinstance(d, dict) else {}
    for s in d.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        x = {**{k: text(s.get(k) if k != "year" else str(s.get(k) or "")) for k in PLEX_TEXT}, **{k: num(s.get(k)) for k in PLEX_NUM},
             **{k: 1 if s.get(k) in (1, "1", True) else 0 for k in PLEX_FLAG}}
        thumb = text(s.get("grandparent_thumb") if s.get("media_type") == "episode" else s.get("thumb"))
        x["art"] = "/api/plexart?img=" + urllib.parse.quote(thumb, safe="") if thumb else None
        sessions.append(x)
    return {"stream_count": num(d.get("stream_count")), "transcodes": num(d.get("stream_count_transcode")),
            "bandwidth": num(d.get("total_bandwidth")), "sessions": sessions}


def plex_art(img):
    """Poster art goes through Tautulli's image proxy, so the Plex token never reaches the browser."""
    if not re.fullmatch(r"/library/metadata/\d+/(thumb|art)(/\d+)?", img or ""):
        return None
    q = urllib.parse.urlencode({"apikey": TAUTULLI["secrets"][0], "cmd": "pms_image_proxy", "img": img, "width": 200, "height": 300})
    with STRICT.open(f"{TAUTULLI['url']}/api/v2?{q}", timeout=10) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ART_TYPES:       # only raster images: an SVG or HTML answer could run script on our origin
            return None
        return r.read(2_000_000), ctype


ART_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# ---------- Seerr / Overseerr / Jellyseerr (same API) ----------

def seerr(path, method="GET"):
    req = urllib.request.Request(SEERR["url"] + "/api/v1" + path, method=method,
                                 headers={"X-Api-Key": SEERR["secrets"][0], "Accept": "application/json"})
    with STRICT.open(req, timeout=10) as r:
        return read_json(r, API_MAX)


_titles = {}


def media_info(kind, tmdb):
    """Title/year/poster for a request. Cached forever: a movie's title doesn't change."""
    key = (kind, tmdb)
    if kind not in ("movie", "tv") or not isinstance(tmdb, int) or isinstance(tmdb, bool):
        return {"title": "unknown title", "year": "", "poster": None, "overview": ""}
    if key not in _titles:
        if len(_titles) > 2000:          # a movie's title never changes, but a hostile Seerr could invent endless ids
            _titles.clear()
        try:
            d = seerr(f"/{kind}/{tmdb}")
            date = text(d.get("releaseDate")) or text(d.get("firstAirDate"))
            poster = text(d.get("posterPath"))
            _titles[key] = {"title": text(d.get("title"), 200) or text(d.get("name"), 200) or f"TMDB {tmdb}",
                            "year": date[:4] if re.fullmatch(r"\d{4}", date[:4]) else "",
                            "poster": poster if re.fullmatch(r"/[\w-]{1,100}\.(jpg|jpeg|png|webp)", poster) else None,
                            "overview": text(d.get("overview"), 280)}
        except Exception:
            return {"title": f"TMDB {tmdb}", "year": "", "poster": None, "overview": ""}
    return _titles[key]


def requests_pending():
    """/api/requests: pending Seerr requests with title, year, poster and who asked."""
    d = seerr("/request?take=50&filter=pending&sort=added")
    out = []
    for r in d.get("results") or []:
        # the id goes into approve/decline URLs and the type into Seerr API paths: anything unexpected is skipped
        if not isinstance(r, dict) or type(r.get("id")) is not int or r.get("type") not in ("movie", "tv"):
            continue
        m = r.get("media") if isinstance(r.get("media"), dict) else {}
        info = media_info(r["type"], m.get("tmdbId"))
        seasons = [s["seasonNumber"] for s in r.get("seasons") or [] if isinstance(s, dict) and type(s.get("seasonNumber")) is int]
        u = r.get("requestedBy") if isinstance(r.get("requestedBy"), dict) else {}
        out.append({"id": r["id"], "type": r["type"], "is4k": r.get("is4k") is True, "created": text(r.get("createdAt"), 40),
                    "user": text(u.get("displayName"), 200) or text(u.get("plexUsername"), 200) or text(u.get("email"), 200),
                    "seasons": seasons, **info})
    return {"pending": out}


# ---------- public (non-secret) config for the dashboard page ----------

def theme_view(tid):
    """A theme's personality: what you set for it > the theme's own defaults > (for some fields) the site's.
    Mascot, quotes, greeting and click emoji never fall back to another theme's, so Bubbles' mascot stays in Bubbles."""
    t = cfgstore.THEMES[tid]
    if tid == "bubbles":
        return {"label": t["label"], **{k: SITE[k] for k in cfgstore.THEME_FIELDS}}
    o = SITE["themes"].get(tid, {})
    own = {k: o.get(k) or t.get(k) or ("" if k != "quotes" else []) for k in cfgstore.THEME_FIELDS}
    for k in ("tagline", "tagline_down", "favicon"):
        own[k] = own[k] or SITE[k]
    own["click_emoji"] = own["click_emoji"] or SITE["click_emoji"]
    return {"label": t["label"], **own}


def page_config():
    return {
        "theme": SITE["theme"], "themes": {tid: theme_view(tid) for tid in cfgstore.THEMES},
        "title": SITE["title"], "tagline": SITE["tagline"], "tagline_down": SITE["tagline_down"] or "something's down!",
        "mascot": SITE["mascot"], "mascot_credit": SITE["mascot_credit"], "favicon": SITE["favicon"],
        "greeting": SITE["greeting"], "quotes": SITE["quotes"], "click_emoji": SITE["click_emoji"],
        "groups": ED["groups"], "apps": ED["apps"],
        "features": {"qbit": QBIT["on"], "seerr": SEERR["on"], "plex": TAUTULLI["on"]},
        "hidden_panels": SITE["hidden_panels"], "motion": SITE["motion"], "settings_ui": cfgstore.UI_ENABLED, "demo": DEMO,
        "auth": AUTH,
        "repo": os.environ.get("REPO_URL", "") if re.fullmatch(r"https://[\w./-]+", os.environ.get("REPO_URL", "")) else "",
    }


# ---------- settings page API ----------

def settings_view(host=None, https=None, proxy_gaps=()):
    """Everything the settings page needs. Secrets are reported only as set / not set, never their values."""
    secrets = {}
    for name, (_url_env, fields) in cfgstore.INTEGRATIONS.items():
        for field, _env in fields:
            val, locked = cfgstore.secret(field, STORE)
            secrets[field] = {"set": bool(val), "locked": locked}
    names = [n for n, _ in demo.SERVICES] if DEMO else [h["name"] for h in read_npm_hosts()]
    return {
        "settings": ED, "secrets": secrets,
        "locked_urls": {n: cfgstore.url_of(n, ED)[1] for n in cfgstore.INTEGRATIONS},
        "infra": {"origins": sorted(ALLOWED_ORIGINS), "trusted_proxy": TRUSTED_PROXY, "npm_dir": NPM_DIR},
        "ui_enabled": cfgstore.UI_ENABLED, "writable": os.access(cfgstore.DATA_DIR, os.W_OK),
        "discovered": sorted(set(names) | set(ED["skip_hosts"])), "collector": read_hw() is not None, "demo": DEMO,
        "themes": cfgstore.THEMES,   # built-in themes and their defaults (shown as placeholders in the editor)
        "panels": cfgstore.PANELS,
        "via_proxy_env": VIA_PROXY_ENV,   # set in the environment: wins over the page and locks the field
        "setup": setup_checks(host, https, proxy_gaps),
        "auth": AUTH,
        "collector": collector_view(),
        "collector_labels": COLLECTOR_LABELS,
        "about": {"listen": LISTEN or "0.0.0.0 (all addresses)", "user_header": USER_HEADER or "", "data_dir": cfgstore.DATA_DIR,
                  "npm_dir": NPM_DIR, "settings_ui": cfgstore.UI_ENABLED},
    }


COLLECTOR_REQ = os.path.join(REQ_DIR, "collector-settings.json")   # what the page changed, for the root collector
COLLECTOR_REQ_MAX = 200 * 1024
# The settings the page may change on the collector, with their shape. The collector checks every value again (it
# trusts nothing from here); this keeps the request, and what the page reads back, in shapes the page can show.
_STRS, _STRMAP, _NUM = ("list", str), ("map", str), ("num",)
COLLECTOR_KEYS = {
    "interval": int, "gpu": bool, "usage": bool, "net_if": str, "mounts": _STRS, "mount_labels": _STRMAP,
    "security.process_watch": bool, "security.cron_watch": bool, "security.firewall": bool, "security.details": bool,
    "security.fail2ban_jail": str, "security.ssh_unit": str, "security.authelia_container": str,
    "security.bad_process_names": _STRS, "security.authelia_db": str, "security.fail2ban_log": str,
    "security.immutable_files": _STRS, "security.watcher": dict, "backup.max_age_hours": _NUM, "backup.log": str,
    "backup.success_regex": str, "backup.failure_regex": str, "backup.checks": list, "backup.artifacts": list,
    "backup.jobs": list,
}


# what each collector setting is called on the page, and which section has it (Setup and the page use these)
COLLECTOR_LABELS = {
    "interval": "Update every", "gpu": "Show the NVIDIA GPU", "usage": "CPU and memory breakdown",
    "net_if": "Network interface", "mounts": "Drives to show", "mount_labels": "Drive names",
    "security.process_watch": "Crypto-miner watch", "security.cron_watch": "Crontab watch", "security.firewall": "Firewall",
    "security.details": "Detail views", "security.fail2ban_jail": "Blocked logins (fail2ban)",
    "security.ssh_unit": "Where SSH logins are logged", "security.authelia_container": "Authelia container",
    "security.bad_process_names": "Process names to flag", "security.authelia_db": "Authelia's database",
    "security.fail2ban_log": "fail2ban's log", "security.immutable_files": "Files that must stay locked",
    "security.watcher": "Your own watch script", "backup.max_age_hours": "A backup is overdue after",
    "backup.log": "Your backup script's log", "backup.success_regex": "A line that means the run went fine",
    "backup.failure_regex": "A line that means it failed", "backup.checks": "Backup checks",
    "backup.artifacts": "Backup files", "backup.jobs": "Backup jobs",
}
collector_section = lambda key: "#backups" if key.startswith("backup.") else "#security" if key.startswith("security.") else "#drives"


def _shape_ok(v, shape):
    if shape is int:
        return isinstance(v, int) and not isinstance(v, bool)
    if shape == _NUM:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if shape in (bool, str):
        return isinstance(v, shape)
    if shape == _STRS:
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if shape == _STRMAP:
        return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())
    if shape is dict:                                        # the watch script
        lists, maps = {"checks"}, {"kinds"}
        return isinstance(v, dict) and all(
            _shape_ok(x, _STRS if k in lists else _STRMAP if k in maps else (int if k == "every_minutes" else str))
            for k, x in v.items())
    if shape is list:                                        # backup checks / files / jobs: a list of small tables
        return isinstance(v, list) and all(isinstance(t, dict) and all(
            _shape_ok(x, _STRS if k == "files" else int if k == "min_count" else _NUM if k == "max_age_hours"
                      else bool if k == "size" else str)
            for k, x in t.items()) for t in v)
    return False


def collector_changes_clean(changes):
    """{key: {"from": the fingerprint of the value the page showed ("" = not set), "to": the new value}} for the keys
    the page may change, each in its shape. Anything else is refused, never passed on."""
    if not isinstance(changes, dict):
        raise cfgstore.Invalid("collector settings: must be an object")
    out = {}
    for k, c in changes.items():
        if k not in COLLECTOR_KEYS:
            raise cfgstore.Invalid(f"collector settings: unknown {str(k)[:60]}")
        label = COLLECTOR_LABELS.get(k, k)
        if not isinstance(c, dict) or set(c) != {"from", "to"} or c["to"] is None:
            raise cfgstore.Invalid(f"collector setting {label}: needs what it was and what it should be")
        if not isinstance(c["from"], str) or not re.fullmatch(r"(?:[0-9a-f]{16})?", c["from"]):   # the fingerprint it showed
            raise cfgstore.Invalid(f"collector setting {label}: needs what it was and what it should be")
        if not _shape_ok(c["to"], COLLECTOR_KEYS[k]):
            raise cfgstore.Invalid(f"collector setting {label}: wrong kind of value")
        out[k] = {"from": c["from"], "to": c["to"]}
    return out


def _clean_values(d):
    return {k: v for k, v in (d.items() if isinstance(d, dict) else ()) if k in COLLECTOR_KEYS and _shape_ok(v, COLLECTOR_KEYS[k])}


def _clean_detected(d):
    d = d if isinstance(d, dict) else {}
    strs = lambda v: [x for x in v if isinstance(x, str)][:200] if isinstance(v, list) else []
    dbs = d.get("authelia_db") if isinstance(d.get("authelia_db"), dict) else {}
    return {**{k: strs(d.get(k)) for k in ("mounts", "ifaces", "jails", "ssh_units", "authelia")},
            "authelia_db": {k: v for k, v in dbs.items() if isinstance(k, str) and isinstance(v, str)},
            "gpu": d.get("gpu") is True, "firewall": str(d.get("firewall") or "")[:40],
            "default_iface": str(d.get("default_iface") or "")[:40]}


def unhandled_request(last):
    """The changes in the request file if the collector hasn't handled it yet (its hash isn't the collector's "last")."""
    try:
        with open(COLLECTOR_REQ, "rb") as f:
            raw = f.read(COLLECTOR_REQ_MAX + 1)
        if len(raw) > COLLECTOR_REQ_MAX or hashlib.sha256(raw).hexdigest() == last:
            return {}
        return collector_changes_clean(json.loads(raw).get("changes"))
    except (OSError, ValueError, AttributeError, cfgstore.Invalid):
        return {}


def collector_view():
    """The collector's side of the settings page, from hw.json: what it found on the machine, what's in effect, which
    of those came from this page, and what waits for `--apply` or was refused. Plus what the page sent that the
    collector hasn't taken yet (it looks every few seconds)."""
    if DEMO:
        return demo.COLLECTOR
    hw = read_hw()
    c = hw.get("collector") if isinstance(hw, dict) else None
    view = {"installed": hw is not None, "supported": isinstance(c, dict) and c.get("v") == 3}
    if view["supported"]:   # root wrote it, but a version mismatch or bug must not break the page: keep only sane shapes
        strs = lambda v: [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
        last = c.get("last") if isinstance(c.get("last"), str) else ""
        fp = c.get("fp") if isinstance(c.get("fp"), dict) else {}
        view.update(detected=_clean_detected(c.get("detected")),
                    settings=_clean_values(c.get("settings")), pending_values=_clean_values(c.get("pending_values")),
                    fp={k: v for k, v in fp.items() if k in COLLECTOR_KEYS and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{16}", v)},
                    from_page=strs(c.get("from_page")), pending=strs(c.get("pending")),
                    rejected=[[str(r[0])[:80], str(r[1])[:300]] for r in c.get("rejected") or []
                              if isinstance(r, (list, tuple)) and len(r) == 2][:20] if isinstance(c.get("rejected"), list) else [],
                    rejected_more=c.get("rejected_more") if isinstance(c.get("rejected_more"), int) else 0,
                    error=str(c.get("error") or "")[:300], sandbox=str(c.get("sandbox") or "")[:40],
                    wanted={k: ch["to"] for k, ch in unhandled_request(last).items()})
    return view


def check_collector_changes(changes):
    """The request for the root collector, as text. Changes the collector hasn't taken yet from the last save stay in
    (the new save wins for a setting in both)."""
    if not cfgstore.UI_ENABLED:
        raise cfgstore.Invalid("the settings page is read-only (SETTINGS_UI=0)")
    new = collector_changes_clean(changes)
    hw = read_hw()
    c = hw.get("collector") if isinstance(hw, dict) else None
    last = c.get("last") if isinstance(c, dict) and isinstance(c.get("last"), str) else ""
    text = json.dumps({"v": 3, "id": os.urandom(8).hex(),   # its own id: the same change saved again is a new request
                       "changes": {**unhandled_request(last), **new}}, ensure_ascii=False)
    if len(text.encode()) > COLLECTOR_REQ_MAX:
        raise cfgstore.Invalid("collector settings: too big")
    return text


def write_collector_changes(text):
    tmp = f"{COLLECTOR_REQ}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, COLLECTOR_REQ)
    except OSError as e:
        raise cfgstore.Invalid(f"the collector's settings couldn't be handed over ({e.strerror}): is {REQ_DIR} mounted "
                               "and writable by the dashboard's user?")


COLLECTOR_WAIT = 6.0   # seconds a save waits for the collector to take its request


def wait_for_collector(text, wait=None):
    """After a save: give the collector a few seconds to take the request (it looks every round), so the page shows
    what's in effect, not what was in effect before."""
    want, end = hashlib.sha256(text.encode("utf-8")).hexdigest(), time.monotonic() + (COLLECTOR_WAIT if wait is None else wait)
    while time.monotonic() < end:
        hw = read_hw()
        c = hw.get("collector") if isinstance(hw, dict) else None
        if isinstance(c, dict) and c.get("last") == want:
            return True
        time.sleep(0.25)
    return False


def _ago(secs):
    secs = max(0, int(secs))
    return f"{secs} s" if secs < 120 else f"{secs // 60} min" if secs < 7200 else f"{secs // 3600} h"


DOCS = "https://github.com/CoreXI7/fishbowl/blob/main/docs/SETTINGS.md"


def setup_checks(host=None, https=None, proxy_gaps=()):
    """The settings page's Setup checklist: [{id, state, title, detail, why, steps: [{text, code}], link, doc, optional,
    compose}]. compose = its fix edits docker-compose.yml (the page then says once: run docker compose up -d).
    state: ok | problem (needs fixing) | warn (works, but worth fixing) | todo (optional, not set up).
    Facts and fixes only: no secrets, no file contents. "%ORIGIN%" in a code line = the page's own address.
    host = the Host the page was opened with (to find the dashboard's own proxy host and its sign-in); https = whether
    this browser reached it over HTTPS (for the sign-in's cookie)."""
    out = []

    def add(id, state, title, detail, steps=(), link="", doc="", optional=False, why="", compose=False, alt=""):
        out.append({"id": id, "state": state, "title": title, "detail": detail, "why": why, "compose": compose, "alt": alt,
                    "steps": [{"text": t, "code": c} for t, c in steps], "link": link, "doc": doc, "optional": optional})

    if DEMO:
        add("proxy", "ok", "Only your reverse proxy can connect", "Demo: the real one accepts only the proxy's address.")
        add("sso", "ok", "Sign-in in front of the dashboard", "Demo: the real one checks its proxy host for a sign-in step.")
        add("origin", "ok", "Changes only come from your dashboard's address", "Demo: the real one checks where every change comes from.")
        add("saving", "ok", "Settings can be saved", "Demo: changes are checked but never saved.")
        add("services", "ok", "Services found", f"{len(demo.SERVICES)} demo services.", link="#services")
        add("collector", "ok", "Hardware & security collector", "Demo: fake drives, temperatures and security data.")
        add("integrations", "ok", "Integrations", "Demo: torrents, requests and now playing are faked.", link="#integrations", optional=True)
        return out

    shown = read_npm_hosts()             # also fills _npm["conf"] (every proxy host, hidden ones too)
    find_proxy = ("Find your reverse proxy's container name (for Nginx Proxy Manager, the one running jc21/nginx-proxy-manager):",
                  "docker ps --format '{{.Names}}  {{.Image}}'")
    if not TRUSTED_PROXY:   # (then AUTH is login: AUTH=proxy needs TRUSTED_PROXY, and the demo returned above)
        add("proxy", "warn", "The dashboard can be reached without your reverse proxy",
            "TRUSTED_PROXY is empty, so anything that can reach port 8000 gets the sign-in page directly, without your "
            "proxy's HTTPS in between. Fishbowl's own sign-in still protects everything.",
            [find_proxy, ("In docker-compose.yml, change the TRUSTED_PROXY line to that name:", "TRUSTED_PROXY: <your proxy's container name>")],
            compose=True)
    elif not proxy_ips():
        add("proxy", "problem", "The trusted proxy can't be found",
            f"TRUSTED_PROXY is {TRUSTED_PROXY!r}, but that name doesn't resolve, so every connection is refused (except the "
            "container's own health check). Either the name is wrong, or the two containers don't share a Docker network.",
            [find_proxy, ("If the name is different, change the TRUSTED_PROXY line in docker-compose.yml to it.", ""),
             ("If they don't share a network, connect the proxy to the dashboard's network (its name is under networks in "
              "docker-compose.yml). The dashboard looks the name up again within a minute; add that network to the "
              "proxy's own compose file too, or it's lost when the proxy is recreated.",
              "docker network connect <network> <your proxy's container name>")], compose=True)
    else:
        add("proxy", "ok", "Only your reverse proxy can connect", f"Connections are accepted only from {TRUSTED_PROXY}.")

    name = (host or "").rsplit(":", 1)[0].lower() if host and not host.startswith("[") else ""
    mine = next((h for h in _npm["conf"] if h["name"].lower() == name), None) if name else None
    if AUTH == "login":
        acct = ACCOUNT.get()   # (this page needs a session, so the file is fine: a damaged one signs everyone out)
        if acct:
            add("signin", "ok", "Fishbowl's own sign-in protects the dashboard",
                f"Account {acct['user'][:64]}; authenticator app {'on' if acct['totp'].get('on') else 'off'}.", link="#account")
            if acct.get("failures", 0) >= auth.LOCK_AT:
                add("locked", "problem", "Sign-ins from new browsers are locked",
                    f"{acct['failures']} wrong tries in a row from browsers that never signed in here. Browsers that did "
                    "(like this one) still can. If those tries weren't yours, someone is guessing: keep two-step sign-in on.",
                    [("When you're ready, unlock it on the host:", "docker exec -it <container> python /app/auth.py unlock")])
            if (acct.get("notice") or {}).get("code"):
                add("password-out", "problem", "Someone may know your password",
                    f"The right password was typed with a wrong code {acct['notice']['code']} time(s), and not just before one of "
                    "your own sign-ins. If that wasn't you, change the password. If it was, choose \"That was me\" in Account.",
                    link="#account")
            if not acct["totp"].get("on"):
                add("totp", "warn", "Turn on two-step sign-in",
                    "Signing in needs only the password. With an authenticator app, a stolen or guessed password isn't enough.",
                    link="#account")
        if _auth_env == "proxy":
            add("auth-proxy", "warn", "AUTH: proxy is set, but Fishbowl's own sign-in is on",
                "AUTH: proxy needs TRUSTED_PROXY too (otherwise anyone could skip your proxy's sign-in), so the dashboard "
                "uses its own sign-in instead.", [("In docker-compose.yml, set TRUSTED_PROXY to your proxy's container name:",
                                                   "TRUSTED_PROXY: <your proxy's container name>")], compose=True)
        heads = [g for g in proxy_gaps if g in PROXY_HEADERS]
        if heads:
            add("realip", "warn", f"Your proxy leaves out {' and '.join(heads)}",
                " ".join(({"X-Real-IP": "Without X-Real-IP every visitor shares one count of wrong tries, and nobody counts "
                                        "as being on your own network.",
                           "X-Forwarded-Proto": "Without X-Forwarded-Proto the dashboard can't tell HTTPS visitors from "
                                                "plain-HTTP ones, so signing in from outside your network is refused."}[g]
                          for g in heads)) + " Nginx Proxy Manager sends both.",
                [("If you use Caddy: in the reverse_proxy block for the dashboard, add:", "header_up X-Real-IP {remote_host}"),
                 ("Or, if you use HAProxy: in the dashboard's backend, add:",
                  "http-request set-header X-Real-IP %[src]\nhttp-request set-header X-Forwarded-Proto https if { ssl_fc }"),
                 ("Or, with any other proxy: send X-Real-IP (the visitor's address) and X-Forwarded-Proto (http or https).", "")])
        if "gateway" in proxy_gaps:
            add("gateway", "warn", "Every visitor looks like the same address",
                "This request came from the container's own gateway, as every request does with Docker's userland proxy or "
                "rootless Docker/Podman. Then the dashboard can't tell visitors apart: they share one count of wrong tries, "
                "and all of them count as being on your own network (so passwords are accepted over plain HTTP).",
                [("Put your reverse proxy in front (same Docker network) and set TRUSTED_PROXY to its container name:",
                  "TRUSTED_PROXY: <your proxy's container name>")], compose=True)
        if https is False:
            add("https", "warn", "Signing in over plain HTTP",
                "This browser reached the dashboard without HTTPS, so the password and the sign-in cookie travel "
                "unencrypted on your network, and your browser also sends that cookie to every other web app on the "
                "same address (any port); it only works from this browser's address. (Does the address bar show "
                "https? Then your proxy tells the dashboard otherwise, for example when HTTPS ends before it: make it "
                "send X-Forwarded-Proto: https.)",
                [("Reach it through your reverse proxy's https address, and set ORIGIN in docker-compose.yml to that address:",
                  "ORIGIN: https://dash.example.com")], compose=True)
        if mine and mine.get("caches"):
            add("cache", "warn", "Turn off Cache Assets for the dashboard",
                f"{mine['name']}'s proxy host has Nginx Proxy Manager's Cache Assets on: it keeps images and files for 30 "
                "minutes for everyone, ignoring the dashboard's sign-in, so uploaded images could reach people who aren't "
                "signed in.", [("In Nginx Proxy Manager, edit that proxy host and switch off Cache Assets.", "")])
        if mine and mine.get("login"):
            add("sso", "ok", "Your proxy asks for a sign-in too",
                f"{mine['name']}'s proxy host has a sign-in step as well. If that's enough for you, AUTH: proxy in "
                "docker-compose.yml turns Fishbowl's own off (one sign-in instead of two).", optional=True)
    elif mine and mine.get("login"):
        add("sso", "ok", "Sign-in in front of the dashboard", f"{mine['name']}'s proxy host in Nginx Proxy Manager has a sign-in step.")
    elif mine:
        add("sso", "warn", "No sign-in found in front of the dashboard",
            f"{mine['name']}'s proxy host in Nginx Proxy Manager has no sign-in step (Authelia, Authentik or an access list), "
            "and AUTH: proxy turns Fishbowl's own sign-in off, so anyone who can reach that address sees it and can change "
            "these settings. (Using another kind of sign-in? Then this check can't see it.)",
            [("In Nginx Proxy Manager, open that proxy host and add your sign-in: your SSO's snippet under Advanced "
              "(Authelia and Authentik both have an NPM guide), or an Access List with a username and password.", ""),
             ("Or turn Fishbowl's own sign-in back on: remove the AUTH: proxy line from docker-compose.yml.", "")])
    elif name:
        add("sso", "todo", "Couldn't check the sign-in",
            f"{name[:100]} isn't one of Nginx Proxy Manager's proxy hosts here, so this page can't see what's in front of it. "
            "AUTH: proxy turns Fishbowl's own sign-in off: make sure your sign-in (SSO) is in front of it.")

    if ALLOWED_ORIGINS:
        add("origin", "ok", "Changes only come from your dashboard's address", ", ".join(sorted(ALLOWED_ORIGINS)))
    else:
        add("origin", "warn", "Set the dashboard's address",
            "Tell the dashboard its own address, so it only accepts changes made there.",
            [("In docker-compose.yml, change the ORIGIN line to the address you normally open the dashboard at "
              "(several: separate them with spaces). This page is open at:", "ORIGIN: %ORIGIN%")],
            why="Without ORIGIN, a change (a save, an approval, adding a known IP) is accepted from any page on the same "
                "address as the request. That already stops ordinary cross-site tricks; your exact address also stops "
                "DNS-rebinding tricks and http/https mix-ups.", compose=True)

    if not cfgstore.UI_ENABLED:
        add("saving", "ok", "The settings page is read-only",
            "SETTINGS_UI=0: change settings in data/settings.json, then run docker restart dash (it reads hand edits only at start).")
    elif os.access(cfgstore.DATA_DIR, os.W_OK):
        add("saving", "ok", "Settings can be saved", f"In the data folder ({cfgstore.DATA_DIR} inside the container; ./data next to "
            "docker-compose.yml with the example compose file).")
    else:
        add("saving", "problem", "Settings can't be saved",
            f"The data folder (./data next to docker-compose.yml, {cfgstore.DATA_DIR} inside the container) isn't writable by "
            "the container's user (uid 1000), so Save, uploads and \"add to known IPs\" fail.",
            [("On the host, in the folder with docker-compose.yml, then click Check again (no restart needed):", "sudo chown -R 1000:1000 ./data")])

    npm_ok = os.path.isdir(NPM_DIR) and os.access(NPM_DIR, os.R_OK | os.X_OK)
    from_npm = _npm["n_npm"] if npm_ok else 0            # counted as read_npm_hosts() builds the list (not by name)
    extra, in_npm = len(shown) - from_npm, len(_npm["conf"]) if npm_ok else 0
    hidden = in_npm - from_npm
    by_hand = [] if not cfgstore.UI_ENABLED else [("No NPM? Add your services by hand in Services → Services not in NPM.", "")]
    if from_npm or extra or hidden:
        add("services", "ok", "Services found",
            (f"{from_npm} from Nginx Proxy Manager" + (f" ({hidden} hidden)" if hidden else "")
             if npm_ok else "Nginx Proxy Manager isn't connected")
            + (f", {extra} added by hand." if extra else "."), link="#services")
    elif npm_ok:
        add("services", "warn", "No services yet",
            f"The Nginx Proxy Manager folder is mounted ({NPM_DIR}), but it holds no proxy hosts. If NPM does have proxy hosts, the path "
            "on the left of the /npm line in docker-compose.yml is wrong (Docker quietly creates an empty folder there).",
            [("On the host, check that NPM's folder holds its 1.conf, 2.conf… (your path, as on the left of the /npm line):",
              "ls /path/to/npm/data/nginx/proxy_host/"),
             ("Fix the path in docker-compose.yml.", "")],
            alt="Or, without NPM: delete the /npm line (Docker made that empty folder), and add your services by hand in "
                "Services → Services not in NPM." if by_hand else "", link="#services", compose=True)
    else:
        add("services", "problem", "No services yet",
            f"Services are read from Nginx Proxy Manager's proxy_host folder, which isn't mounted at {NPM_DIR}.",
            [("Using Nginx Proxy Manager? In docker-compose.yml, set the left side of the /npm line to the proxy_host folder "
              "inside NPM's data folder (the one that holds 1.conf, 2.conf…):", f"- /path/to/npm/data/nginx/proxy_host:{NPM_DIR}:ro")],
            alt="Or, without NPM: delete that line, and add your services by hand in Services → Services not in NPM."
                if by_hand else "", link="#services", compose=True)

    hw = read_hw()
    install = [("On the host, in the folder you cloned (the one with docker-compose.yml). It needs Python 3.11+ and systemd, "
                "and it runs as root, so this one step needs sudo:", "sudo bash collector/install.sh"),
               ("Keep these two lines under volumes in docker-compose.yml (the example has them):",
                "- /var/lib/homelab-dash:/hw:ro\n- /var/lib/homelab-dash/req:/req"),
               ("Then choose what it checks (drives, fail2ban, Authelia, backups) in its config file:",
                "sudo nano /etc/homelab-dash/collector.toml"),
               ("And restart it:", "sudo systemctl restart homelab-dash-collector")]
    if hw is None and not os.path.exists(HW_FILE):
        add("collector", "todo", "Drives, GPU and security need the collector",
            "Drives and disk health, the GPU, the CPU and memory breakdown, package power and the Security & backups panels "
            "come from a small helper on the host (CPU and board temperatures work without it). It isn't installed, or its "
            "folder isn't mounted.", install, optional=True, compose=True)
    elif hw is None:
        add("collector", "problem", "The collector's file can't be read",
            f"{HW_FILE} is there, but the dashboard can't read it" + ("." if os.access(HW_FILE, os.R_OK) else
            f" (it runs as uid {os.getuid()}). The collector makes the file readable by dash_uid from its config, which "
            "must match the user: line in docker-compose.yml (1000 by default)."),
            [("Check dash_uid:", "sudo grep dash_uid /etc/homelab-dash/collector.toml"),
             ("Then restart the collector:", "sudo systemctl restart homelab-dash-collector")])
    else:
        age = time.time() - num(hw.get("updated"), 0)
        sd_age = time.time() - (_mtime(SEC_DETAIL_FILE) or 0) / 1e9
        if age >= 120:
            add("collector", "problem", "The collector stopped updating",
                f"Its last update was {_ago(age)} ago, so drives, the GPU and Security & backups show old data.",
                [("On the host, see why:", "sudo systemctl status homelab-dash-collector"),
                 ("Its log:", "sudo journalctl -u homelab-dash-collector -n 30")])
        elif sd_age >= 600:
            add("collector", "warn", "Security details aren't being updated",
                f"The collector is running (updated {_ago(age)} ago), but the Security & backups detail views get no new data.",
                [("Update the collector: on the host, in the folder you cloned (the one with docker-compose.yml), run the "
                  "install command again (it also updates). Or check that details isn't set to false in "
                  "/etc/homelab-dash/collector.toml.", "sudo bash collector/install.sh")])
        else:
            add("collector", "ok", "Hardware & security collector",
                f"Connected: updated {_ago(age)} ago; security details {_ago(sd_age)} ago.")
        cv = collector_view()
        if not cv["supported"]:
            add("collector_page", "todo", "Update the collector to set it up here",
                "This collector is older than the settings page: drives, security checks and backups can only be set in "
                "/etc/homelab-dash/collector.toml until it's updated.",
                [("On the host, in the folder you cloned (the one with docker-compose.yml):", "sudo bash collector/install.sh")],
                optional=True)
        else:
            sb = cv.get("sandbox")
            if sb == "none":
                add("sandbox", "todo", "The collector runs without the extra sandbox",
                    "When it was installed, no stricter sandbox passed the collector's self-test on this machine, so it has "
                    "the plain settings: it can't change /usr, /etc or anyone's home, but nothing stops it using the network.",
                    [("To try again (on the host, in the folder you cloned):", "sudo bash collector/sandbox.sh")], optional=True)
            elif sb:
                add("sandbox", "ok", "The collector is sandboxed", f"Level: {sb} (no network, a read-only system"
                    + (", only the root powers it needs)." if sb == "strict" else ")."), optional=True)
            if cv.get("pending"):
                n = len(cv["pending"])
                names = ", ".join(COLLECTOR_LABELS.get(k, k) for k in cv["pending"][:6])
                add("collector_pending", "warn", f"{n} collector setting{'s' if n > 1 else ''} wait{'' if n > 1 else 's'} for your OK on the host",
                    f"Waiting: {names}. Files and patterns make the collector (it runs as root) read files and run searches, so "
                    "a person at the host approves them. The command shows exactly what changes and asks before applying.",
                    [("On the host:", "sudo homelab-dash-collector --apply")], link=collector_section(cv["pending"][0]))
            if cv.get("error"):
                add("collector_error", "warn", "The collector can't use the page's settings right now", cv["error"],
                    link="#drives")
            if cv.get("rejected"):
                add("collector_refused", "warn", "The collector refused some settings",
                    "; ".join(f"{COLLECTOR_LABELS.get(k, k)}: {why}" for k, why in cv["rejected"][:5])[:600],
                    link=collector_section(cv["rejected"][0][0]))

    on = {"Torrents": QBIT["on"], "Requests": SEERR["on"], "Now playing": TAUTULLI["on"]}
    paths = {"Torrents": "/api/qbit", "Requests": "/api/requests", "Now playing": "/api/plex"}
    failing = [k for k, v in on.items() if v and not _answered.get(paths[k], (True, 0))[0]
               and time.time() - _answered[paths[k]][1] < 900]
    if failing:
        add("integrations", "warn", "An integration isn't answering",
            f"Not answering: {', '.join(failing)} (on its last try). Test in Integrations says why: usually an address the "
            "dashboard's container can't reach, or a wrong key.", link="#integrations")
    elif any(on.values()):
        add("integrations", "ok", "Integrations",
            "Set up: " + ", ".join(k for k, v in on.items() if v)
            + (". Not set up: " + ", ".join(k for k, v in on.items() if not v) if not all(on.values()) else "")
            + ". Test in Integrations checks that each one answers.", link="#integrations", optional=True)
    else:
        add("integrations", "todo", "Integrations",
            "Connect qBittorrent, Seerr/Overseerr/Jellyseerr or Tautulli to show torrents (with the VPN-leak check), requests "
            "and Plex now playing. Until then those panels stay hidden.", link="#integrations", optional=True)

    if TRUSTED_PROXY:
        addrs = LISTEN.split()
        if addrs and not any(a in ("0.0.0.0", "::", "*") for a in addrs):
            add("listen", "ok", "Answers only on its own addresses",
                f"Answers only on {', '.join(addrs)}. Other containers are locked out as long as only your proxy shares that network.",
                optional=True)
        else:
            add("listen", "todo", "Optional: lock the dashboard to the proxy",
                "Other containers on the same Docker network are refused, but one that fakes the proxy's address could get "
                "past that. A network that only the proxy shares closes it.", doc=DOCS + "#lock-the-dash-to-the-proxy-listen", optional=True)
    return out


def test_integration(name):
    """Try an integration with its SAVED settings (never an address typed in, so keys can't be sent elsewhere)."""
    if DEMO:
        return {"ok": True, "detail": "demo mode"}
    i = {"qbittorrent": QBIT, "seerr": SEERR, "tautulli": TAUTULLI}.get(name)
    if not i:
        raise cfgstore.Invalid("unknown integration")
    r = _test_integration(name, i)
    if i["on"]:
        _answered[{"qbittorrent": "/api/qbit", "seerr": "/api/requests", "tautulli": "/api/plex"}[name]] = (r["ok"], time.time())
    return r


def _test_integration(name, i):
    if not i["on"]:
        return {"ok": False, "detail": "not configured: needs a URL and its key(s)"}
    try:
        if name == "qbittorrent":
            d = qbit()
            detail = f"connected · {d['total']} torrents · external IP {d['external_ip'] or '?'}"
        elif name == "seerr":
            d = seerr("/status")
            detail = f"connected · version {d.get('version', '?')}"
        else:
            d = tautulli("get_activity")
            detail = f"connected · {d.get('stream_count', 0)} streams now"
        return {"ok": True, "detail": detail}
    except urllib.error.HTTPError as e:
        hint = (" (wrong key or login?)" if e.code in (401, 403) else
                " (it redirects: use the address it redirects to)" if 300 <= e.code < 400 else "")
        return {"ok": False, "detail": f"HTTP {e.code}{hint}"}
    except urllib.error.URLError as e:
        return {"ok": False, "detail": scrub(why_no_answer(e.reason, i["url"]))}
    except (TimeoutError, ConnectionError, socket.gaierror) as e:
        return {"ok": False, "detail": scrub(why_no_answer(e, i["url"]))}
    except Exception as e:
        return {"ok": False, "detail": scrub(f"{type(e).__name__}: {e}")}


def why_no_answer(reason, url):
    """What a failed connection usually means, for the Test button."""
    u = urllib.parse.urlparse(url)
    if isinstance(reason, socket.gaierror):
        return (f"Can't find '{u.hostname}': the dashboard only knows container names on Docker networks it shares. "
                "Use your server's LAN IP, or put the app on one of the dashboard's networks.")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "No answer in time: a wrong address or port, or a firewall (like UFW) that blocks containers from reaching it."
    if isinstance(reason, ConnectionRefusedError):
        return f"Nothing answers at {u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}: is the app running, and is that its port?"
    return f"{type(reason).__name__}: {reason}"


IMAGE_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "gif": b"GIF8", "webp": b"RIFF"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MAX_UPLOADS, MAX_UPLOAD_TOTAL = 200, 100 * MB      # mascots and icons: plenty, but can't fill the disk


def save_upload(data):
    """Store an uploaded image (mascot or icon). Raster formats only: SVG can carry scripts."""
    ext = next((e for e, magic in IMAGE_MAGIC.items() if data.startswith(magic)), None)
    if ext == "webp" and data[8:12] != b"WEBP":
        ext = None
    if not ext:
        raise cfgstore.Invalid("only PNG, JPEG, GIF or WebP images")
    name = hashlib.sha256(data).hexdigest()[:16] + "." + ext
    os.makedirs(cfgstore.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(cfgstore.UPLOAD_DIR, name)
    if not os.path.exists(path):
        files = [e for e in os.scandir(cfgstore.UPLOAD_DIR) if e.is_file(follow_symlinks=False)]
        if len(files) >= MAX_UPLOADS:
            raise cfgstore.Invalid(f"too many uploads ({MAX_UPLOADS}): delete some from the data folder's uploads/")
        if sum(e.stat(follow_symlinks=False).st_size for e in files) + len(data) > MAX_UPLOAD_TOTAL:
            raise cfgstore.Invalid(f"the uploads folder is full ({MAX_UPLOAD_TOTAL // MB} MB): delete some old images")
        with open(path, "wb") as f:
            f.write(data)
    return {"url": "/uploads/" + name}


# ---------- HTTP ----------

if DEMO:
    import demo                            # fake data for screenshots / trying it out

# GET endpoints: path -> (cache seconds, function). The page polls these; caching means extra browsers
# (or the wall display) don't multiply the work. FEATURE ones answer 404 when that integration isn't set up.
API = {
    "/api/config": (0, page_config),
    "/api/services": (25, services),
    "/api/qbit": (4, qbit),
    "/api/requests": (10, requests_pending),
    "/api/plex": (5, plex),
    "/api/system": (2, system),
    "/api/live": (0.9, live),          # the live window polls every second
    "/api/security/detail": (0, security_detail),   # the Security & backups detail views (cached by file change)
}
FEATURE = {"/api/qbit": lambda: QBIT, "/api/requests": lambda: SEERR, "/api/plex": lambda: TAUTULLI}
PAGES = {"/": "index.html", "/settings": "settings.html"}
SCRIPTS = {f[:-3] for f in os.listdir(os.path.join(HERE, "js")) if f.endswith(".js")}   # app/js/*.js, read at start
apply_settings()


COMPRESSIBLE = {"application/json", "text/html", "text/css", "text/javascript"}


def accepts_gzip(header):
    """Does an Accept-Encoding header allow gzip ("gzip;q=0" means no; an explicit gzip entry beats "*")?"""
    q = {}
    for part in (header or "").lower().split(","):
        name, _, params = part.strip().partition(";")
        q[name.strip()] = not re.fullmatch(r"\s*q\s*=\s*0(\.0*)?\s*", params)
    return q.get("gzip", q.get("*", False))


class Handler(BaseHTTPRequestHandler):
    server_version = UA
    sys_version = ""                      # don't advertise the Python version
    timeout = 60                          # drop idle/stuck connections so threads can't pile up

    def log_message(self, fmt, *args):
        path, cmd = getattr(self, "path", None) or "", getattr(self, "command", None) or ""   # unset on a malformed request
        if not path.startswith("/api/") or cmd == "POST":
            print(log_safe(f"{self.client()} {fmt % args}", 500), flush=True)

    def client(self):
        """Who sent this request, for the log. The proxy's X-Real-IP (and USER_HEADER, if set) count only when the
        request really came from the proxy; anyone else could send those headers themselves."""
        ip, headers = self.client_address[0], getattr(self, "headers", None)
        via_proxy = headers is not None and self.via_proxy()
        who = (self.real_ip() if via_proxy else None) or ip   # (checked: never the header's raw text)
        user = headers.get(USER_HEADER) if via_proxy and USER_HEADER else None
        return log_safe(f"{user} ({who})" if user else who, 120)

    def send(self, code, body, ctype="application/json", cache="no-store", extra=None, cookies=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        # gzip text answers (the proxy only compresses HTML): /api/system shrinks ~4x. Not the account's answers (the
        # authenticator secret, recovery codes): compressing a secret next to request-controlled text can leak it (BREACH).
        gz = (len(body) > 1024 and ctype.split(";")[0] in COMPRESSIBLE and accepts_gzip(self.headers.get("Accept-Encoding"))
              and not (self.path or "").startswith("/api/account"))
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        if ctype.split(";")[0] in COMPRESSIBLE:
            self.send_header("Vary", "Accept-Encoding")
        if code != 304:                   # a 304's Content-Length would have to be the full body's
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        elif "Content-Security-Policy" not in (extra or {}):   # never framed, never a document with powers
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """http.server's own refusals (bad request line, headers too long, unknown method): as JSON, with the same
        headers as every other answer (CSP, nosniff, no framing), and the connection closed."""
        self.close_connection = True
        if not hasattr(self, "headers"):
            self.headers = http.client.HTTPMessage()   # (the request never got that far)
        if self.request_version == "HTTP/0.9":         # (an answer without a status line or headers helps nobody)
            self.request_version = "HTTP/1.0"
        try:
            self.send(code, {"error": str(message or code)[:200]})
        except OSError:
            pass

    def send_revalidated(self, body, ctype):
        """A page, CSS or JS file with an ETag: the browser revalidates every time (no-cache), and gets an empty
        304 when it already has this version. Weak tag: the same content may go out gzipped or not."""
        tag = 'W/"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        # Behind the sign-in, pages are no-store: after signing out, Back mustn't show them (or recovery codes) again
        # from the browser's back/forward cache. CSS and scripts are the same for everyone: revalidated as before.
        cache = ("no-store" if ctype.startswith("text/html") else "private, no-cache") if AUTH == "login" else "no-cache"
        if tag in (self.headers.get("If-None-Match") or "").replace(" ", "").split(","):
            return self.send(304, b"", ctype, cache, {"ETag": tag})
        self.send(200, body, ctype, cache, {"ETag": tag})

    def safe(self, fn):
        try:
            self.send(200, fn())
        except Exception as e:
            self.send(502, {"error": scrub(f"{type(e).__name__}: {e}")})

    def allowed(self):
        ip = self.client_address[0]
        if allowed_ip(ip, self.connection.getsockname()[0]):
            return True
        print(log_safe(f"blocked direct request from {ip} {self.command} {self.path[:80]}"), flush=True)
        self.send(403, {"error": "forbidden"})
        return False

    def origin_problem(self):
        """Why this POST is refused ("" = fine). CSRF guard for every POST: the browser's Origin must be the dashboard itself (ORIGIN / site.origin if set,
        otherwise the Host the request was sent to); the browser's own Sec-Fetch-Site, when it sends one, must say
        same-origin (a sibling subdomain is "same-site", not enough); and the body must be JSON (or an upload's raw
        bytes), which no HTML form on another site can send."""
        if DEMO:
            return ""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        want = "application/octet-stream" if urllib.parse.urlsplit(self.path).path == "/api/upload" else "application/json"
        if ctype != want:
            return f"it wasn't sent as {want}"
        if (self.headers.get("Sec-Fetch-Site") or "same-origin").strip().lower() != "same-origin":
            return "your browser says it came from another site"
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if ALLOWED_ORIGINS:
            return "" if origin in ALLOWED_ORIGINS else "it didn't come from the address set in ORIGIN"
        host = self.headers.get("Host", "")
        try:
            netloc = urllib.parse.urlsplit(origin).netloc
        except ValueError:   # "http://[" and the like
            netloc = None
        if not host or netloc != host:
            return "it didn't come from this dashboard's own page"
        if AUTH == "login" and not self.https() and not local_name(host):
            return "over plain HTTP, only a LAN address (an IP, a name without dots, .lan, .local) is trusted without ORIGIN"
        return ""

    def body(self, limit):
        cl = (self.headers.get("Content-Length") or "0").strip()
        if not re.fullmatch(r"[0-9]{1,9}", cl):
            raise cfgstore.Invalid("bad Content-Length")
        n = int(cl)
        if n > limit:
            raise cfgstore.Invalid(f"too large (max {limit // 1024} KB)")
        data = self.rfile.read(n)
        self.read_done()
        return data

    def json_body(self, limit):
        """Parse a JSON object safely: deeply nested input ([[[[...) would overflow the parser's C stack and kill
        the whole process, so nesting is checked with a cheap scan first."""
        try:
            raw = self.body(limit).decode("utf-8")   # (json.loads would also take UTF-16, past the scan below)
        except UnicodeDecodeError:
            raise cfgstore.Invalid("not UTF-8")
        depth = deepest = 0
        in_str = esc_next = False
        for ch in raw:
            if in_str:
                if esc_next:
                    esc_next = False
                elif ch == "\\":
                    esc_next = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
                deepest = max(deepest, depth)
                if deepest > 32:
                    raise cfgstore.Invalid("JSON nested too deeply")
            elif ch in "]}":
                depth -= 1
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise cfgstore.Invalid("not valid JSON")
        if not isinstance(d, dict):
            raise cfgstore.Invalid("expected a JSON object")
        return d

    # ---------- Fishbowl's own sign-in (AUTH=login) ----------

    def via_proxy(self):
        return bool(TRUSTED_PROXY) and self.client_address[0] in proxy_ips()

    def one_header(self, name):
        """A header the proxy sets, or None when it's missing or repeated (a proxy that appends instead of replacing
        would let the client pick the first copy)."""
        vals = self.headers.get_all(name) or []
        return vals[0].strip() if len(vals) == 1 else None

    def real_ip(self):
        """The browser's own address when it's known: the proxy's X-Real-IP for requests through the proxy (anyone else
        could send that header themselves; X-Forwarded-For's first entry is the client's to choose), otherwise the
        connection's address. None when the proxy doesn't say (Caddy doesn't send X-Real-IP by default)."""
        if not self.via_proxy() and any(self.headers.get(h) for h in FORWARD_HEADERS):
            return None   # a proxy we weren't told about: the connection's address is the proxy's, not the browser's
        if self.via_proxy():
            try:
                a = ipaddress.ip_address(self.one_header("X-Real-IP") or "")
            except ValueError:
                return None
            if a.version == 6 and a.scope_id:   # "fe80::1%<any text>": not an address the owner should read
                return None
            return str(getattr(a, "ipv4_mapped", None) or a)
        return self.client_address[0]

    def client_ip(self):
        """The address sign-in limits count against (the proxy's own when it doesn't say who's behind it: one shared count)."""
        return self.real_ip() or self.client_address[0]

    def https(self):
        """Does the browser reach us over HTTPS? Through the proxy: its X-Forwarded-Proto says. Otherwise (or when the
        proxy doesn't say) ORIGIN decides: every change must come from that address (origin_problem), and a browser only
        sends an https Origin from a page it loaded over HTTPS. Decides the cookies' Secure flag and __Host- name."""
        if self.via_proxy():
            xfp = (self.one_header("X-Forwarded-Proto") or "").lower()
            if xfp in ("http", "https"):
                return xfp == "https"
        return bool(ALLOWED_ORIGINS) and all(o.startswith("https://") for o in ALLOWED_ORIGINS)

    def cookie_values(self, base):
        """Every value sent for one of our cookies that has our tokens' shape, read by hand (http.cookies drops every
        cookie when one is odd). All of them, in any position: a site that can set cookies for a parent domain could
        otherwise push ours out by sending junk ones first. Over HTTPS only the __Host- name counts, which no other
        site or subdomain can set."""
        name = ("__Host-" + base) if self.https() else base
        vals, seen = [], set()
        for part in "; ".join(self.headers.get_all("Cookie") or []).split(";"):   # (a header line is at most 64 KB)
            k, sep, v = part.strip(" \t").partition("=")
            v = v.strip(" \t")
            if sep and k == name and v not in seen and auth.TOKEN.fullmatch(v):
                seen.add(v)
                vals.append(v)
                if len(vals) >= 300:   # a browser keeps at most ~180 cookies per site: more can only come from a script
                    break
        return vals

    def make_cookie(self, base, value, max_age=None, clear=False):
        """HttpOnly (scripts can't read it), SameSite=Strict (never sent on a request another site starts; a link from
        elsewhere bounces through /login, which sees the session and comes straight back), and over HTTPS Secure with
        the __Host- name (no other site or subdomain can set or read it)."""
        secure = self.https()
        parts = [f"{'__Host-' + base if secure else base}={'' if clear else value}", "Path=/", "HttpOnly", "SameSite=Strict",
                 "Priority=High"]   # (Chrome evicts lower-priority cookies first when a sibling site floods the jar)
        if secure:
            parts.append("Secure")
        if clear:
            parts.append("Max-Age=0")
        elif max_age:
            parts.append(f"Max-Age={int(max_age)}")
        return "; ".join(parts)

    def proxy_gaps(self):
        """What keeps the dashboard from seeing who's connecting, and how: headers the trusted proxy leaves out, a proxy
        it wasn't told about ("untrusted"), or every request coming from the container's gateway ("gateway")."""
        gaps = []
        if self.via_proxy():
            gaps += ["X-Real-IP"] if not self.real_ip() else []
            gaps += ["X-Forwarded-Proto"] if (self.one_header("X-Forwarded-Proto") or "").lower() not in ("http", "https") else []
        elif AUTH == "login" and any(self.headers.get(h) for h in FORWARD_HEADERS):
            gaps.append("untrusted")
        elif AUTH == "login" and self.client_address[0] == default_gateway():
            gaps.append("gateway")
        return gaps

    def view(self):
        """The settings page's data, with what only this request can tell (the address it was opened at, HTTPS, and
        whether the proxy says who's connecting)."""
        return settings_view(self.headers.get("Host"), self.https(), self.proxy_gaps())

    def account(self):
        """The account, or None when there's none or its file is damaged (then nobody is signed in)."""
        try:
            return ACCOUNT.get()
        except auth.Broken:
            return None

    def session_token(self):
        """This request's session token if it's valid (looked up once per request). Sessions from before the last
        password change or two-step change (an older epoch) don't count."""
        if self._token is None:
            d = self.account()
            tls = self.https()
            # a plain-HTTP session counts only on your own network, and only over plain HTTP (it was exposed there)
            ok = d and (tls or self.plain_http_ok())
            self._token = next((t for t in self.cookie_values("fishbowl") if SESSIONS.check(t, d["epoch"], self.client_ip(), tls)), "") if ok else ""
        return self._token or None

    def device(self, acct):
        """This browser's trusted-device token hash (it signed in here before), or None."""
        tls, ip = self.https(), self.real_ip()
        return next((h for h in (auth.device_of(acct, t, tls, ip) for t in self.cookie_values("fishbowl-device")) if h), None) if acct else None

    def gate(self, p, post=False, q=""):
        """AUTH=login: everything except the sign-in page (and the open-source CSS/JS/fonts it uses) needs a session.
        Pages send you to sign in (and back to the same page afterwards); the API answers 401. Public paths are
        matched exactly."""
        if AUTH != "login" or p in (PUBLIC_POST if post else PUBLIC_GET):
            return True
        if not post and (p.startswith("/static/") or (re.fullmatch(r"/js/[a-z-]+\.js", p) and p[4:-3] in SCRIPTS)):
            return True
        if self.session_token():
            return True
        if not post and p in PAGES:
            nxt = safe_next(p + ("?" + q if q else ""))
            self.send(303, b"", "text/plain", extra={"Location": "/login?next=" + urllib.parse.quote(nxt, safe="/")})
        else:
            self.send(401, {"error": "Sign in first.", "signin": True})
        return False

    def login_page(self, query):
        nxt = safe_next((query.get("next") or ["/"])[0])   # only our own pages: never an open redirect
        if AUTH != "login" or self.session_token():
            return self.send(303, b"", "text/plain", extra={"Location": nxt})
        page = open(os.path.join(HERE, "login.html"), "rb").read().replace(
            b'<html lang="en">', f'<html lang="en" data-theme="{SITE["theme"]}" data-site-motion="{SITE["motion"]}">'.encode(), 1)
        self.send_revalidated(page, "text/html; charset=utf-8")

    def proxy_says_https(self):
        return self.via_proxy() and (self.one_header("X-Forwarded-Proto") or "").lower() == "https"

    def plain_http_ok(self):
        """May a password come over this connection? Only on positive evidence: the trusted proxy says the browser used
        HTTPS, or the browser's address is known and on your own network (LAN, loopback, link-local). ORIGIN alone
        isn't enough: a browser on http:// would send the password before the Origin check refuses it."""
        if self.proxy_says_https():
            return True
        try:
            a = ipaddress.ip_address(self.real_ip() or "")
        except ValueError:
            return False
        a = getattr(a, "ipv4_mapped", None) or a
        return any(a in n for n in OWN_NETWORKS)

    def auth_state(self):
        """What the sign-in page needs to know (nothing about the account itself)."""
        if AUTH != "login":
            return {"mode": AUTH}
        try:
            broken, setup = False, ACCOUNT.get() is None
        except auth.Broken:
            broken, setup = True, False
        return {"mode": "login", "setup": setup, "broken": broken, "signed_in": bool(self.session_token()),
                "code_due": bool(self.pending()), "https": self.https(), "http_ok": self.plain_http_ok(),
                "min_length": auth.PW_MIN, "proxy_asked": _auth_env == "proxy", "gap_hint": gap_hint(self.proxy_gaps())}

    def pending(self, consume=False):
        """The half-finished sign-in for this browser (the password was right; the authenticator code is due)."""
        now = time.time()
        with _pending_lock:
            for k in [k for k, v in _pending.items() if now - v["t"] > PENDING_FOR]:
                del _pending[k]
            for tok in self.cookie_values("fishbowl-pre"):
                key = hashlib.sha256(tok.encode()).hexdigest()
                if key in _pending:
                    return _pending.pop(key) if consume else _pending[key]
        return None

    def begin_try(self, ip, dev=None, account=True, session=None, waits=True):
        """Count a try BEFORE checking it, under one lock: parallel tries can't all slip in under a limit, and a crash
        mid-check still counted it. 0 = go ahead, -1 = locked, -2 = this session used up its tries; otherwise the
        seconds to wait. A trusted device (signed in here before) counts on its own, so someone hammering the account
        can't lock its owner out; so does a signed-in browser re-confirming the password (session), which then gets
        10 tries before it's signed out. waits=False (the code step): counted the same, toward the lock too, but
        never made to wait, or someone else's guessing could make every wait outlast the 5-minute step (that step
        has its own cap: 5 codes)."""
        now = time.time()
        with _try_lock:
            if session:
                fails, last = _session_tries.get(session, (0, 0))
                if fails >= SESSION_FAILS:
                    return -2
                wait = THROTTLE.wait_for(fails, last, now)
                if not wait:
                    if len(_session_tries) > 1000:
                        _session_tries.clear()
                    _session_tries[session] = (fails + 1, now)
                return wait
            d = ACCOUNT.get() if account else None
            if d and dev and dev in d.get("devices", {}) and d["devices"][dev].get("fails", 0) < auth.DEVICE_FAILS:   # (worn out: just a browser)
                v = d["devices"][dev]
                wait = THROTTLE.wait_for(v.get("fails", 0), v.get("last", 0), now) if waits else 0
                if not wait:
                    ACCOUNT.update(lambda x: x["devices"][dev].update(fails=x["devices"][dev].get("fails", 0) + 1, last=int(now))
                                   if dev in x.get("devices", {}) else None)
                return wait
            wait = THROTTLE.ip_wait(ip, now) if waits else 0
            if d:
                if d.get("failures", 0) >= auth.LOCK_AT:
                    return -1
                if waits:
                    wait = max(wait, THROTTLE.wait_for(d.get("failures", 0), d.get("last_fail", 0), now))
            if wait:
                return wait
            THROTTLE.ip_failed(ip, now)
            if d:
                ACCOUNT.update(lambda x: x.update(failures=x.get("failures", 0) + 1, last_fail=int(now)))
            return 0

    def try_ok(self, ip, dev=None, session=None):
        """A right answer: this address's count starts again, and the browser's own: the session's, a trusted
        device's, or (from any other browser) the account's. A trusted device never clears the account's count, so
        someone else's guessing keeps counting toward the lock while the owner signs in."""
        THROTTLE.ip_ok(ip)
        if session:
            _session_tries.pop(session, None)
            return
        ACCOUNT.update(lambda x: x["devices"][dev].update(fails=0) if dev and dev in x.get("devices", {})
                       else x.update(failures=0, last_fail=0))   # (a device forgotten mid-try was counted on the account)

    def refuse_wait(self, wait):
        if wait == -2:
            SESSIONS.end(self._token or "")
            ip = self.client_ip()
            try:
                ACCOUNT.update(lambda x: ACCOUNT.fail(x, f"signed out after {SESSION_FAILS} wrong tries in Settings", ip, "settings"))
            except auth.Broken:
                pass
            print(log_safe(f"sign-in: a browser at {ip} signed out after {SESSION_FAILS} wrong tries in Settings"), flush=True)
            return self.send(401, {"error": f"{SESSION_FAILS} wrong tries in this browser: it's signed out. Sign in again.",
                                   "signin": True}, cookies=(self.make_cookie("fishbowl", "", clear=True),))
        if wait < 0:
            return self.send(423, {"error": f"Sign-ins from new browsers are locked after {auth.LOCK_AT} wrong tries in a row. "
                                            "A browser that signed in here before can still sign in; or unlock it on the host: "
                                            "docker exec -it <container> python /app/auth.py unlock"})
        return self.send(429, {"error": f"Too many tries. Wait {fmt_wait(wait)}, then try again.", "retry": wait},
                         extra={"Retry-After": str(wait)})

    def note_failure(self, ip, what, count=None, dev=None):
        """Log a failure; count = "password" or "code" counts it toward the next sign-in's notice. One made in a trusted
        browser (dev) is remembered for that browser: if the same browser then signs in, it was a typo. (Never by
        address: an attacker can share one with the owner, or every visitor can look alike.)"""
        try:
            ACCOUNT.update(lambda d: ACCOUNT.fail(d, what, ip, count))
        except auth.Broken:
            pass
        if count and dev:
            with _pending_lock:
                if len(_recent_fails) > 1000:
                    _recent_fails.clear()
                _recent_fails.setdefault(dev, []).append((time.time(), count))
        print(log_safe(f"sign-in: {what} from {ip}"), flush=True)

    @staticmethod
    def own_typos(dev, wrong_codes=0):
        """What the sign-in that follows forgives: this trusted browser's own wrong passwords and codes of the last 10
        minutes, and the wrong codes of this very sign-in (its half-finished step). Every other record is dropped:
        after a sign-in, older ones can only be stale."""
        now = time.time()
        with _pending_lock:
            mine = [k for t, k in _recent_fails.get(dev, []) if now - t < 600] if dev else []
            _recent_fails.clear()
        return {"password": mine.count("password"), "code": max(mine.count("code"), wrong_codes)}

    def revoke_everything(self, ip, why, change, epoch):
        """One way out for every change that must end other access (password changed, two-step turned on or off): the
        change and a new epoch are saved together, so every session, half-finished sign-in and trusted device from
        before is worthless from that moment, even one being created right now. Applied only if the account is still
        at the epoch the change was approved under (and this browser's session still counts): a change approved just
        before another one can't land after it. This browser gets a new session and device (returned as cookies);
        None when it was too late."""
        ua = self.headers.get("User-Agent", "")
        cur = SESSIONS.check(self._token, epoch) if self._token else None
        if not cur:
            return None
        new = {}

        def apply(x):   # (under the account's file lock, which every sign-out also takes)
            if x["epoch"] != epoch or not SESSIONS.on_disk(self._token, epoch):
                return
            change(x)
            x["epoch"] = auth.new_epoch(epoch)
            x["devices"] = {}
            new.update(dev=auth.new_device(x, self.https(), ip), epoch=x["epoch"])
            ACCOUNT.event(x, why, ip, ua)
        ACCOUNT.update(apply)
        if not new:
            return None
        SESSIONS.end_all()
        with _pending_lock:
            _pending.clear()
            _pending_totp.clear()
        token, max_age = SESSIONS.create(bool(cur.get("keep")), ip, ua, new["epoch"], self.https())
        self._token = token
        print(log_safe(f"sign-in: {why} from {ip}: every other browser signed out"), flush=True)
        return (self.make_cookie("fishbowl", token, max_age), self.make_cookie("fishbowl-device", new["dev"], auth.DEVICE_FOR))

    def signed_in_now(self, ip, keep, what, dev, epoch, extra_cookies=(), wrong_codes=0):
        """Start a session (a new token: never one the browser brought) for the account as it was when the password was
        checked (epoch), and remember this browser as a trusted device. Failures since the last sign-in are shown once."""
        ua = self.headers.get("User-Agent", "")
        token, max_age = SESSIONS.create(keep, ip, ua, epoch, self.https())
        mine = self.device(ACCOUNT.get())   # (forgive only this browser's own typos: a step planted from elsewhere isn't)
        out, typos = {}, self.own_typos(dev if dev and dev == mine else None, wrong_codes if dev and dev == mine else 0)

        def apply(x):
            if x["epoch"] != epoch:   # the password or two-step changed meanwhile: that session is already worthless
                return
            if not dev:
                out["dev"] = auth.new_device(x, self.https(), ip)
            ACCOUNT.event(x, what, ip, ua)
            since = {k: v - typos.get(k, 0) for k, v in x.get("since", {}).items()
                     if k in ("password", "code", "settings") and v > typos.get(k, 0)}
            x["since"] = {}
            if since.get("code"):   # the lasting alarm adds up until the password changes (or "That was me")
                x["notice"] = {"code": x.get("notice", {}).get("code", 0) + since["code"], "t": int(time.time())}
            out.update(ok=True, since=since, recovery_left=len(x.get("recovery", [])))
        ACCOUNT.update(apply)
        if not out.get("ok"):
            SESSIONS.end(token)
            return self.send(401, {"error": "The password or two-step sign-in just changed: sign in again.", "restart": True},
                             cookies=(self.make_cookie("fishbowl-pre", "", clear=True),))
        self.try_ok(ip, dev)
        cookies = [self.make_cookie("fishbowl", token, max_age), *extra_cookies]
        if out.get("dev"):
            cookies.append(self.make_cookie("fishbowl-device", out["dev"], auth.DEVICE_FOR))
        print(log_safe(f"sign-in: {what} from {ip}"), flush=True)
        notice = dict(out["since"])
        if "recovery code" in what:
            notice["recovery_left"] = out["recovery_left"]
        self.send(200, {"ok": True, **({"notice": notice} if notice else {})}, cookies=cookies)

    def account_view(self):
        d = ACCOUNT.get()
        return {"user": d["user"], "totp": bool(d["totp"].get("on")), "recovery_left": len(d.get("recovery", [])),
                "sessions": SESSIONS.listing(self.session_token(), d["epoch"]), "events": d.get("events", [])[:20],
                "fails": d.get("fails", [])[:20], "notice": d.get("notice") or {}, "failures": d.get("failures", 0),
                "locked": d.get("failures", 0) >= auth.LOCK_AT, "min_length": auth.PW_MIN}

    def code_ok(self, code, epoch):
        """"ok" for a right authenticator code or recovery code (each works once), "used" for a right code that was
        already used, "wrong", or "stale" when the account changed since the password was checked. Checked and used up under one lock, on a fresh read: two
        requests with the same code can't both get through."""
        code = code if isinstance(code, str) else ""
        with _try_lock:
            d = ACCOUNT.get()
            if not d or d["epoch"] != epoch:
                return "stale"
            if not d["totp"].get("on"):
                return "wrong"
            if auth.looks_like_recovery(code):
                h = auth.rc_hash(code)
                if not any(hmac.compare_digest(h, x) for x in d.get("recovery", [])):
                    return "wrong"
                used = []   # still there inside the update (new codes made meanwhile end the old ones)
                ACCOUNT.update(lambda x: (used.append(1), x.update(recovery=[r for r in x["recovery"] if r != h]))
                               if h in x["recovery"] and x["epoch"] == epoch else None)
                return "ok" if used else "wrong"
            step = auth.totp_match(d["totp"]["secret"], code, d["totp"].get("last_step", 0))
            if step is None:
                return "used" if auth.totp_used(d["totp"]["secret"], code, d["totp"].get("last_step", 0)) else "wrong"
            ACCOUNT.update(lambda x: x["totp"].update(last_step=max(step, x["totp"].get("last_step", 0))))
            return "ok"

    def auth_post(self, path):
        try:
            return self._auth_post(path)
        except auth.Broken as e:   # e.g. the account was reset on the host in the middle of this request
            return self.send(503, {"error": f"Signing in is off: {e}. Reload the page.", "restart": True})

    def _auth_post(self, path):
        d_in = self.json_body(8192)
        ip, ua = self.client_ip(), self.headers.get("User-Agent", "")

        def s(k):   # a text field, or "" (wrong types, too long, unpaired surrogates)
            v = d_in.get(k)
            return v if auth.is_text(v, 2048) else ""
        keep = d_in.get("keep") is True

        if path == "/api/logout":
            tok = self.session_token()
            if tok:
                SESSIONS.end(tok)
                print(log_safe(f"sign-in: signed out from {ip}"), flush=True)
            self.pending(consume=True)   # a half-finished sign-in in this browser ends too ("Start over")
            # (not Clear-Site-Data "cookies": that wipes every cookie of the whole site, your SSO's included)
            return self.send(200, {"ok": True}, extra={"Clear-Site-Data": '"cache"'},
                             cookies=(self.make_cookie("fishbowl", "", clear=True), self.make_cookie("fishbowl-pre", "", clear=True)))
        try:
            acct = ACCOUNT.get()
        except auth.Broken as e:
            return self.send(503, {"error": f"Signing in is off: {e}. On the host: docker exec -it <container> "
                                            "python /app/auth.py reset (settings and keys stay)."})
        if path in ("/api/login", "/api/login/code", "/api/setup-account") and not self.plain_http_ok():
            return self.send(403, {"error": gap_hint(self.proxy_gaps()) or "Use the dashboard's https address to sign in: "
                                            "over plain HTTP, passwords are only accepted from your own network."})

        if path == "/api/setup-account":
            if acct is not None:
                return self.send(409, {"error": "The account already exists. Sign in instead.", "restart": True})
            if not s("code").strip():
                return self.send(400, {"error": "Type the setup code.", "field": "code"})
            wait = self.begin_try(ip, account=False)
            if wait:
                return self.refuse_wait(wait)
            wrong_code = {"error": "That's not the setup code. Use the newest one in the container's log: "
                                   "docker logs <container> 2>&1 | grep '^SETUP CODE' | tail -n 1 (or the one auth.py "
                                   "reset or setup-code printed).", "field": "code"}
            if not auth.setup_code_matches(s("code")):
                print(log_safe(f"sign-in: wrong setup code from {ip}"), flush=True)
                return self.send(403, wrong_code)
            THROTTLE.ip_ok(ip)   # the right code: retrying a too-weak password doesn't count as guessing
            bad_name = auth.name_problem(s("user"))
            problem = bad_name or auth.password_problem(s("password"), s("user").strip())
            if problem:
                return self.send(400, {"error": problem, "field": "user" if bad_name else "password"})
            d = {"v": 1, "user": s("user").strip(), "hash": auth.hash_password(s("password")), "totp": {}, "recovery": [],
                 "failures": 0, "last_fail": 0, "devices": {}, "events": [], "fails": [], "epoch": auth.new_epoch(),
                 "created": int(time.time())}   # (hashed before taking the locks: nothing else waits for scrypt)
            ACCOUNT.event(d, "account created", ip, ua)
            with ACCOUNT.lock, auth.file_lock():   # one account, even when raced (and never over a reset on the host)
                if ACCOUNT.exists():
                    return self.send(409, {"error": "The account already exists. Sign in instead.", "restart": True})
                if not auth.setup_code_matches(s("code")):   # used (or replaced on the host) meanwhile
                    return self.send(403, wrong_code)
                ACCOUNT.save(d)
                auth.clear_setup_code()
            print(log_safe(f"sign-in: account created from {ip}"), flush=True)
            return self.signed_in_now(ip, keep, "signed in (new account)", None, d["epoch"])

        if acct is None:
            return self.send(409, {"error": "There's no account yet: create it first.", "setup": True, "restart": True})
        dev = self.device(acct)

        if path == "/api/login":
            if not s("user").strip() or not s("password"):
                return self.send(400, {"error": "Type your name and password.", "field": "user" if not s("user").strip() else "password"})
            wait = self.begin_try(ip, dev)
            if wait:
                return self.refuse_wait(wait)
            acct = ACCOUNT.get()   # fresh: the account as it is now that this try is counted
            if acct is None:
                return self.send(409, {"error": "There's no account yet: create it first.", "setup": True, "restart": True})
            name_ok = hmac.compare_digest(auth.norm(s("user").strip()).casefold().encode(), auth.norm(acct["user"]).casefold().encode())
            pw_ok, rehash = auth.verify_password(s("password"), acct["hash"])   # always runs: a wrong name takes as long
            if not (name_ok and pw_ok):
                self.note_failure(ip, "wrong name or password", "password", dev)
                return self.send(401, {"error": "Wrong name or password.", "field": "password"})
            if rehash:   # stronger settings since it was saved; only if the password didn't change meanwhile
                new_hash = auth.hash_password(s("password"))
                ACCOUNT.update(lambda x: x.update(hash=new_hash) if x["hash"] == acct["hash"] else None)
            if acct["totp"].get("on"):
                pre = secrets.token_urlsafe(32)
                with _pending_lock:
                    if len(_pending) > 200:
                        _pending.clear()
                    _pending[hashlib.sha256(pre.encode()).hexdigest()] = {"t": time.time(), "keep": keep, "tries": 0, "dev": dev,
                                                                          "epoch": acct["epoch"]}
                return self.send(200, {"code_due": True}, cookies=(self.make_cookie("fishbowl-pre", pre, PENDING_FOR),))
            return self.signed_in_now(ip, keep, "signed in", dev, acct["epoch"])

        if path == "/api/login/code":
            p = self.pending()
            if not p:
                return self.send(401, {"error": "That step expired (it lasts 5 minutes, and ends when the dashboard restarts): "
                                                "sign in again.", "restart": True})
            if not s("code").strip():
                return self.send(400, {"error": "Type a code from your authenticator app, or a recovery code.", "field": "code"})
            with _pending_lock:   # the step's own 5 codes, reserved before checking: parallel tries can't all slip in
                if p["tries"] >= 5:
                    self.pending(consume=True)
                    return self.send(401, {"error": "Wrong code 5 times: sign in with your password again.", "restart": True,
                                           "field": "code"})
                p["tries"] += 1
            wait = self.begin_try(ip, p.get("dev"), waits=False)
            if wait:
                with _pending_lock:
                    p["tries"] -= 1
                return self.refuse_wait(wait)
            result = self.code_ok(s("code"), p["epoch"])
            if result == "used":   # (refused, but no guess: no strike, nothing for the alarm)
                with _pending_lock:
                    p["tries"] -= 1
                return self.send(401, {"error": CODE_USED, "field": "code"})
            if result != "ok":
                with _pending_lock:
                    over = result == "stale" or p["tries"] >= 5
                    if over:
                        self.pending(consume=True)
                if result == "stale":
                    return self.send(401, {"error": "The password or two-step sign-in just changed: sign in again.", "restart": True})
                self.note_failure(ip, "right password, wrong code", "code", p.get("dev"))
                return self.send(401, {"error": "Wrong code 5 times: sign in with your password again." if over else
                                                "Wrong code. Use the newest 6-digit code from your app, or a recovery code.",
                                       "restart": over, "field": "code"})
            p = self.pending(consume=True) or p
            used = "recovery code" if auth.looks_like_recovery(s("code")) else "authenticator code"
            return self.signed_in_now(ip, p["keep"], f"signed in (password + {used})", p.get("dev"), p["epoch"],
                                      (self.make_cookie("fishbowl-pre", "", clear=True),), wrong_codes=p["tries"])

        # ---- everything below changes the account: a session is needed (the gate checked it) ----
        tok = self.session_token()
        if not tok:
            return self.send(401, {"error": "Sign in first.", "signin": True})
        tok_key = hashlib.sha256(tok.encode()).hexdigest()

        if path == "/api/account/totp/confirm":
            if acct["totp"].get("on"):
                return self.send(409, {"error": "The authenticator app is already on."})
            if not s("code").strip():
                return self.send(400, {"error": "Type the 6-digit code your app shows.", "field": "code"})
            wait = self.begin_try(ip, session=tok_key)
            if wait:
                return self.refuse_wait(wait)
            with _try_lock:   # one confirm at a time: a double click can't show recovery codes that were then replaced
                with _pending_lock:
                    pend = _pending_totp.pop(tok_key, None)
                if not pend or time.time() - pend["t"] > 900:
                    return self.send(409, {"error": "Start again: that setup took more than 15 minutes, or the server restarted.",
                                           "restart": True})
                step = auth.totp_match(pend["secret"], s("code"), 0)
                if step is None:
                    with _pending_lock:
                        _pending_totp[tok_key] = pend   # another try with the same QR code
            if step is None:
                self.note_failure(ip, "wrong code while setting up the authenticator")
                return self.send(400, {"error": "That code doesn't match. Check the app's clock, and type the newest code.", "field": "code"})
            self.try_ok(ip, session=tok_key)
            codes, hashes = auth.new_recovery_codes()
            cookies = self.revoke_everything(ip, "authenticator app turned on",
                                             lambda x: x.update(totp={"secret": pend["secret"], "on": True, "last_step": step}, recovery=hashes),
                                             pend["epoch"])
            if not cookies:
                return self.send(409, TOO_LATE)
            return self.send(200, {"ok": True, "recovery": codes}, cookies=cookies)

        if path not in ("/api/account/password", "/api/account/totp/start", "/api/account/totp/off", "/api/account/recovery",
                        "/api/account/sessions/end", "/api/account/notice/dismiss"):
            return self.send(404, {"error": "not found"})
        needs_code = acct["totp"].get("on") and path != "/api/account/totp/start"   # (a stolen session + password isn't enough)
        # cheap checks first: nothing is counted (or a code used up) for a request that can't succeed anyway
        if path == "/api/account/totp/start" and acct["totp"].get("on"):
            return self.send(409, {"error": "The authenticator app is already on."})
        if path in ("/api/account/totp/off", "/api/account/recovery") and not acct["totp"].get("on"):
            return self.send(409, {"error": "The authenticator app is already off." if path.endswith("off")
                                            else "Recovery codes come with the authenticator app: turn it on first."})
        if path == "/api/account/password":
            problem = auth.password_problem(s("new"), acct["user"])
            if problem:
                return self.send(400, {"error": problem, "field": "new"})
            if hmac.compare_digest(auth.norm(s("new")).encode(), auth.norm(s("password")).encode()):
                return self.send(400, {"error": "That's the same password.", "field": "new"})
        if not s("password"):
            return self.send(400, {"error": "Type your current password.", "field": "password"})
        if needs_code and not s("code").strip():
            return self.send(400, {"error": "Type a code from your authenticator app, or a recovery code.", "field": "code"})
        # the current password again (and the code, when the authenticator is on): counted for this session
        wait = self.begin_try(ip, session=tok_key)
        if wait:
            return self.refuse_wait(wait)
        acct = ACCOUNT.get()
        if acct is None:
            return self.send(409, {"error": "There's no account any more: reload the page.", "restart": True})
        if not auth.verify_password(s("password"), acct["hash"])[0]:
            self.note_failure(ip, "wrong password (account change)", "settings")
            return self.send(403, {"error": "That's not the current password.", "field": "password"})
        needs_code = acct["totp"].get("on") and path != "/api/account/totp/start"   # (as the account is now)
        if needs_code:
            result = self.code_ok(s("code"), acct["epoch"])
            if result == "stale":
                return self.send(409, TOO_LATE)
            if result == "used":   # (a try, as it cost a password hash; not a wrong code: nothing for the alarm)
                return self.send(403, {"error": CODE_USED, "field": "code"})
            if result != "ok":
                self.note_failure(ip, "wrong code (account change)", "settings")
                return self.send(403, {"error": "Wrong code. Use the newest 6-digit code from your app, or a recovery code.", "field": "code"})
        self.try_ok(ip, session=tok_key)

        # (the password again for these too, as ASVS asks: a session alone, say at a wall screen, can't end the
        # owner's other sessions or silence the "someone may know your password" warning)
        if path == "/api/account/sessions/end":
            if d_in.get("all") is True:   # every other browser's session and half-finished sign-in. Trusted devices
                if not SESSIONS.end_all(but_token=tok):   # stay: forgetting them is part of a password change. Half-finished
                    return self.send(409, TOO_LATE)       # sign-ins stay too: they aren't sessions, and the owner's own
                ACCOUNT.update(lambda x: ACCOUNT.event(x, "signed out everywhere else", ip, ua))
                return self.send(200, {"ok": True})
            ended = SESSIONS.end_id(s("id"), tok)
            if ended is None:
                return self.send(409, TOO_LATE)
            if not ended:
                return self.send(404, {"error": "That browser isn't signed in any more."})
            return self.send(200, {"ok": True})

        if path == "/api/account/notice/dismiss":   # "That was me": the owner says those wrong codes were theirs
            seen = d_in.get("seen") if isinstance(d_in.get("seen"), int) else None   # what the page showed: newer ones stay

            def dismiss(x):
                left = 0 if seen is None else x.get("notice", {}).get("code", 0) - max(0, seen)
                x["notice"] = {**x["notice"], "code": left} if left > 0 else {}
                ACCOUNT.event(x, "warning dismissed (wrong codes were mine)", ip, ua)
            ACCOUNT.update(dismiss)
            return self.send(200, {"ok": True})

        if path == "/api/account/password":
            h, now = auth.hash_password(s("new")), int(time.time())
            cookies = self.revoke_everything(ip, "password changed", lambda x: (
                x.update(hash=h, changed=now), x.get("notice", {}).pop("code", None), x.get("since", {}).pop("code", None)), acct["epoch"])
            return self.send(200, {"ok": True}, cookies=cookies) if cookies else self.send(409, TOO_LATE)

        if path == "/api/account/totp/start":
            secret = auth.new_totp_secret()
            with _pending_lock:
                if len(_pending_totp) > 50:
                    _pending_totp.clear()
                _pending_totp[tok_key] = {"t": time.time(), "secret": secret, "epoch": acct["epoch"]}
            uri = auth.otpauth_uri(secret, acct["user"])
            return self.send(200, {"secret": secret, "uri": uri, "qr": auth.qr_svg(uri)})

        if path == "/api/account/totp/off":
            cookies = self.revoke_everything(ip, "authenticator app turned off", lambda x: x.update(totp={}, recovery=[]), acct["epoch"])
            return self.send(200, {"ok": True}, cookies=cookies) if cookies else self.send(409, TOO_LATE)

        if path == "/api/account/recovery":
            codes, hashes, done = *auth.new_recovery_codes(), []
            ACCOUNT.update(lambda x: (x.update(recovery=hashes), ACCOUNT.event(x, "new recovery codes", ip, ua), done.append(1))
                           if x["epoch"] == acct["epoch"] and SESSIONS.on_disk(tok, acct["epoch"]) else None)
            return self.send(200, {"ok": True, "recovery": codes}) if done else self.send(409, TOO_LATE)

    def do_GET(self):
        self.guarded(self.get)

    def do_POST(self):
        self.guarded(self.post)

    def parse_request(self):
        """The request line and headers, within 64 KB in all (http.server alone allows 100 lines of 64 KB each, so a few
        stalled connections could fill the memory)."""
        real = self.rfile
        self.rfile = _Budget(real, 64 * 1024)
        try:
            return super().parse_request()
        finally:
            self.rfile = real

    def read_done(self):
        """The request is read in full: from now on only the overall deadline applies (see Server)."""
        if hasattr(self.server, "read_done"):
            self.server.read_done(self.request)

    def guarded(self, fn):
        """No raw exception (whose text can include upstream URLs) ever reaches the browser or the log. The request
        target must be a plain path ("/x", not "//x", "\\x" or "http://host/x"): the sign-in gate and the router read
        it the same way (urlsplit), so no spelling of a path can reach a page the gate didn't check."""
        try:
            self._token = None   # this request's session: never one from an earlier request on the connection
            if self.request_version == "HTTP/0.9":
                return self.send_error(400, "HTTP/0.9 isn't supported")
            if self.command == "GET":
                self.read_done()
            # through the proxy, the per-address connection cap applies to the browser's own address
            # (only when the proxy says who it is: without X-Real-IP every visitor would share one small bucket)
            if self.via_proxy() and self.real_ip() and hasattr(self.server, "proxied") and not self.server.proxied(self.request, self.real_ip()):
                print(log_safe(f"refused a connection from {self.real_ip()} (via the proxy): too many at once"), flush=True)
                return self.send(429, {"error": "The dashboard is busy for your address: try again in a few seconds.", "retry": 3,
                                       "busy": True}, extra={"Retry-After": "3"})
            path = urllib.parse.urlsplit(self.path or "").path if re.match(r"/(?![/\\])", self.path or "") else ""
            # One plain spelling per path: no "//", "\\", "%", ".", ";" tricks, control characters or spaces, which other
            # apps' auth checks and routers read differently. Real paths here never need them (queries can have %).
            if not path or re.search(r"[\\%;\x00-\x20\x7f]|//|/\.", path):
                return self.send(400, {"error": "bad request"})
            fn()
        except cfgstore.Invalid as e:
            self.send(400, {"error": str(e), **({"section": e.section} if e.section else {})})
        except (BrokenPipeError, ConnectionResetError):
            pass   # the browser left (or a stuck connection was cut): nothing to answer, nothing to log
        except Exception as e:
            print(log_safe(f"error on {self.command} {self.path[:80]}: {scrub(f'{type(e).__name__}: {e}')}"), flush=True)
            try:
                self.send(502, {"error": "upstream error (see container log)"})
            except Exception:
                pass

    def get(self):
        url = urllib.parse.urlsplit(self.path)
        p, query = url.path, urllib.parse.parse_qs(url.query)
        if not self.allowed() or not self.gate(p, q=url.query):
            return
        if p == "/login":
            return self.login_page(query)
        if p == "/api/auth/state":
            return self.send(200, self.auth_state())
        if p == "/api/account" and AUTH == "login":
            return self.send(200, self.account_view())
        if p in PAGES:
            # default theme baked into <html> so the page paints in the right colours before any script runs
            page = open(os.path.join(HERE, PAGES[p]), "rb").read().replace(
                b'<html lang="en">', f'<html lang="en" data-theme="{SITE["theme"]}">'.encode(), 1)
            self.send_revalidated(page, "text/html; charset=utf-8")
        elif p in ("/style.css", "/themes.css", "/fonts.css"):
            self.send_revalidated(open(os.path.join(HERE, p[1:]), "rb").read(), "text/css; charset=utf-8")
        elif (m := re.fullmatch(r"/js/([a-z-]+)\.js", p)) and m[1] in SCRIPTS:
            self.send_revalidated(open(os.path.join(HERE, "js", m[1] + ".js"), "rb").read(), "text/javascript; charset=utf-8")
        elif p in API:
            ttl, fn = API[p]
            if p in FEATURE and not FEATURE[p]()["on"]:
                return self.send(404, {"error": "integration not configured"})
            fn = getattr(demo, fn.__name__, fn) if DEMO else fn
            self.safe(lambda: cached(p, ttl, fn) if ttl else fn())
        elif p == "/api/settings":
            self.send(200, self.view())
        elif p == "/api/plexart" and TAUTULLI["on"] and not DEMO:
            try:
                art = plex_art(query.get("img", [""])[0])
            except Exception:
                art = None
            if not art:
                return self.send(404, {"error": "no art"})
            self.send(200, art[0], art[1], "private, max-age=86400", {"Content-Security-Policy": "default-src 'none'"})
        elif p == "/api/icon" and not DEMO:
            ico = favicon(query.get("host", [""])[0])
            if not ico:
                return self.send(404, {"error": "no icon"})
            self.send(200, ico[0], ico[1], "private, max-age=86400", {"Content-Security-Policy": "default-src 'none'"})
        elif p == "/favicon.ico":
            self.path = "/static/favicon.svg"
            return self.get()
        elif m := re.fullmatch(r"/(static|uploads)/((?:fonts/)?[\w-][\w.-]*\.(png|svg|webp|ico|jpg|gif|woff2))", p):
            folder = os.path.join(HERE, "static") if m[1] == "static" else cfgstore.UPLOAD_DIR
            f = os.path.join(folder, m[2])
            # Web uploads are raster-only (save_upload); an SVG in uploads/ was put there by the admin on disk.
            # Either way it's served with scripts blocked (CSP below), so even a hostile SVG can't run anything.
            # Fonts and the fonts/ folder exist only in static/.
            if not os.path.isfile(f) or (m[1] == "uploads" and (m[3] in ("ico", "woff2") or "/" in m[2])):
                return self.send(404, {"error": "not found"})
            ctype = {"png": "image/png", "svg": "image/svg+xml", "webp": "image/webp", "ico": "image/x-icon",
                     "jpg": "image/jpeg", "gif": "image/gif", "woff2": "font/woff2"}[m[3]]
            cache = "private, max-age=604800" if m[1] == "uploads" and AUTH == "login" else "public, max-age=604800"
            self.send(200, open(f, "rb").read(), ctype, cache,
                      {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        elif p == "/healthz":
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "not found"})

    def post(self):
        if not self.allowed():
            return
        why = self.origin_problem()
        if why:
            return self.send(403, {"error": f"Refused: {why}. Open the dashboard by its usual address; if that's what you "
                                            "did, set ORIGIN in docker-compose.yml to it."})
        path = urllib.parse.urlsplit(self.path).path
        if not self.gate(path, post=True):
            return
        if AUTH == "login" and (path in PUBLIC_POST or path.startswith("/api/account/")):
            return self.auth_post(path)
        who = self.client()
        if path == "/api/settings":
            d = self.json_body(256 * 1024)
            with _cfg_lock:
                new = cfgstore.validate(d.get("settings"), ED)
                store = cfgstore._in("integrations", cfgstore.apply_secret_changes, new, ED, cfgstore._obj(d.get("secrets"), "secrets"), STORE)
                if VIA_PROXY_ENV:         # set in the environment: it wins, so keep what's saved (as for integration URLs)
                    new["via_proxy"] = ED["via_proxy"]
                # the root collector's settings: checked now, handed over as a request only once the rest is saved
                wish = cfgstore._in("collector", check_collector_changes, d.get("collector")) if "collector" in d else None
                if DEMO:                  # public demo: check everything, store nothing (visitors can't change it for others)
                    return self.send(200, {**self.view(), "settings": new, "demo_not_saved": True})
                cfgstore._in("setup", cfgstore.save, new, store)
                apply_settings()
                warning = ""
                if wish is not None:
                    try:
                        write_collector_changes(wish)
                    except cfgstore.Invalid as e:   # the dashboard's settings are saved and in use; say what wasn't
                        warning, wish = str(e), None
            print(f"settings changed by {who}", flush=True)
            if wish is not None:
                wait_for_collector(wish)
            return self.send(200, {**self.view(), **({"collector_warning": warning} if warning else {})})
        if path == "/api/settings/test":
            name = self.json_body(1024).get("name", "")
            return self.send(200, test_integration(name))
        if path == "/api/upload":
            if DEMO:
                raise cfgstore.Invalid("uploads are turned off in the demo")
            if not cfgstore.UI_ENABLED:
                raise cfgstore.Invalid("the settings page is read-only (SETTINGS_UI=0)")
            try:
                return self.send(200, save_upload(self.body(2 * 1024 * 1024)))
            except PermissionError:
                raise cfgstore.Invalid("can't write to the data folder (make it writable by uid 1000)")
        if path == "/api/security/ack":
            try:
                result = ack_logins()
            except PermissionError:
                raise cfgstore.Invalid("can't write to the data folder")
            print(f"SSH logins marked as reviewed by {who}", flush=True)
            return self.send(200, result)
        if path == "/api/security/known":
            d = self.json_body(200)
            result = add_known_ips(d.get("ip"), everything=d.get("all") is True)
            if result["added"]:
                print(f"known IPs {', '.join(result['added'])} added by {who}", flush=True)
            return self.send(200, result)
        if path == "/api/cron/approve":
            try:
                want = self.json_body(200).get("hash", "")
            except cfgstore.Invalid:
                want = ""
            if not re.fullmatch(r"[0-9a-f]{64}", want or ""):
                return self.send(400, {"error": "bad hash"})
            if DEMO:
                return self.send(200, {"ok": True})
            with open(os.path.join(REQ_DIR, "cron-approve"), "w") as f:   # the root collector verifies it matches the live crontabs
                f.write(want)
            print(f"cron approval {want[:12]} requested by {who}", flush=True)
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/requests/(\d+)/(approve|decline)", path)
        if not m or not SEERR["on"]:
            return self.send(404, {"error": "not found"})
        rid, action = m.groups()
        if not DEMO:
            try:
                seerr(f"/request/{rid}/{action}", method="POST")
            except urllib.error.HTTPError as e:
                return self.send(502, {"error": f"Seerr said {e.code}"})
            except Exception as e:
                return self.send(502, {"error": scrub(f"Seerr unreachable: {type(e).__name__}: {e}")})
        with _lock:
            _cache.pop("/api/requests", None)
        print(f"request {rid} {action}d by {who}", flush=True)
        self.send(200, {"ok": True})


class _Budget:
    """A file wrapper that lets readline() read at most n bytes in all: http.client.LineTooLong once they're used up."""
    def __init__(self, f, n):
        self.f, self.n = f, n

    def readline(self, limit=-1):
        line = self.f.readline(self.n + 1 if limit < 0 else min(limit, self.n + 1))
        self.n -= len(line)
        if self.n < 0:
            raise http.client.LineTooLong("request headers")
        return line

    def __getattr__(self, name):
        return getattr(self.f, name)


class Server(ThreadingHTTPServer):
    """Refuses a client that isn't the proxy before a thread is started for it: otherwise any container on the
    proxy's network could hold the dash's threads (pids_limit) with idle connections. Logged once a minute per IP.
    Also caps connections (in all, and per address other than the proxy's) and gives each one a deadline, so
    connections trickling a byte at a time can't use up the threads either."""
    daemon_threads = True
    request_queue_size = 64   # the default 5 drops connections when a page opens many at once
    MAX_CONN, MAX_PER_IP, READ_DEADLINE, DEADLINE = 80, 32, 20, 120   # (the compose file allows 128 threads in all)
    _logged = {}
    # one table for the whole process (every LISTEN address): request -> [opened, address, still reading, read by, done by]
    _conn_lock, _conns, _reaping = threading.Lock(), {}, []

    def _note(self, ip, why):
        if len(self._logged) > 10000:   # (one entry per refused address: bounded)
            self._logged.clear()
        if time.monotonic() - self._logged.get(ip, float("-inf")) > 60:
            self._logged[ip] = time.monotonic()
            print(log_safe(f"blocked direct connection from {ip}" + why), flush=True)

    def verify_request(self, request, client_address):
        ip = client_address[0]
        if not allowed_ip(ip, request.getsockname()[0]):
            self._note(ip, f" (TRUSTED_PROXY {TRUSTED_PROXY!r} doesn't resolve, so every connection is refused: set it to "
                           "your proxy's container name (docker ps), on a network the dashboard shares)"
                           if TRUSTED_PROXY and not proxy_ips() else "")
            return False
        if not Server._reaping:
            Server._reaping.append(True)
            threading.Thread(target=Server._reaper, daemon=True).start()
        key, now = auth.Throttle._key(ip), time.monotonic()   # (IPv6 per /64)
        with self._conn_lock:
            mine = sum(1 for v in self._conns.values() if v[1] == key)
            if mine >= self.MAX_PER_IP and ip not in proxy_ips():
                self._note(ip, f": too many connections ({mine} from it)")
                return False
            if len(self._conns) >= self.MAX_CONN:
                # full: cut the oldest connection still sending its request, so slow senders (through a proxy that
                # passes bodies on as they come, from many addresses) can't hold every place
                slow = [(v[0], r) for r, v in self._conns.items() if v[2] is True]
                if not slow:
                    self._note(ip, f": too many connections ({len(self._conns)} open)")
                    return False
                oldest = min(slow, key=lambda x: x[0])[1]
                self._conns[oldest][2] = "cut"
                try:
                    oldest.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._conns[request] = [now, key, True, now + self.READ_DEADLINE, now + self.DEADLINE]
        return True

    def read_done(self, request):
        with self._conn_lock:
            if request in self._conns and self._conns[request][2] is True:
                self._conns[request][2] = False

    def proxied(self, request, ip):
        """A connection from the proxy is for this browser address: count it there (False = that address has too many)."""
        key = "via " + auth.Throttle._key(ip)
        with self._conn_lock:
            if request not in self._conns:
                return True
            if sum(1 for r, v in self._conns.items() if v[1] == key and r is not request) >= self.MAX_PER_IP:
                return False
            self._conns[request][1] = key
        return True

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionError, TimeoutError)):
            return   # a browser that left (or a connection cut for taking too long): nothing worth a traceback
        if isinstance(e, RuntimeError) and "thread" in str(e):   # out of threads (the process limit): one line, not a traceback
            return self._note(client_address[0], ": no thread left to answer it")
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        try:
            super().process_request(request, client_address)
        except Exception:   # no thread could be started: the connection is closed, so it no longer counts
            self._done(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done(request)

    def _done(self, request):
        with self._conn_lock:
            self._conns.pop(request, None)

    @staticmethod
    def _reaper():
        """Every second: cut connections still sending their request after READ_DEADLINE, and any after DEADLINE
        (a read timeout alone restarts with every byte)."""
        while True:
            time.sleep(1)
            now = time.monotonic()
            with Server._conn_lock:
                old = [r for r, v in Server._conns.items() if now > v[4] or (v[2] is True and now > v[3])]
            for r in old:
                try:
                    r.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Server6(Server):
    address_family = socket.AF_INET6


def make_servers(listen, port):
    """One server per LISTEN address (space-separated; IPv6 ones get an IPv6 socket; repeats count once)."""
    return [(Server6 if ":" in a else Server)((a, port), Handler) for a in dict.fromkeys(listen.split())]


if __name__ == "__main__":
    threading.stack_size(4 * 1024 * 1024)  # Alpine's default thread stack is tiny; give each request thread room
    port = int(os.environ.get("PORT", "8000"))
    # LISTEN: the addresses to answer on (space-separated). Default: all. Set it to "127.0.0.1 <address on a network
    # only the proxy shares>" so other containers on the proxy's network can't reach the dash at all.
    servers = make_servers(os.environ.get("LISTEN") or "0.0.0.0", port)
    print(f"Fishbowl listening on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
          + (" (DEMO mode, fake data)" if DEMO else "") + f" · data folder {cfgstore.DATA_DIR}", flush=True)
    if TRUSTED_PROXY and not proxy_ips():
        print(f"WARNING: TRUSTED_PROXY {TRUSTED_PROXY!r} doesn't resolve yet, so connections are refused (it's looked up "
              "again every minute; normal for a moment after a reboot if the proxy starts later). If it stays: set it to your "
              "reverse proxy's container name (docker ps) and put both containers on the same Docker network.", flush=True)
    if AUTH == "login":
        try:
            if ACCOUNT.get() is None:
                code = auth.new_setup_code()
                print("=" * 72 + "\nSign-in: there's no account yet. Open the dashboard and create it with this one-time code:\n"
                      f"SETUP CODE: {code}\n(a new one at every start until the account exists: use the newest. To show "
                      "it: docker logs <container> 2>&1 | grep '^SETUP CODE' | tail -n 1. "
                      + ("AUTH: proxy is set but needs TRUSTED_PROXY too, see the WARNING above)" if _auth_env == "proxy" else
                         "AUTH: proxy turns this sign-in off if your reverse proxy's sign-in protects the dashboard)")
                      + "\n" + "=" * 72, flush=True)
            else:
                auth.clear_setup_code()
        except auth.Broken as e:
            print(f"WARNING: sign-in is off: {e}. Reset it: docker exec -it <container> python /app/auth.py reset", flush=True)
    else:
        if os.path.exists(auth.SESSIONS_FILE):   # switching back to login later must not revive old sign-ins
            os.remove(auth.SESSIONS_FILE)
        print(f"Sign-in: AUTH={AUTH} (Fishbowl's own login is off"
              + ("; your reverse proxy's sign-in must protect the dashboard)" if AUTH == "proxy" else ")"), flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()
