"""homelab-dash: a glance dashboard for a self-hosted server.

Stdlib only (Python 3.11+). Settings are edited on the /settings page (stored in the data folder),
optionally seeded from config.toml; secrets can also come from environment variables.
Services are read from Nginx Proxy Manager's proxy_host/*.conf (mounted read-only), so new NPM
hosts show up automatically. Hardware/security stats come from the optional root collector
(see ../collector), which writes a JSON file this app reads.
"""
import functools
import glob
import hashlib
import http.client
import http.cookiejar
import ipaddress
import json
import os
import re
import socket
import sys
import ssl
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

DEMO = cfgstore.DEMO
HW_FILE = os.environ.get("HW_FILE", "/hw/hw.json")
REQ_DIR = os.environ.get("REQ_DIR", "/req")
INFRA = cfgstore.infra()
NPM_DIR = INFRA["npm_dir"]
TRUSTED_PROXY = INFRA["trusted_proxy"]      # e.g. "nginx": only that host may talk to us
ALLOWED_ORIGINS = set(INFRA["origins"])
USER_HEADER = os.environ.get("USER_HEADER", "")   # e.g. Remote-User: logged only if your proxy sets (overwrites) it
_cfg_lock = threading.Lock()


def apply_settings():
    """(Re)build everything that depends on settings. Called at start and after every save from the GUI."""
    global ED, STORE, SITE, SKIP_HOSTS, EXTRA_SERVICES, KNOWN_IPS, KNOWN, QBIT, SEERR, TAUTULLI, HOME_IP, SECRETS, qb
    ED, STORE = cfgstore.load(), cfgstore.load_secrets()
    SITE = ED["site"]
    SKIP_HOSTS = set(ED["skip_hosts"])
    EXTRA_SERVICES = ED["services"]                     # [{name, url}] for hosts not in NPM
    KNOWN_IPS = ED["known_ips"]
    KNOWN = known_matcher(KNOWN_IPS)

    def integration(name):
        url, _ = cfgstore.url_of(name, ED)
        secrets = [cfgstore.secret(field, STORE)[0] for field, _env in cfgstore.INTEGRATIONS[name][1]]
        return {"url": url, "secrets": secrets, "on": bool(url and all(secrets)) or DEMO}

    QBIT, SEERR, TAUTULLI = integration("qbittorrent"), integration("seerr"), integration("tautulli")
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
    """Every address of the trusted reverse proxy (a container on two networks has one per network), looked up at
    most once a minute: they can change when it's recreated. The last good answer is kept if a lookup fails."""
    if time.monotonic() - _proxy_ip["at"] > 60:
        try:
            _proxy_ip["ips"] = {a[4][0] for a in socket.getaddrinfo(TRUSTED_PROXY, None)}
        except OSError:
            pass
        _proxy_ip["at"] = time.monotonic()
    return _proxy_ip["ips"]


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: https://cdn.jsdelivr.net https://image.tmdb.org; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

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
        "auth": bool(re.search(r"auth_request\s+/(authelia|authentik|outpost)", txt)),   # behind an SSO proxy
    }


_npm = {"sig": None, "hosts": []}   # last parsed host list + the folder state it came from


def read_npm_hosts():
    """Every service to show: NPM's proxy hosts plus [[services]] from the settings, minus skipped ones.
    The .conf files are only re-read when one was added, removed or edited (checked by name + mtime)."""
    try:
        sig = tuple(sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(NPM_DIR) if e.name.endswith(".conf")))
    except OSError:
        sig = ()
    if sig != _npm["sig"]:
        hosts = [h for h in (parse_npm_conf(os.path.join(NPM_DIR, n)) for n, _ in sig) if h and h["name"] not in SKIP_HOSTS]
        for s in EXTRA_SERVICES:
            if s.get("name") and s.get("url") and s["name"] not in SKIP_HOSTS:
                hosts.append({"name": s["name"], "upstream": s["url"].rstrip("/"), "auth": bool(s.get("auth"))})
        hosts.sort(key=lambda h: (h["name"].split(".", 1)[-1], h["name"]))   # group by domain, then name
        _npm.update(sig=sig, hosts=hosts)
    return _npm["hosts"]


