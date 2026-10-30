"""Settings for Fishbowl: what the settings page can edit, how it's validated and stored.

Layers (later wins):  config.toml (optional seed)  <  data/settings.json (saved from the GUI)  <  environment.
Secrets live in data/secrets.json (chmod 600) or in the environment; an env value is "locked" (GUI can't change it).
Security-critical infrastructure (allowed origin, trusted proxy, NPM folder) is env/config.toml only, never the GUI.
"""
import ipaddress
import json
import os
import re
import tempfile
import tomllib
import urllib.parse

DEMO = os.environ.get("DEMO", "") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    d = os.environ.get("DATA_DIR", "/data")
    if DEMO and not os.access(d if os.path.isdir(d) else os.path.dirname(d) or ".", os.W_OK):
        d = tempfile.mkdtemp(prefix="fishbowl-demo-")
    return d


DATA_DIR = _data_dir()
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CONFIG_FILE = os.environ.get("CONFIG", os.path.join(DATA_DIR, "config.toml"))
UI_ENABLED = os.environ.get("SETTINGS_UI", "1") != "0"

INTEGRATIONS = {   # name -> url env var, [(secret field, env var)]
    "qbittorrent": ("QBIT_URL", [("qbit_user", "QBIT_USER"), ("qbit_pass", "QBIT_PASS")]),
    "seerr": ("SEERR_URL", [("seerr_key", "SEERR_KEY")]),
    "tautulli": ("TAUTULLI_URL", [("tautulli_key", "TAUTULLI_KEY")]),
}
SECRET_ENV = {field: env for _, (_, fields) in INTEGRATIONS.items() for field, env in fields}

DEFAULT_SITE = {"title": "my homelab", "tagline": "everything's running ✨", "tagline_down": "uh oh, something's down!",
                "click_emoji": "🫧", "greeting": "", "quotes": [], "mascot": "", "mascot_credit": "", "favicon": "",
                "theme": "bubbles", "themes": {}, "hidden_panels": [], "motion": "auto"}
MOTION_MODES = ("auto", "on", "off")
# Panels you can switch off on the settings page (site.hidden_panels). Hiding a panel keeps its summary tile at the top.
PANELS = {"services": "Services", "system": "System", "temps": "Temperatures", "gpu": "GPU", "mascot": "Mascot",
          "plex": "Plex", "requests": "Requests", "torrents": "Torrents", "security": "Security & backups", "drives": "Drives"}

# Built-in themes. The look is CSS: style.css is "bubbles" (the default), every other theme lives in themes.css under
# html[data-theme="<id>"]. A theme can bring its own personality (quotes, click emoji, tab icon); what you set for it
# on the settings page wins. "bubbles" keeps using the top-level site fields, so older settings files work unchanged.
THEMES = {
    "bubbles": {"label": "Bubbles"},
    "miku": {"label": "Miku", "click_emoji": "🎵🥬💙🎶", "favicon": "/static/favicon-miku.svg",
             "greeting": "Miku-mode on ♪ everything's in tune!",
             "quotes": ["All services in tune 🎵", "Encore! Everything's still up 🎤", "39! Thanks for keeping me running 💙",
                        "Leek break? The servers are fine 🥬", "Turning the volume up on uptime ♪", "Mic check… 1, 2… 01!",
                        "Singing in harmony: nothing down 🎶", "Twin-tails spinning, disks spinning 💫"]},
    "gura": {"label": "Gura", "click_emoji": "🦈🔱🫧🌊🦐", "favicon": "/static/favicon-gura.svg",
             "tagline": "everything's swimming 🫧", "tagline_down": "something sank! 🌊",
             "greeting": "a! Welcome back to Atlantis, chumbud 🦈",
             "quotes": ["a", "Shaaark! Zero downtime spotted 🦈", "Shrimp check: all servers present 🦐",
                        "Atlantis status: still not sunk 🔱", "Bloop ate a bug. The software kind 🐛",
                        "I'm a smart shark. Look at this uptime 🦈", "Low on shrimp, never on uptime 🦐",
                        "Sharks never stop swimming. Neither does Plex 🌊", "chomp chomp, logs eaten 🦷",
                        "Blub blub… that means \"all good\" in shark 🫧"]},
}
THEME_FIELDS = ("mascot", "mascot_credit", "greeting", "quotes", "click_emoji", "tagline", "tagline_down", "favicon")


def _read_toml():
    path = CONFIG_FILE
    if DEMO and not os.path.isfile(path):
        path = os.path.join(HERE, "..", "config.example.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, IsADirectoryError):
        return {}


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())   # (on disk before it replaces the old one: a power cut can't leave half a file)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)          # (and the rename itself)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)   # (a write that failed halfway leaves nothing behind: failed saves can't fill the disk)
        except FileNotFoundError:
            pass             # (already renamed into place)
        raise


