"""homelab-dash: a glance dashboard for a self-hosted server.

Stdlib only (Python 3.11+). Settings are edited on the /settings page (stored in the data folder),
optionally seeded from config.toml; secrets can also come from environment variables.
Services are read from Nginx Proxy Manager's proxy_host/*.conf (mounted read-only), so new NPM
hosts show up automatically. Hardware/security stats come from the optional root collector
(see ../collector), which writes a JSON file this app reads.
"""
import gzip
import hashlib
import http.client
import http.cookiejar
import ipaddress
import json
import os
import re
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


def allowed_ip(ip, local=None):
    """May this client talk to us at all? Always when no proxy is configured (LAN use) or in the demo, and when the
    connection comes from the dash itself (its source address = the address it arrived on: the health check of the
    dash's own proxy host connects to dash:8000)."""
    return not TRUSTED_PROXY or DEMO or ip == "127.0.0.1" or ip == local or ip in proxy_ips()


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: https://cdn.jsdelivr.net https://image.tmdb.org; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")



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
    }


def has_login(txt):
    """Does this proxy host put a sign-in in front of / ? Comments and "off" don't count (quoted or not), and a
    directive counts at server level or in `location /` / `location ^~ /` (not in location /admin, nor `location = /`,
    which only covers the bare /). A `location /` that switches it off wins over server level. An include counts only
    if it's an auth-request snippet (Authelia's NPM guide: authelia-authrequest.conf; authelia-location.conf only defines
    the endpoint); an NPM access list counts when it asks for a user and password (auth_basic). Linear in the file."""
    # quoted text becomes a neutral word (so a brace or # inside quotes means nothing; "off" stays off), comments go
    txt = re.sub(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|#[^\n]*""",
                 lambda m: ("off" if m.group(1)[1:-1].strip() == "off" else "quoted") if m.group(1) else "", txt)
    parts = re.split(r"([{};])", txt)
    stack, server, root, root_off = [], False, False, False
    for head, sep in zip(parts[0::2], parts[1::2]):
        head = " ".join(head.split())
        if sep == "{":
            stack.append(head)
            continue
        if sep == "}":
            stack = stack[:-1]
            continue
        locs = [b for b in stack if b.startswith("location")]
        if any(not re.fullmatch(r"location (\^~\s*)?/", b) for b in locs):
            continue                                           # another path (or limit_except/if: not for every request)
        if any(not b.startswith(("location", "server")) for b in stack):
            continue
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
        "hidden_panels": SITE["hidden_panels"], "settings_ui": cfgstore.UI_ENABLED, "demo": DEMO,
        "repo": os.environ.get("REPO_URL", "") if re.fullmatch(r"https://[\w./-]+", os.environ.get("REPO_URL", "")) else "",
    }


# ---------- settings page API ----------

def settings_view(host=None):
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
        "setup": setup_checks(host),
        "about": {"listen": LISTEN or "0.0.0.0 (all addresses)", "user_header": USER_HEADER or "", "data_dir": cfgstore.DATA_DIR,
                  "npm_dir": NPM_DIR, "settings_ui": cfgstore.UI_ENABLED},
    }


def _ago(secs):
    secs = max(0, int(secs))
    return f"{secs} s" if secs < 120 else f"{secs // 60} min" if secs < 7200 else f"{secs // 3600} h"


DOCS = "https://github.com/CoreXI7/homelab-dash/blob/main/docs/SETTINGS.md"


