"""Settings for homelab-dash: what the settings page can edit, how it's validated and stored.

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

DEMO = os.environ.get("DEMO", "") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    d = os.environ.get("DATA_DIR", "/data")
    if DEMO and not os.access(d if os.path.isdir(d) else os.path.dirname(d) or ".", os.W_OK):
        d = tempfile.mkdtemp(prefix="homelab-dash-demo-")
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
                "theme": "bubbles", "themes": {}}

# Built-in themes. The look is CSS: style.css is "bubbles" (the default), every other theme lives in themes.css under
# html[data-theme="<id>"]. A theme can bring its own personality (quotes, click emoji, tab icon); what you set for it
# on the settings page wins. "bubbles" keeps using the top-level site fields, so older settings files work unchanged.
THEMES = {
    "bubbles": {"label": "Bubbles"},
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
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
        "known_ips": t.get("security", {}).get("known_ips", []),
        "qbittorrent": {"url": t.get("qbittorrent", {}).get("url", ""), "home_ip": t.get("qbittorrent", {}).get("home_ip", "")},
        "seerr": {"url": t.get("seerr", {}).get("url", "")},
        "tautulli": {"url": t.get("tautulli", {}).get("url", "")},
    }


def load():
    saved = _read_json(SETTINGS_FILE)
    return validate(saved, None) if saved else validate(_seed(), None)


def load_secrets():
    return _read_json(SECRETS_FILE) or {}


def secret(field, store):
    """(value, locked): environment wins and locks the field; otherwise the GUI-saved value."""
    env = os.environ.get(SECRET_ENV[field], "")
    return (env, True) if env else (store.get(field, ""), False)


def url_of(name, editable):
    env = os.environ.get(INTEGRATIONS[name][0], "")
    return (env.rstrip("/"), True) if env else (editable[name]["url"].rstrip("/"), False)


# ---------- validation: everything from the browser is checked before it's saved ----------

class Invalid(ValueError):
    pass


def _s(v, n, what, pattern=None):
    if v is None:
        v = ""
    if not isinstance(v, str) or len(v) > n or any(ord(c) < 32 for c in v):
        raise Invalid(f"{what}: text too long or invalid")
    v = v.strip()
    if v and pattern and not re.fullmatch(pattern, v):
        raise Invalid(f"{what}: '{v[:60]}' is not valid")
    return v


def _list(v, n, what):
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > n:
        raise Invalid(f"{what}: too many entries")
    return v


HOST = r"[A-Za-z0-9.-]{1,253}"
URL = r"https?://[^\s<>\"']{1,500}"
ICON = r"[a-z0-9][a-z0-9-]{0,80}|/(static|uploads)/[\w.-]{1,120}"


def _ip_or_prefix(v):
    v = _s(v, 64, "known IP")
    try:
        ipaddress.ip_address(v)
    except ValueError:
        if not re.fullmatch(r"(\d{1,3}\.){1,3}|[0-9a-fA-F:]{2,39}:", v):
            raise Invalid(f"known IP: '{v}' is not an IP or a prefix ending in '.'")
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


def validate(d, _old):
    if not isinstance(d, dict):
        raise Invalid("settings must be an object")
    site_in = d.get("site") or {}
    site = {"title": _s(site_in.get("title"), 60, "title") or DEFAULT_SITE["title"], **_personality(site_in, "")}
    site["click_emoji"] = site["click_emoji"] or "🫧"
    site["theme"] = site_in.get("theme") if site_in.get("theme") in THEMES else "bubbles"
    themes_in = site_in.get("themes") or {}
    if not isinstance(themes_in, dict):
        raise Invalid("themes: invalid")
    site["themes"] = {}
    for tid, t in themes_in.items():
        if tid in THEMES and tid != "bubbles" and isinstance(t, dict):   # bubbles = the top-level fields above
            t = {k: v for k, v in _personality(t, THEMES[tid]["label"] + " theme: ").items() if v}
            if t:
                site["themes"][tid] = t
    groups = []
    for g in _list(d.get("groups"), 30, "groups"):
        name = _s((g or {}).get("name"), 60, "group name")
        hosts = [_s(h, 253, "host", HOST) for h in _list(g.get("hosts"), 300, "hosts in a group")]
        if name:
            groups.append({"name": name, "hosts": list(dict.fromkeys(h for h in hosts if h))})
    apps_in = d.get("apps") or {}
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
    services = []
    for s in _list(d.get("services"), 200, "extra services"):
        s = s if isinstance(s, dict) else {}
        name, url = _s(s.get("name"), 253, "service name", HOST), _s(s.get("url"), 500, "service URL", URL)
        if name and url:
            services.append({"name": name, "url": url, "auth": bool(s.get("auth"))})
    out = {
        "site": site, "groups": groups, "apps": apps, "services": services,
        "skip_hosts": [h for h in (_s(h, 253, "hidden host", HOST) for h in _list(d.get("skip_hosts"), 500, "hidden hosts")) if h],
        "known_ips": [ip for ip in (_ip_or_prefix(v) for v in _list(d.get("known_ips"), 200, "known IPs")) if ip],
    }
    for name in INTEGRATIONS:
        sec = d.get(name) or {}
        out[name] = {"url": _s(sec.get("url"), 500, f"{name} URL", URL)}
    home = _s((d.get("qbittorrent") or {}).get("home_ip"), 64, "home IP")
    if home:
        try:
            ipaddress.ip_address(home)
        except ValueError:
            raise Invalid(f"home IP: '{home}' is not an IP address")
    out["qbittorrent"]["home_ip"] = home
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
    if not UI_ENABLED:
        raise Invalid("the settings page is read-only (SETTINGS_UI=0)")
    try:
        _write_json(SETTINGS_FILE, editable)
        _write_json(SECRETS_FILE, store)
    except PermissionError:
        raise Invalid(f"can't write to {DATA_DIR}: make the data folder writable by the container's user (uid 1000)")