TOML = _read_toml()


def infra():
    """Security-critical settings: environment first, then config.toml. Never editable in the GUI."""
    site, proxy = TOML.get("site", {}), TOML.get("proxy", {})
    origins = os.environ.get("ORIGIN", site.get("origin", ""))
    return {
        "origins": [o.rstrip("/") for o in origins.split() + proxy.get("extra_origins", []) if o],
        "trusted_proxy": os.environ.get("TRUSTED_PROXY", proxy.get("trusted_proxy", "")),
        "npm_dir": os.environ.get("NPM_DIR", proxy.get("npm_dir", "/npm")),
    }


def _seed():
    """Editable settings as they come from config.toml (first run, or no GUI saves yet)."""
    t = TOML
    return {
        "site": {k: t.get("site", {}).get(k, v) for k, v in DEFAULT_SITE.items()},
        "groups": t.get("groups", []), "apps": t.get("apps", {}), "services": t.get("services", []),
        "skip_hosts": t.get("proxy", {}).get("skip_hosts", []),
        "via_proxy": t.get("proxy", {}).get("check_via_proxy", []),
        "known_ips": t.get("security", {}).get("known_ips", []),
        "qbittorrent": {"url": t.get("qbittorrent", {}).get("url", ""), "home_ip": t.get("qbittorrent", {}).get("home_ip", "")},
        "seerr": {"url": t.get("seerr", {}).get("url", "")},
        "tautulli": {"url": t.get("tautulli", {}).get("url", "")},
    }


def load():
    """The editable settings: what the GUI saved, else the config.toml seed. Always validated."""
    saved = _read_json(SETTINGS_FILE)
    return validate(saved, None) if saved else validate(_seed(), None)


def load_secrets():
    return _read_json(SECRETS_FILE) or {}


def secret(field, store):
    """(value, locked): environment wins and locks the field; otherwise the GUI-saved value."""
    env = os.environ.get(SECRET_ENV[field], "")
    return (env, True) if env else (store.get(field, ""), False)


def url_of(name, editable):
    """(url, locked) for an integration: the environment wins and locks it, else the saved URL."""
    env = os.environ.get(INTEGRATIONS[name][0], "")
    return (env.rstrip("/"), True) if env else (editable[name]["url"].rstrip("/"), False)


# ---------- validation: everything from the browser is checked before it's saved ----------

class Invalid(ValueError):
    section = None   # which part of the settings page it's about (set by validate()), so the page can point at it


def _in(section, fn, *args):
    """Run a validator; an error it raises is tagged with the settings page section it belongs to."""
    try:
        return fn(*args)
    except Invalid as e:
        e.section = e.section or section
        raise


def _s(v, n, what, pattern=None):
    """A text field: at most n characters, no control characters, trimmed, and matching pattern if given."""
    if v is None:
        v = ""
    if not isinstance(v, str) or len(v) > n or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in v):
        raise Invalid(f"{what}: text too long or invalid")   # (a lone surrogate, which JSON allows, can't be saved)
    v = v.strip()
    if v and pattern and not re.fullmatch(pattern, v):
        raise Invalid(f"{what}: '{v[:60]}' is not valid")
    return v


