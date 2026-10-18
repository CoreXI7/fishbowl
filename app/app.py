"""homelab-dash: a glance dashboard for a self-hosted server.

Stdlib only (Python 3.11+). Settings are edited on the /settings page (stored in the data folder),
optionally seeded from config.toml; secrets can also come from environment variables.
Services are read from Nginx Proxy Manager's proxy_host/*.conf (mounted read-only), so new NPM
hosts show up automatically. Hardware/security stats come from the optional root collector
(see ../collector), which writes a JSON file this app reads.
"""
import glob
import hashlib
import http.client
import http.cookiejar
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
_cfg_lock = threading.Lock()


def apply_settings():
    """(Re)build everything that depends on settings. Called at start and after every save from the GUI."""
    global ED, STORE, SITE, SKIP_HOSTS, EXTRA_SERVICES, KNOWN_IPS, QBIT, SEERR, TAUTULLI, HOME_IP, SECRETS, qb
    ED, STORE = cfgstore.load(), cfgstore.load_secrets()
    SITE = ED["site"]
    SKIP_HOSTS = set(ED["skip_hosts"])
    EXTRA_SERVICES = ED["services"]                     # [{name, url}] for hosts not in NPM
    KNOWN_IPS = ED["known_ips"]

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


_proxy_ip = {"ip": None, "at": 0}


def scrub(text):
    """Remove anything secret from text that may reach the browser or logs (error messages can echo URLs)."""
    text = str(text)
    for v in SECRETS:
        text = text.replace(v, "***").replace(urllib.parse.quote_plus(v), "***").replace(urllib.parse.quote(v, safe=""), "***")
    text = re.sub(r"(?i)(api_?key|token|pass(word)?|secret)=[^&\s'\"]*", r"\1=***", text)
    return re.sub(r"(https?://[^\s'\"?]*)\?[^\s'\"]*", r"\1?…", text)[:300]


def proxy_ip():
    if time.monotonic() - _proxy_ip["at"] > 60:
        try:
            _proxy_ip["ip"] = socket.gethostbyname(TRUSTED_PROXY)
        except OSError:
            pass
        _proxy_ip["at"] = time.monotonic()
    return _proxy_ip["ip"]


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: https://cdn.jsdelivr.net https://image.tmdb.org; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

INSECURE = ssl.create_default_context()      # upstreams are on the LAN, often with self-signed certs
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE
UA = "homelab-dash"


# ---------- services (from NPM + config) ----------

def read_npm_hosts():
    hosts = []
    for f in glob.glob(os.path.join(NPM_DIR, "*.conf")):
        try:
            txt = open(f).read()
        except OSError:
            continue
        name = re.search(r"server_name\s+([^\s;]+)", txt)
        server = re.search(r'set \$server\s+"?([^";]+)"?;', txt)
        port = re.search(r"set \$port\s+(\d+);", txt)
        scheme = re.search(r"set \$forward_scheme\s+(\w+);", txt)
        if not (name and server and port) or name.group(1) in SKIP_HOSTS:
            continue
        hosts.append({
            "name": name.group(1),
            "upstream": f"{scheme.group(1) if scheme else 'http'}://{server.group(1)}:{port.group(1)}",
            "auth": bool(re.search(r"auth_request\s+/(authelia|authentik|outpost)", txt)),
        })
    for s in EXTRA_SERVICES:
        if s.get("name") and s.get("url") and s["name"] not in SKIP_HOSTS:
            hosts.append({"name": s["name"], "upstream": s["url"].rstrip("/"), "auth": bool(s.get("auth"))})
    return sorted(hosts, key=lambda h: (h["name"].split(".", 1)[-1], h["name"]))


def check(host):
    r = check_once(host)
    if not r["up"] and r["code"] is None:   # retry once: socket-activated apps (e.g. Cockpit) wake slowly
        r = check_once(host, timeout=10)
    return r


# Hosts the dashboard can't reach directly (e.g. on a network only the proxy shares): check them through the
# proxy instead, the way visitors reach them. Env CHECK_VIA_PROXY="demo.example.com other.example.com".
VIA_PROXY = set(os.environ.get("CHECK_VIA_PROXY", "").split())


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
        conn.request("GET", "/", headers={"Host": host["name"], "User-Agent": UA})
        code = conn.getresponse().status
    except Exception as e:
        return {"name": host["name"], "auth": host["auth"], "up": False, "code": None, "ms": None, "err": type(e).__name__}
    finally:
        conn.close()
    return {"name": host["name"], "auth": host["auth"], "up": code < 500, "code": code,
            "ms": round((time.monotonic() - t0) * 1000), "err": None}