def check(host):
    """Is this service answering? Any HTTP reply below 500 counts as up (a login page is fine)."""
    r = check_once(host)
    if not r["up"] and r["code"] is None:   # retry once: socket-activated apps (e.g. Cockpit) wake slowly
        r = check_once(host, timeout=10)
    return r


# Hosts the dashboard can't reach directly (e.g. on a network only the proxy shares): check them through the
# proxy instead, the way visitors reach them. Env CHECK_VIA_PROXY="demo.example.com other.example.com/health".
# An optional path is checked instead of / (useful when / sits behind a login the proxy enforces).
VIA_PROXY = dict((e.split("/", 1)[0], "/" + e.split("/", 1)[1] if "/" in e else "/")
                 for e in os.environ.get("CHECK_VIA_PROXY", "").split())


class _SNIConnection(http.client.HTTPSConnection):
    """HTTPS to the proxy container while presenting the site's own name (proxies refuse unknown names)."""
    def __init__(self, host, sni, **kw):
        super().__init__(host, **kw)
        self.sni = sni

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.sni)


def check_via_proxy(host, timeout):
    t0 = time.monotonic()
    conn = _SNIConnection(TRUSTED_PROXY or "nginx", host["name"], port=443, timeout=timeout, context=INSECURE)
    try:
        conn.request("GET", VIA_PROXY[host["name"]], headers={"Host": host["name"], "User-Agent": UA})
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
    if host["name"] in VIA_PROXY:
        return check_via_proxy(host, timeout)
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
        val = fn()
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


# ---------- host system (container sees host /proc + /sys/class/hwmon; the rest comes from the collector) ----------

_cpu_prev = {}          # previous /proc/stat counters for /api/system
_live_prev = {}         # ... and a separate set for /api/live, so the two endpoints don't skew each other's deltas


def cpu_usage(prev=_cpu_prev):
    """Per-CPU busy % since the previous call with the same prev dict (first call: since boot)."""
    out = {}
    for line in open("/proc/stat"):
        if not line.startswith("cpu"):
            break
        f = line.split()
        vals = list(map(int, f[1:9]))
        idle, total = vals[3] + vals[4], sum(vals)
        p_idle, p_total = prev.get(f[0], (0, 0))
        prev[f[0]] = (idle, total)
        dt = total - p_total
        out[f[0]] = round(100 * (1 - (idle - p_idle) / dt), 1) if dt > 0 else 0.0
    return out


@functools.lru_cache(maxsize=1)                 # the CPU doesn't change while we run
def cpu_model():
    """Short CPU name for the System panel, e.g. "Ryzen 9 9950X"."""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                name = line.split(":", 1)[1].strip()
                name = re.sub(r"\s+\d+-Core Processor|\(R\)|\(TM\)|CPU @.*", "", name)
                return re.sub(r"^(AMD|Intel)\s+", "", name.strip())
    except OSError:
        pass
    return ""


_sensors = {"at": float("-inf"), "list": []}   # [(chip, label, path of its temp*_input)], rescanned every 5 min


def sensor_files():
    """Find every temperature sensor once (chip names and labels don't change), then just read the values."""
    if time.monotonic() - _sensors["at"] > 300:
        found = []
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                with open(d + "/name") as f:
                    chip = f.read().strip()
            except OSError:
                continue
            for inp in sorted(glob.glob(d + "/temp*_input")):
                try:
                    with open(inp.replace("_input", "_label")) as f:
                        label = f.read().strip()
                except OSError:
                    label = os.path.basename(inp).split("_")[0]   # no label file: "temp1"
                found.append((chip, label, inp))
        _sensors.update(at=time.monotonic(), list=found)
    return _sensors["list"]


