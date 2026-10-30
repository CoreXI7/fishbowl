# How Fishbowl is put together

A small stdlib-only Python server, three HTML pages (the dashboard, settings, sign-in), plain CSS and plain JavaScript files. No framework, no build step,
no dependencies. The optional root collector (`collector/`) gathers what the container can't see and writes JSON files
the server reads.

```
browser ──► reverse proxy (HTTPS) ──► app/app.py (ThreadingHTTPServer, port 8000)
                                       │  every request    the sign-in gate (auth.py; or your SSO with AUTH: proxy)
                                       │  GET /            index.html + css + js/*.js
                                       │  GET /api/...     JSON, cached per endpoint (see API in app.py)
                                       │  POST /api/...    settings, approvals (Origin-checked)
                                       ├── reads NPM's proxy_host/*.conf        -> the service list
                                       ├── reads /proc, /sys (hostinfo.py)      -> CPU, memory, temperatures, disk I/O
                                       ├── reads hw.json + sec-detail.json      <- written by the root collector
                                       └── calls qBittorrent, Seerr, Tautulli, every service upstream (upstream.py rules)
```

## Server (`app/`)

| File | What's in it |
|---|---|
| `app.py` | Entry point. Settings-dependent state (`apply_settings()`), services (NPM parsing, health checks, favicons), security data (known IPs, acks, `hw.json` / `sec-detail.json`), the integrations (qBittorrent, Tautulli/Plex, Seerr), the page and settings views, and the HTTP `Handler` (routing, proxy check, CSRF check, logging). |
| `auth.py` | Fishbowl's own sign-in: password hashing and rules, authenticator codes (TOTP) and recovery codes, the account (`data/auth.json`), sessions (`data/sessions.json`), trusted devices, the guessing limits, the setup code, one file lock shared with its command line (`python /app/auth.py status / unlock / setup-code / reset`). The gate and the sign-in API that use it are the "Fishbowl's own sign-in" part of `Handler` in `app.py`; THREAT-MODEL.md "The sign-in" lists every rule. |
| `qrcodegen.py`, `common-passwords.txt.gz` | Vendored (see `VENDORED.md`): the QR code for authenticator apps, drawn on the server; the list of common passwords that are refused. |
| `hostinfo.py` | Reading the host: per-CPU load, CPU model and topology, clocks, temperature sensors, disk I/O. No settings, no network. |
| `upstream.py` | Rules for talking to upstreams: openers that never follow redirects off-host (`NOFOLLOW`, `STRICT`, `fetch_same_host`), size caps (`read_capped`), and `num()` / `text()`, which let only the expected types through from upstream JSON. |
| `settings.py` | Loading, validating and saving settings (`data/settings.json`, `data/secrets.json`, optional `config.toml`), the built-in themes. Every field is validated server-side. |
| `demo.py` | Fake data for `DEMO=1` (the public demo, screenshots). Each function replaces the real one of the same name. |

Every GET endpoint is a plain function in the `API` table with a cache time: several open browsers (or the wall
display) never multiply the work, and `cached()` makes concurrent misses share one computation.

## Page (`app/index.html`, `app/js/`, `app/*.css`)

The HTML is the static skeleton. Each panel is one script file; they are classic scripts loaded in order and share one
global scope:

| File | Panel |
|---|---|
| `core.js` | helpers (`$`, `esc`, `setHTML`), number/time formatting, `getJSON`/`postJSON`, config, themes |
| `services.js` | service tiles and groups |
| `torrents.js`, `requests.js`, `plex.js` | qBittorrent, Seerr requests, Plex now playing |
| `system.js` | CPU/RAM, temperatures, GPU, drives |
| `security.js`, `security-detail.js` | the Security & backups card and its detail views |
| `usage.js`, `live.js` | the CPU/memory breakdown and the live window |
| `mascot.js`, `wall.js` | mascot, bubbles, confetti; wall mode |
| `start.js` | polling: what refreshes how often |
| `theme-boot.js` | runs in `<head>` so the page paints in the saved theme, and reloads a page brought back by Back (after signing out, nothing shows from the browser's cache) |
| `login.js` | the sign-in page (`login.html`): first-run setup, sign-in, the code step, the notice after signing in |
| `settings.js` | the settings page |

Rules the page code follows:

- **Every value from the API goes through `esc()`** (or a formatter that only returns digits and units) before it
  reaches HTML. The CSP (`script-src 'self'`) is the second line: even markup that slips in can't run script.
- **No inline handlers.** Buttons carry `data-*` attributes and one delegated listener per panel handles them;
  broken icons are caught by one capturing `error` listener.
- **`setHTML()` only touches the DOM when the HTML changed**, so a refresh with the same data costs no layout or paint.
- **`poll()` never overlaps a refresh with the previous one and stops while the tab is hidden.**
- **No `backdrop-filter`, and animations use `transform`/`opacity` only** (the wall display runs all day).

Looks: `style.css` is the default theme (Bubbles) and the layout; `themes.css` holds the other themes under
`html[data-theme="…"]`; `fonts.css` the self-hosted fonts (`static/fonts/`, SIL OFL).

## Root collector (`collector/`)

`homelab-dash-collector.py` runs as root under systemd (`python3 -I`, `ProtectSystem`, memory cap) and never listens
on the network. Every 5 s it writes `hw.json` (drives, GPU, power, containers, usage breakdown, the security card), and
once a minute, in its own thread, `sec-detail.json` (the detail views). Commands are argument lists, never shell
strings. The only input from the dash is `req/cron-approve`, a hash it checks against the live crontabs.

## Security model

See [THREAT-MODEL.md](THREAT-MODEL.md): what's protected, the trust boundaries, and which control holds each one.

## Tests

`python3 -m unittest discover -s tests` from the repo root (stdlib only; the page tests use headless Chrome when it's
installed and skip otherwise). Each security fix has a test that failed before it. `tests/helpers.py` loads a fresh
app with its own environment and fakes upstream services.

## Adding a panel

1. A function in `app.py` that returns the panel's data, plus an `API` entry with a cache time.
2. Its `DEMO` twin in `demo.py` (same name) so the demo and screenshots show it.
3. A `<section>` in `index.html`, a `js/<panel>.js` with `load<Panel>()` that fetches and renders with `esc()` +
   `setHTML()`, its `<script>` tag before `start.js`, and a `poll()` line in `start.js`.