def check_once(host, timeout=5):
    if host["name"] in VIA_PROXY:
        return check_via_proxy(host, timeout)
    t0 = time.monotonic()
    req = urllib.request.Request(host["upstream"] + "/", method="GET", headers={"User-Agent": UA, "Host": host["name"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=INSECURE) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code                     # 401/403/404 still means the app answered
    except Exception as e:
        return {"name": host["name"], "auth": host["auth"], "up": False, "code": None, "ms": None, "err": type(e).__name__}
    ms = round((time.monotonic() - t0) * 1000)
    return {"name": host["name"], "auth": host["auth"], "up": code < 500, "code": code, "ms": ms, "err": None}


_cache = {}
_lock = threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
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
    try:
        req = urllib.request.Request(base + "/", headers={"User-Agent": UA, "Host": name})
        with urllib.request.urlopen(req, timeout=5, context=INSECURE) as r:
            html = r.read(200_000).decode("utf-8", "replace")
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            if re.search(r'rel=["\'][^"\']*icon', tag, re.I):
                href = re.search(r'href=["\']([^"\']+)', tag, re.I)
                if href and not href.group(1).startswith("data:"):
                    cands.append(urllib.parse.urljoin(base + "/", href.group(1)))
    except Exception:
        pass
    cands.append(base + "/favicon.ico")
    result = None
    for url in cands:
        if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(base).netloc:
            continue                      # stay on the service's own upstream
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Host": name})
            with urllib.request.urlopen(req, timeout=5, context=INSECURE) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0]
                data = r.read(512_000)
            if ctype.startswith("image/") and ctype != "image/svg+xml" and data:
                result = (data, ctype)
                break
        except Exception:
            continue
    _icons[name] = result
    return result


def services():
    hosts = read_npm_hosts()
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(check, hosts))
    return {"checked": int(time.time()), "services": results}


# ---------- host system (container sees host /proc + /sys/class/hwmon; the rest comes from the collector) ----------

_cpu_prev = {}


def cpu_usage():
    """Per-CPU busy % since the previous call (first call: since boot)."""
    out = {}
    for line in open("/proc/stat"):
        if not line.startswith("cpu"):
            break
        f = line.split()
        vals = list(map(int, f[1:9]))
        idle, total = vals[3] + vals[4], sum(vals)
        p_idle, p_total = _cpu_prev.get(f[0], (0, 0))
        _cpu_prev[f[0]] = (idle, total)
        dt = total - p_total
        out[f[0]] = round(100 * (1 - (idle - p_idle) / dt), 1) if dt > 0 else 0.0
    return out


def cpu_model():
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                name = line.split(":", 1)[1].strip()
                name = re.sub(r"\s+\d+-Core Processor|\(R\)|\(TM\)|CPU @.*", "", name)
                return re.sub(r"^(AMD|Intel)\s+", "", name.strip())
    except OSError:
        pass
    return ""


def hwmon():
    """Temperature cells from whatever sensors this machine has (AMD k10temp/zenpower, Intel coretemp, DDR5, NVMe, amdgpu)."""
    raw = {}
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(d + "/name").read().strip()
        except OSError:
            continue
        for inp in sorted(glob.glob(d + "/temp*_input")):
            try:
                v = int(open(inp).read()) / 1000
            except (OSError, ValueError):
                continue
            lbl_f = inp.replace("_input", "_label")
            lbl = open(lbl_f).read().strip() if os.path.exists(lbl_f) else os.path.basename(inp).split("_")[0]
            raw.setdefault(name, []).append((lbl, v))
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


def is_known(ip):
    return any(ip == k or (k.endswith(".") and ip.startswith(k)) for k in KNOWN_IPS)


ACKS_FILE = os.path.join(cfgstore.DATA_DIR, "acks.json")   # logins you marked "looks fine" on the dashboard


def login_key(item):
    return hashlib.sha256(f"{item.get('time')}|{item.get('user')}|{item.get('ip')}".encode()).hexdigest()[:20]