def hwmon():
    """Temperature cells from whatever sensors this machine has (AMD k10temp/zenpower, Intel coretemp, DDR5, NVMe, amdgpu)."""
    raw = {}                                   # chip -> [(label, °C)]
    for chip, lbl, inp in sensor_files():
        try:
            with open(inp) as f:
                v = int(f.read()) / 1000
        except (OSError, ValueError):
            continue
        raw.setdefault(chip, []).append((lbl, v))
    cells, cpu = [], None
    for chip in ("k10temp", "zenpower"):
        for lbl, v in raw.get(chip, []):
            if lbl in ("Tctl", "Tdie") and cpu is None:
                cpu = v
                cells.append({"label": "CPU", "c": v, "warn": 80, "crit": 90})
            elif lbl.startswith("Tccd"):
                cells.append({"label": "CPU CCD" + lbl[4:], "c": v, "warn": 80, "crit": 90})
    for lbl, v in raw.get("coretemp", []):
        if lbl.startswith("Package") and cpu is None:
            cpu = v
            cells.append({"label": "CPU", "c": v, "warn": 80, "crit": 95})
    for i, (lbl, v) in enumerate(raw.get("amdgpu", [])[:1]):
        cells.append({"label": "GPU", "c": v, "warn": 80, "crit": 95})
    nvmes = [v for lbl, v in raw.get("nvme", []) if lbl in ("Composite", "temp1")]
    for i, v in enumerate(nvmes):
        cells.append({"label": "NVMe" + (f" {i + 1}" if len(nvmes) > 1 else ""), "c": v, "warn": 65, "crit": 75})
    ram = [v for chip in ("spd5118", "jc42") for _, v in raw.get(chip, [])]
    for i, v in enumerate(ram):
        cells.append({"label": f"RAM {i + 1}", "c": v, "warn": 70, "crit": 80})
    return {"cpu": cpu, "cells": cells}


def known_matcher(entries):
    """Known-IP entries split for is_known(): full addresses in canonical form (IPv6 has many spellings), and
    prefixes ("203.0.113." / "2001:db8:"), lower-cased."""
    full, prefixes = set(), []
    for k in entries:
        try:
            full.add(str(ipaddress.ip_address(k)))
        except ValueError:
            prefixes.append(k.lower())
    return full, tuple(prefixes)


def is_known(ip):
    """Is this IP on your known list (an exact address, or inside one of the prefixes)?"""
    if not isinstance(ip, str) or not ip:
        return False
    full, prefixes = KNOWN
    try:
        canon = str(ipaddress.ip_address(ip))
    except ValueError:
        canon = ""
    return canon in full or ip.lower().startswith(prefixes) or bool(canon and canon.startswith(prefixes))


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
    hw, detail = (demo.system(), demo.security_detail()) if DEMO else (read_hw(), security_detail())
    seen = {l.get("ip") for l in (((hw or {}).get("security") or {}).get("ssh") or {}).get("logins", [])}
    ssh = detail.get("ssh") or {}
    seen |= {x.get("ip") for x in ssh.get("logins", []) + ssh.get("sources", [])}
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

@functools.lru_cache(maxsize=1)
def cpu_topology():
    """Per CPU thread: which group it belongs to (CCD / L3 cache cluster on AMD, package elsewhere), which core
    (= the core's first thread number) and whether it is an SMT sibling (not that first thread). Read once from /sys;
    [] if the folders aren't there."""
    out, groups = [], {}
    n = 0
    while os.path.isdir(f"/sys/devices/system/cpu/cpu{n}"):
        base = f"/sys/devices/system/cpu/cpu{n}"
        def rd(path, default=""):
            try:
                with open(f"{base}/{path}") as f:
                    return f.read().strip()
            except OSError:
                return default
        key = rd("cache/index3/id") or rd("topology/physical_package_id", "0")
        group = groups.setdefault(key, len(groups))
        siblings = rd("topology/thread_siblings_list", str(n))
        first = int(siblings.replace("-", ",").split(",")[0]) if siblings else n
        out.append({"group": group, "core": first, "smt": first != n})
        n += 1
    return out