def _list(v, n, what):
    """A list field with at most n entries (missing = empty)."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise Invalid(f"{what}: must be a list")
    if len(v) > n:
        raise Invalid(f"{what}: too many entries")
    return v


def _obj(v, what):
    """A section that must be an object (missing = empty). Anything else is refused with a clear message
    instead of crashing further down."""
    if not v:                         # missing or empty ("", [], null) = not set
        return {}
    if not isinstance(v, dict):
        raise Invalid(f"{what}: must be an object")
    return v


HOST = r"(?!(__proto__|constructor|prototype)$)(\*\.)?[A-Za-z0-9_.-]{1,253}"   # NPM allows "_" and "*." (wildcards); never a JS built-in name
URL = r"https?://[^\s<>\"']{1,500}"
# an integration's URL: a host and maybe a path, no query, fragment or user (urllib drops what follows #, so
# "http://camera/snap.jpg#" made the poster proxy return the camera's picture)
INTEGRATION_URL = r"https?://[^\s<>\"'?#@]{1,500}"
ICON = r"[a-z0-9][a-z0-9-]{0,80}|/(static|uploads)/[\w.-]{1,120}"


def _ip_or_prefix(v):
    """A known IP: a full address, or a prefix like "203.0.113." / "2001:db8:" that matches a whole range."""
    v = _s(v, 64, "known IP")
    try:
        ipaddress.ip_address(v)
    except ValueError:
        if not re.fullmatch(r"(\d{1,3}\.){1,3}|[0-9a-fA-F:]{2,39}:", v):
            raise Invalid(f"known IP: '{v}' isn't an IP address or a prefix ending in a dot or a colon")
    return v


def _personality(src, where):
    """The fields a theme can override: mascot, what it says, click emoji, taglines, tab icon."""
    return {
        "tagline": _s(src.get("tagline"), 120, where + "tagline"),
        "tagline_down": _s(src.get("tagline_down"), 120, where + "tagline when down"),
        "click_emoji": _s(src.get("click_emoji"), 16, where + "click emoji"),
        "greeting": _s(src.get("greeting"), 200, where + "greeting"),
        "quotes": [q for q in (_s(q, 200, where + "quote") for q in _list(src.get("quotes"), 50, where + "quotes")) if q],
        "mascot": _s(src.get("mascot"), 200, where + "mascot", r"/(static|uploads)/[\w.-]{1,120}"),
        "mascot_credit": _s(src.get("mascot_credit"), 80, where + "mascot credit"),
        "favicon": _s(src.get("favicon"), 200, where + "tab icon", r"/(static|uploads)/[\w.-]{1,120}"),
    }


def _site(site_in):
    """Title, the default theme, and the personality fields (top level = Bubbles; other themes under "themes")."""
    site = {"title": _s(site_in.get("title"), 60, "title") or DEFAULT_SITE["title"], **_in("themes", _personality, site_in, "")}
    site["click_emoji"] = site["click_emoji"] or "🫧"
    theme = site_in.get("theme")
    site["theme"] = theme if isinstance(theme, str) and theme in THEMES else "bubbles"
    hidden = (_s(p, 20, "hidden panel") for p in _list(site_in.get("hidden_panels"), 20, "hidden panels"))
    site["hidden_panels"] = [p for p in dict.fromkeys(hidden) if p in PANELS]   # unknown names (older/newer versions): dropped
    # background motion for browsers that haven't chosen with 🫧: "auto" follows the device's reduce-motion setting
    motion = site_in.get("motion")
    site["motion"] = motion if motion in MOTION_MODES else "auto"
    themes_in = site_in.get("themes") or {}
    if not isinstance(themes_in, dict):
        raise Invalid("themes: invalid")
    site["themes"] = {}
    for tid, t in themes_in.items():
        if tid in THEMES and tid != "bubbles" and isinstance(t, dict):   # bubbles = the top-level fields above
            t = {k: v for k, v in _in("themes", _personality, t, THEMES[tid]["label"] + " theme: ").items() if v}
            if t:                                                        # keep only what was actually set
                site["themes"][tid] = t
    return site


def _groups(groups_in):
    """Named service groups, each a list of hostnames (duplicates dropped, order kept)."""
    groups = []
    for g in _list(groups_in, 30, "groups"):
        g = _obj(g, "group")
        name = _s(g.get("name"), 60, "group name")
        hosts = [_s(h, 253, "host", HOST) for h in _list(g.get("hosts"), 300, "hosts in a group")]
        if name:
            groups.append({"name": name, "hosts": list(dict.fromkeys(h for h in hosts if h))})
    return groups


def _apps(apps_in):
    """Per-host display overrides: label, icon (dashboard-icons name or an uploaded file) and link."""
    apps_in = apps_in or {}
    if not isinstance(apps_in, dict) or len(apps_in) > 1000:
        raise Invalid("apps: too many entries")
    apps = {}
    for host, a in apps_in.items():
        host = _s(host, 253, "host", HOST)
        a = a if isinstance(a, dict) else {}
        entry = {k: v for k, v in {"label": _s(a.get("label"), 60, f"label for {host}"),
                                   "icon": _s(a.get("icon"), 140, f"icon for {host}", ICON),
                                   "url": _s(a.get("url"), 500, f"link for {host}", URL)}.items() if v}
        if host and entry:
            apps[host] = entry
    return apps


def _services(services_in):
    """Extra services that aren't NPM proxy hosts: name + URL to check (+ whether it's behind SSO)."""
    services = []
    for s in _list(services_in, 200, "extra services"):
        s = s if isinstance(s, dict) else {}
        name, url = _s(s.get("name"), 253, "service name", HOST), _s(s.get("url"), 500, "service URL", URL)
        if name and url:
            services.append({"name": name, "url": url, "auth": bool(s.get("auth"))})
    return services


VIA = r"[A-Za-z0-9.-]{1,253}(/[A-Za-z0-9._~%+,;=:@/-]{0,200})?"


def _via_proxy(entries):
    """Hosts checked through the reverse proxy instead of directly: "host" or "host/path" (path instead of /)."""
    out = []
    for e in _list(entries, 100, "check through the proxy"):
        e = _s(e, 460, "check through the proxy", VIA)
        path = e.partition("/")[2]
        if ".." in path or "%2e" in path.lower():
            raise Invalid(f"check through the proxy: '{e[:60]}': a path can't contain '..'")
        if e:
            out.append(e)
    return list(dict.fromkeys(out))


def _home_ip(qbit_in):
    """Your real WAN IP: qBittorrent reporting it means the VPN isn't in use."""
    home = _s(_obj(qbit_in, "qbittorrent").get("home_ip"), 64, "home IP")
    if home:
        try:
            ipaddress.ip_address(home)
        except ValueError:
            raise Invalid(f"home IP: '{home}' is not an IP address")
    return home