def load_acks():
    try:
        return set(json.load(open(ACKS_FILE)))
    except Exception:
        return set()


def read_hw():
    try:
        hw = json.load(open(HW_FILE))
    except Exception:
        return None
    hw["stale"] = time.time() - hw.get("updated", 0) > 60
    sec = hw.get("security") or {}
    acks = load_acks()
    for item in (sec.get("ssh") or {}).get("logins", []) + (sec.get("authelia") or {}).get("recent", []):
        item["known"] = is_known(item.get("ip", ""))
        item["acked"] = login_key(item) in acks
    return hw


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


def system():
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


# ---------- qBittorrent ----------

class Qbit:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.lock = threading.Lock()

    def login(self):
        user, pw = QBIT["secrets"]
        data = urllib.parse.urlencode({"username": user, "password": pw}).encode()
        req = urllib.request.Request(QBIT["url"] + "/api/v2/auth/login", data=data, headers={"Referer": QBIT["url"]})
        self.op.open(req, timeout=10).read()

    def get(self, path):
        with self.lock:
            for attempt in (1, 2):
                try:
                    with self.op.open(QBIT["url"] + path, timeout=10) as r:
                        return json.load(r)
                except urllib.error.HTTPError as e:
                    if e.code == 403 and attempt == 1:
                        self.login()
                        continue
                    raise


SLIM = ("name", "state", "progress", "dlspeed", "upspeed", "eta", "size", "num_seeds",
        "num_leechs", "ratio", "category", "added_on", "uploaded")


def qbit():
    t = qb.get("/api/v2/transfer/info")
    torrents = qb.get("/api/v2/torrents/info")
    slim = lambda x: {k: x.get(k) for k in SLIM}
    dl_states = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "checkingDL", "pausedDL", "stoppedDL"}
    downloading = [slim(x) for x in torrents if x["state"] in dl_states]
    downloading.sort(key=lambda x: (-x["dlspeed"], -x["progress"]))
    seeding = [slim(x) for x in torrents if x["upspeed"] > 0 and x["state"] not in dl_states]
    seeding.sort(key=lambda x: -x["upspeed"])
    ext = t.get("last_external_address_v4") or ""
    return {
        "connection": t.get("connection_status"),
        "external_ip": ext,
        "vpn_check": bool(HOME_IP),
        "vpn_leak": bool(HOME_IP and ext == HOME_IP),
        "dl_speed": t.get("dl_info_speed"), "up_speed": t.get("up_info_speed"),
        "dl_total": t.get("dl_info_data"), "up_total": t.get("up_info_data"),
        "total": len(torrents),
        "downloading": downloading, "seeding": seeding[:30], "seeding_active": len(seeding),
    }


# ---------- Plex via Tautulli ----------

def tautulli(cmd, **params):
    q = urllib.parse.urlencode({"apikey": TAUTULLI["secrets"][0], "cmd": cmd, **params})
    with urllib.request.urlopen(f"{TAUTULLI['url']}/api/v2?{q}", timeout=10) as r:
        return json.load(r)["response"]["data"]


PLEX_FIELDS = ("session_key", "friendly_name", "full_title", "title", "grandparent_title", "parent_media_index",
               "media_index", "year", "media_type", "state", "progress_percent", "transcode_decision",
               "video_decision", "audio_decision", "stream_video_full_resolution", "video_full_resolution",
               "bandwidth", "product", "player", "platform", "location", "transcode_hw_encoding",
               "transcode_hw_decoding", "duration", "view_offset")


def plex():
    d = tautulli("get_activity")
    sessions = []
    for s in d.get("sessions", []):
        x = {k: s.get(k) for k in PLEX_FIELDS}
        thumb = s.get("grandparent_thumb") if s.get("media_type") == "episode" else s.get("thumb")
        x["art"] = "/api/plexart?img=" + urllib.parse.quote(thumb or "", safe="") if thumb else None
        sessions.append(x)
    return {"stream_count": int(d.get("stream_count") or 0), "transcodes": int(d.get("stream_count_transcode") or 0),
            "bandwidth": int(d.get("total_bandwidth") or 0), "sessions": sessions}