def cpu_mhz():
    """Current clock of every thread, in /proc/cpuinfo order (= thread number)."""
    mhz = []
    for line in open("/proc/cpuinfo"):
        if line.startswith("cpu MHz"):
            mhz.append(int(float(line.split(":", 1)[1])))
    return mhz


_io_prev = {"t": None, "stats": {}}
DISK_RE = re.compile(r"(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)")   # whole disks, not partitions


def disk_io(names):
    """Read/write MB/s and % busy per physical disk since the previous call. names: {"sdb": "disk1"}."""
    now, stats = time.monotonic(), {}
    for line in open("/proc/diskstats"):
        f = line.split()
        if len(f) >= 13 and DISK_RE.fullmatch(f[2]):
            stats[f[2]] = (int(f[5]), int(f[9]), int(f[12]))   # sectors read, sectors written, ms busy
    prev, dt = _io_prev["stats"], (now - _io_prev["t"]) if _io_prev["t"] else 0
    _io_prev.update(t=now, stats=stats)
    disks = []
    for dev, (r, w, busy) in stats.items():
        if dev in prev and dt > 0:
            pr, pw, pb = prev[dev]
            disks.append({"dev": dev, "name": names.get(dev, dev), "read_bps": (r - pr) * 512 / dt,
                          "write_bps": (w - pw) * 512 / dt, "busy": min(100.0, (busy - pb) / (dt * 10))})
    disks.sort(key=lambda d: -d["busy"])
    return {"read_bps": sum(d["read_bps"] for d in disks), "write_bps": sum(d["write_bps"] for d in disks), "disks": disks}


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


# ---------- qBittorrent ----------