def _integration_url(v, name, loading):
    """An integration's URL (INTEGRATION_URL). A saved file from before that rule (loading) has its query, fragment and
    user dropped instead of stopping the dashboard from starting; one that can't be read that way (a password with a #
    in it reads as a port) is dropped: the Setup check then says the integration isn't connected."""
    if loading and isinstance(v, str) and re.search(r"[?#@]", v):
        try:
            u = urllib.parse.urlsplit(v.strip())
            u.port                                              # (raises for a port that isn't a number)
            v = f"{u.scheme}://{u.netloc.rpartition('@')[2]}{u.path}" if u.scheme in ("http", "https") else ""
        except ValueError:
            v = ""
    return _s(v, 500, f"{name} URL", INTEGRATION_URL)


def validate(d, _old):
    """Check and normalise a whole settings object (from the browser, settings.json or config.toml).
    Returns a clean copy; raises Invalid with a message the settings page can show. (_old is unused, kept for callers.)"""
    if not isinstance(d, dict):
        raise Invalid("settings must be an object")
    out = {
        "site": _in("look", _site, _obj(d.get("site"), "site")),
        "groups": _in("services", _groups, d.get("groups")),
        "apps": _in("services", _apps, d.get("apps")),
        "services": _in("services", _services, d.get("services")),
        "skip_hosts": _in("services", lambda v: [h for h in (_s(h, 253, "hidden host", HOST) for h in _list(v, 500, "hidden hosts")) if h],
                          d.get("skip_hosts")),
        "via_proxy": _in("services", _via_proxy, d.get("via_proxy")),
        "known_ips": _in("security", lambda v: [ip for ip in (_ip_or_prefix(x) for x in _list(v, 200, "known IPs")) if ip], d.get("known_ips")),
    }
    for name in INTEGRATIONS:
        out[name] = {"url": _in("integrations", _integration_url, _obj(d.get(name), name).get("url"), name, _old is None)}
    out["qbittorrent"]["home_ip"] = _in("integrations", _home_ip, d.get("qbittorrent"))
    return out


def apply_secret_changes(new, old_editable, changes, store):
    """Merge secret edits. Rule: changing an integration's URL requires re-entering its keys in the same save,
    so someone who reaches the settings page can't redirect a stored key to their own server."""
    store = dict(store)
    for name, (url_env, fields) in INTEGRATIONS.items():
        if os.environ.get(url_env):
            new[name]["url"] = old_editable[name]["url"]          # URL is locked by the environment
        url_changed = new[name]["url"].rstrip("/") != old_editable[name]["url"].rstrip("/")
        for field, env in fields:
            if os.environ.get(env):
                if url_changed:
                    raise Invalid(f"{name}: its key is set in the environment, so its URL can only be changed there too")
                continue
            c = changes.get(field)
            if c is not None and not isinstance(c, str):
                raise Invalid(f"{field}: invalid")
            if c:                                                     # new value typed in
                store[field] = _s(c, 500, field)
            elif changes.get(field + "_clear"):
                store.pop(field, None)
            elif url_changed and store.get(field):
                raise Invalid(f"{name}: you changed its URL, so please type its "
                              f"{'login' if name == 'qbittorrent' else 'API key'} again (keys are never sent to a new address unconfirmed)")
    return store


def save(editable, store):
    """Write secrets.json and settings.json (both chmod 600, written atomically). Secrets first: if the second write
    fails (a full disk), an old URL with a newly typed key is harmless; a new URL with the old key is what
    apply_secret_changes() exists to stop."""
    if not UI_ENABLED:
        raise Invalid("the settings page is read-only (SETTINGS_UI=0)")
    try:
        _write_json(SECRETS_FILE, store)
        _write_json(SETTINGS_FILE, editable)
    except PermissionError:
        raise Invalid(f"can't write to {DATA_DIR}: make the data folder writable by the container's user (uid 1000)")