def setup_checks(host=None):
    """The settings page's Setup checklist: [{id, state, title, detail, why, steps: [{text, code}], link, doc, optional,
    compose}]. compose = its fix edits docker-compose.yml (the page then says once: run docker compose up -d).
    state: ok | problem (needs fixing) | warn (works, but worth fixing) | todo (optional, not set up).
    Facts and fixes only: no secrets, no file contents. "%ORIGIN%" in a code line = the page's own address.
    host = the Host the page was opened with (to find the dashboard's own proxy host and its sign-in)."""
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
    if not TRUSTED_PROXY:
        add("proxy", "problem", "Anyone who can reach the dashboard can change it",
            "TRUSTED_PROXY is empty, so anything that can reach port 8000 can open this page and change settings without "
            "going through your sign-in. That's only fine if you run it on your LAN only, on purpose.",
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
    if mine and mine.get("login"):
        add("sso", "ok", "Sign-in in front of the dashboard", f"{mine['name']}'s proxy host in Nginx Proxy Manager has a sign-in step.")
    elif mine:
        add("sso", "warn", "No sign-in found in front of the dashboard",
            f"{mine['name']}'s proxy host in Nginx Proxy Manager has no sign-in step (Authelia, Authentik or an access list). "
            "The dashboard has no login of its own, so anyone who can reach that address sees it and can change these settings. "
            "(Using another kind of sign-in? Then this check can't see it.)",
            [("In Nginx Proxy Manager, open that proxy host and add your sign-in: your SSO's snippet under Advanced "
              "(Authelia and Authentik both have an NPM guide), or an Access List with a username and password.", "")])
    elif TRUSTED_PROXY and name:
        add("sso", "todo", "Couldn't check the sign-in",
            f"{name[:100]} isn't one of Nginx Proxy Manager's proxy hosts here, so this page can't see what's in front of it. "
            "The dashboard has no login of its own: make sure your sign-in (SSO) is in front of it.")

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
        via_proxy = bool(TRUSTED_PROXY) and ip in proxy_ips() and headers is not None
        who = (headers.get("X-Real-IP") if via_proxy else None) or ip
        user = headers.get(USER_HEADER) if via_proxy and USER_HEADER else None
        return log_safe(f"{user} ({who})" if user else who, 120)

    def send(self, code, body, ctype="application/json", cache="no-store", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        # gzip text answers (the proxy only compresses HTML): /api/system shrinks ~4x. No answer carries a secret,
        # so compressing next to request-controlled text can't leak one (BREACH).
        gz = len(body) > 1024 and ctype.split(";")[0] in COMPRESSIBLE and accepts_gzip(self.headers.get("Accept-Encoding"))
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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_revalidated(self, body, ctype):
        """A page, CSS or JS file with an ETag: the browser revalidates every time (no-cache), and gets an empty
        304 when it already has this version. Weak tag: the same content may go out gzipped or not."""
        tag = 'W/"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        if tag in (self.headers.get("If-None-Match") or "").replace(" ", "").split(","):
            return self.send(304, b"", ctype, "no-cache", {"ETag": tag})
        self.send(200, body, ctype, "no-cache", {"ETag": tag})

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

    def same_origin(self):
        """CSRF guard for every POST: the browser's Origin must be the dashboard itself
        (ORIGIN / site.origin if set, otherwise the Host the request was sent to)."""
        if DEMO:
            return True
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if ALLOWED_ORIGINS:
            return origin in ALLOWED_ORIGINS
        return bool(origin) and urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")

    def body(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise cfgstore.Invalid("bad Content-Length")
        if n < 0 or n > limit:
            raise cfgstore.Invalid(f"too large (max {limit // 1024} KB)")
        return self.rfile.read(n)

    def json_body(self, limit):
        """Parse a JSON object safely: deeply nested input ([[[[...) would overflow the parser's C stack and kill
        the whole process, so nesting is checked with a cheap scan first."""
        raw = self.body(limit)
        depth = deepest = 0
        in_str = esc_next = False
        for ch in raw.decode("utf-8", "replace"):
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

    def do_GET(self):
        self.guarded(self.get)

    def do_POST(self):
        self.guarded(self.post)

    def guarded(self, fn):
        """No raw exception (whose text can include upstream URLs) ever reaches the browser or the log."""
        try:
            fn()
        except cfgstore.Invalid as e:
            self.send(400, {"error": str(e), **({"section": e.section} if e.section else {})})
        except Exception as e:
            print(log_safe(f"error on {self.command} {self.path[:80]}: {scrub(f'{type(e).__name__}: {e}')}"), flush=True)
            try:
                self.send(502, {"error": "upstream error (see container log)"})
            except Exception:
                pass

    def get(self):
        url = urllib.parse.urlparse(self.path)
        p, query = url.path, urllib.parse.parse_qs(url.query)
        if not self.allowed():
            return
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
            self.send(200, settings_view(self.headers.get("Host")))
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
            self.send(200, open(f, "rb").read(), ctype, "public, max-age=604800",
                      {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        elif p == "/healthz":
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "not found"})

    def post(self):
        if not self.allowed():
            return
        if not self.same_origin():
            return self.send(403, {"error": "bad origin: open the dashboard by its own address (or set ORIGIN)"})
        who = self.client()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/settings":
            d = self.json_body(256 * 1024)
            with _cfg_lock:
                new = cfgstore.validate(d.get("settings"), ED)
                store = cfgstore._in("integrations", cfgstore.apply_secret_changes, new, ED, cfgstore._obj(d.get("secrets"), "secrets"), STORE)
                if VIA_PROXY_ENV:         # set in the environment: it wins, so keep what's saved (as for integration URLs)
                    new["via_proxy"] = ED["via_proxy"]
                if DEMO:                  # public demo: check everything, store nothing (visitors can't change it for others)
                    return self.send(200, {**settings_view(self.headers.get("Host")), "settings": new, "demo_not_saved": True})
                cfgstore._in("setup", cfgstore.save, new, store)
                apply_settings()
            print(f"settings changed by {who}", flush=True)
            return self.send(200, settings_view(self.headers.get("Host")))
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


class Server(ThreadingHTTPServer):
    """Refuses a client that isn't the proxy before a thread is started for it: otherwise any container on the
    proxy's network could hold the dash's threads (pids_limit) with idle connections. Logged once a minute per IP."""
    daemon_threads = True
    _logged = {}

    def verify_request(self, request, client_address):
        ip = client_address[0]
        if allowed_ip(ip, request.getsockname()[0]):
            return True
        if time.monotonic() - self._logged.get(ip, float("-inf")) > 60:
            self._logged[ip] = time.monotonic()
            print(log_safe(f"blocked direct connection from {ip}" + (f" (TRUSTED_PROXY {TRUSTED_PROXY!r} doesn't resolve, so "
                  "every connection is refused: set it to your proxy's container name (docker ps), on a network the "
                  "dashboard shares)" if TRUSTED_PROXY and not proxy_ips() else "")), flush=True)
        return False


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
    print(f"homelab-dash listening on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
          + (" (DEMO mode, fake data)" if DEMO else "") + f" · data folder {cfgstore.DATA_DIR}", flush=True)
    if TRUSTED_PROXY and not proxy_ips():
        print(f"WARNING: TRUSTED_PROXY {TRUSTED_PROXY!r} doesn't resolve yet, so connections are refused (it's looked up "
              "again every minute; normal for a moment after a reboot if the proxy starts later). If it stays: set it to your "
              "reverse proxy's container name (docker ps) and put both containers on the same Docker network.", flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()