class Qbit:
    """qBittorrent Web API client. Keeps the login cookie and logs in again when it expires (403)."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)
        self.lock = threading.Lock()

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
                        return json.loads(read_capped(r, QBIT_MAX))
                except urllib.error.HTTPError as e:
                    if e.code == 403 and attempt == 1:
                        self.login()
                        continue
                    raise


SLIM_TEXT = ("name", "state", "category")
SLIM_NUM = ("progress", "dlspeed", "upspeed", "eta", "size", "num_seeds", "num_leechs", "ratio", "added_on", "uploaded")


def qbit():
    """/api/qbit: speeds, the VPN check (external IP vs your home IP) and the torrent lists, trimmed to what the page shows."""
    t = qb.get("/api/v2/transfer/info")
    torrents = qb.get("/api/v2/torrents/info")
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
        return json.loads(read_capped(r, API_MAX))["response"]["data"]


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
        return r.read(2_000_000), r.headers.get("Content-Type", "image/jpeg")


# ---------- Seerr / Overseerr / Jellyseerr (same API) ----------

def seerr(path, method="GET"):
    req = urllib.request.Request(SEERR["url"] + "/api/v1" + path, method=method,
                                 headers={"X-Api-Key": SEERR["secrets"][0], "Accept": "application/json"})
    with STRICT.open(req, timeout=10) as r:
        body = read_capped(r, API_MAX)
        return json.loads(body) if body else {}


_titles = {}


def media_info(kind, tmdb):
    """Title/year/poster for a request. Cached forever: a movie's title doesn't change."""
    key = (kind, tmdb)
    if kind not in ("movie", "tv") or not isinstance(tmdb, int) or isinstance(tmdb, bool):
        return {"title": "unknown title", "year": "", "poster": None, "overview": ""}
    if key not in _titles:
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
        "settings_ui": cfgstore.UI_ENABLED, "demo": DEMO,
        "repo": os.environ.get("REPO_URL", "") if re.fullmatch(r"https://[\w./-]+", os.environ.get("REPO_URL", "")) else "",
    }


# ---------- settings page API ----------

def settings_view():
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
    }


def test_integration(name):
    """Try an integration with its SAVED settings (never an address typed in, so keys can't be sent elsewhere)."""
    if DEMO:
        return {"ok": True, "detail": "demo mode"}
    i = {"qbittorrent": QBIT, "seerr": SEERR, "tautulli": TAUTULLI}.get(name)
    if not i:
        raise cfgstore.Invalid("unknown integration")
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
    except Exception as e:
        return {"ok": False, "detail": scrub(f"{type(e).__name__}: {e}")}


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
apply_settings()


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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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

    def safe(self, fn):
        try:
            self.send(200, fn())
        except Exception as e:
            self.send(502, {"error": scrub(f"{type(e).__name__}: {e}")})

    def allowed(self):
        if not TRUSTED_PROXY or DEMO:
            return True
        ip = self.client_address[0]
        if ip == "127.0.0.1" or ip in proxy_ips():
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
            self.send(400, {"error": str(e)})
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
            self.send(200, page, "text/html; charset=utf-8")
        elif p in ("/style.css", "/themes.css"):
            self.send(200, open(os.path.join(HERE, p[1:]), "rb").read(), "text/css; charset=utf-8", "no-cache")
        elif p in API:
            ttl, fn = API[p]
            if p in FEATURE and not FEATURE[p]()["on"]:
                return self.send(404, {"error": "integration not configured"})
            fn = getattr(demo, fn.__name__, fn) if DEMO else fn
            self.safe(lambda: cached(p, ttl, fn) if ttl else fn())
        elif p == "/api/settings":
            self.send(200, settings_view())
        elif p == "/api/plexart" and TAUTULLI["on"] and not DEMO:
            try:
                art = plex_art(query.get("img", [""])[0])
            except Exception:
                art = None
            if not art:
                return self.send(404, {"error": "no art"})
            self.send(200, art[0], art[1], "private, max-age=86400")
        elif p == "/api/icon" and not DEMO:
            ico = favicon(query.get("host", [""])[0])
            if not ico:
                return self.send(404, {"error": "no icon"})
            self.send(200, ico[0], ico[1], "private, max-age=86400", {"Content-Security-Policy": "default-src 'none'"})
        elif p == "/favicon.ico":
            self.path = "/static/favicon.svg"
            return self.get()
        elif m := re.fullmatch(r"/(static|uploads)/([\w.-]+\.(png|svg|webp|ico|jpg|gif))", p):
            folder = os.path.join(HERE, "static") if m[1] == "static" else cfgstore.UPLOAD_DIR
            f = os.path.join(folder, m[2])
            # Web uploads are raster-only (save_upload); an SVG in uploads/ was put there by the admin on disk.
            # Either way it's served with scripts blocked (CSP below), so even a hostile SVG can't run anything.
            if not os.path.isfile(f) or (m[1] == "uploads" and m[3] == "ico"):
                return self.send(404, {"error": "not found"})
            ctype = {"png": "image/png", "svg": "image/svg+xml", "webp": "image/webp", "ico": "image/x-icon",
                     "jpg": "image/jpeg", "gif": "image/gif"}[m[3]]
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
                store = cfgstore.apply_secret_changes(new, ED, d.get("secrets") or {}, STORE)
                if DEMO:                  # public demo: check everything, store nothing (visitors can't change it for others)
                    return self.send(200, {**settings_view(), "settings": new, "demo_not_saved": True})
                cfgstore.save(new, store)
                apply_settings()
            print(f"settings changed by {who}", flush=True)
            return self.send(200, settings_view())
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


if __name__ == "__main__":
    threading.stack_size(4 * 1024 * 1024)  # Alpine's default thread stack is tiny; give each request thread room
    ThreadingHTTPServer.daemon_threads = True
    port = int(os.environ.get("PORT", "8000"))
    # LISTEN: the addresses to answer on (space-separated). Default: all. Set it to "127.0.0.1 <address on a network
    # only the proxy shares>" so other containers on the proxy's network can't reach the dash at all.
    servers = [ThreadingHTTPServer((a, port), Handler) for a in (os.environ.get("LISTEN") or "0.0.0.0").split()]
    print(f"homelab-dash listening on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
          + (" (DEMO mode, fake data)" if DEMO else "") + f" · data folder {cfgstore.DATA_DIR}", flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()