def plex_art(img):
    """Poster art goes through Tautulli's image proxy, so the Plex token never reaches the browser."""
    if not re.fullmatch(r"/library/metadata/\d+/(thumb|art)(/\d+)?", img or ""):
        return None
    q = urllib.parse.urlencode({"apikey": TAUTULLI["secrets"][0], "cmd": "pms_image_proxy", "img": img, "width": 200, "height": 300})
    with urllib.request.urlopen(f"{TAUTULLI['url']}/api/v2?{q}", timeout=10) as r:
        return r.read(2_000_000), r.headers.get("Content-Type", "image/jpeg")


# ---------- Seerr / Overseerr / Jellyseerr (same API) ----------

def seerr(path, method="GET"):
    req = urllib.request.Request(SEERR["url"] + "/api/v1" + path, method=method,
                                 headers={"X-Api-Key": SEERR["secrets"][0], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return json.loads(body) if body else {}


_titles = {}


def media_info(kind, tmdb):
    key = (kind, tmdb)
    if key not in _titles:
        try:
            d = seerr(f"/{kind}/{tmdb}")
            date = d.get("releaseDate") or d.get("firstAirDate") or ""
            _titles[key] = {"title": d.get("title") or d.get("name") or f"TMDB {tmdb}",
                            "year": date[:4], "poster": d.get("posterPath"),
                            "overview": (d.get("overview") or "")[:280]}
        except Exception:
            return {"title": f"TMDB {tmdb}", "year": "", "poster": None, "overview": ""}
    return _titles[key]


def requests_pending():
    d = seerr("/request?take=50&filter=pending&sort=added")
    out = []
    for r in d.get("results", []):
        m = r.get("media") or {}
        info = media_info(r["type"], m.get("tmdbId"))
        seasons = [s["seasonNumber"] for s in r.get("seasons") or []]
        u = r.get("requestedBy") or {}
        out.append({"id": r["id"], "type": r["type"], "is4k": r.get("is4k"), "created": r.get("createdAt"),
                    "user": u.get("displayName") or u.get("plexUsername") or u.get("email"),
                    "seasons": seasons, **info})
    return {"pending": out}


# ---------- public (non-secret) config for the dashboard page ----------

def page_config():
    return {
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
        return {"ok": False, "detail": f"HTTP {e.code}" + (" (wrong key or login?)" if e.code in (401, 403) else "")}
    except Exception as e:
        return {"ok": False, "detail": scrub(f"{type(e).__name__}: {e}")}


IMAGE_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "gif": b"GIF8", "webp": b"RIFF"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


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
        with open(path, "wb") as f:
            f.write(data)
    return {"url": "/uploads/" + name}


# ---------- HTTP ----------

if DEMO:
    import demo                            # fake data for screenshots / trying it out

API = {
    "/api/config": (0, page_config),
    "/api/services": (25, services),
    "/api/qbit": (4, qbit),
    "/api/requests": (10, requests_pending),
    "/api/plex": (5, plex),
    "/api/system": (2, system),
}
FEATURE = {"/api/qbit": lambda: QBIT, "/api/requests": lambda: SEERR, "/api/plex": lambda: TAUTULLI}
PAGES = {"/": "index.html", "/settings": "settings.html"}
apply_settings()


class Handler(BaseHTTPRequestHandler):
    server_version = UA
    sys_version = ""                      # don't advertise the Python version
    timeout = 60                          # drop idle/stuck connections so threads can't pile up

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/") or self.command == "POST":
            print(f"{self.headers.get('X-Real-IP') or self.client_address[0]} {fmt % args}", flush=True)

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
        if ip == "127.0.0.1" or ip == proxy_ip():
            return True
        print(f"blocked direct request from {ip} {self.command} {self.path[:80]}", flush=True)
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
            print(f"error on {self.command} {self.path[:80]}: {scrub(f'{type(e).__name__}: {e}')}", flush=True)
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
            self.send(200, open(os.path.join(HERE, PAGES[p]), "rb").read(), "text/html; charset=utf-8")
        elif p == "/style.css":
            self.send(200, open(os.path.join(HERE, "style.css"), "rb").read(), "text/css; charset=utf-8", "no-cache")
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
        who = self.headers.get("Remote-User") or self.headers.get("X-Real-IP") or self.client_address[0]
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
    print(f"homelab-dash listening on :{port}" + (" (DEMO mode, fake data)" if DEMO else "")
          + f" · data folder {cfgstore.DATA_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
