# Installing Fishbowl

The full version of the README's quick start: every step, the collector, and the keys and buttons.

## What you need

- A Linux server with Docker and Docker Compose
- A reverse proxy that gives the dashboard an HTTPS address (any one you already use is fine)
- Optional, for the collector: Python 3.11 or newer, systemd and `smartctl` on the server

## Install

### 1. Download Fishbowl

```sh
git clone https://github.com/CoreXI7/fishbowl
cd fishbowl
```

This gets the compose file and the optional collector. The dashboard itself runs from the image.

### 2. Create the data folder

Fishbowl keeps everything in `data/`: settings, and the API keys you type on the settings page (in `data/secrets.json`,
mode 600). No other secrets file is needed.

```sh
mkdir data
sudo chown 1000:1000 data
```

**Another user than 1000?** Fishbowl runs as `PUID:PGID` (1000:1000 by default). To use your own user, put its ids in a
`.env` next to `docker-compose.yml`, give it the data folder, and tell the collector (step 6) the same uid:

```sh
printf 'PUID=%s\nPGID=%s\n' "$(id -u myuser)" "$(id -g myuser)" > .env && chmod 600 .env
sudo chown "$(id -u myuser):$(id -g myuser)" data
```

The Setup page checks both and tells you the exact fix if they don't match.

**Docker Compose older than 2.24?** It doesn't know the optional `env_file` form and refuses the file. Replace the
`env_file:` block in `docker-compose.yml` with the single line `env_file: .env` and create an empty one: `touch .env`.
(`docker compose version` shows yours.)

**Prefer keys in a file?** `.env` can also hold the keys from `.env.example` instead of the settings page; a key set there
wins and is locked on the page.

### 3. Edit `docker-compose.yml`

Set `ORIGIN` to the address you'll open the dashboard at, for example `https://dash.example.com`. The rest depends on
two things about your setup.

**Where does your reverse proxy run?**

| Your proxy | `TRUSTED_PROXY` | How the proxy reaches Fishbowl |
|---|---|---|
| A Docker container on this server (Nginx Proxy Manager, Caddy, Traefik, ...) | its container name (default `nginx`) | Keep `networks:` and set it to the proxy's Docker network (default `reverse_proxy`). |
| Another machine, an LXC or VM, or installed on this server without Docker | its IP address, e.g. `10.0.0.50` (on this same server: this server's own LAN IP) | Delete both `networks:` parts and add `ports: ["10.0.0.10:8000:8000"]` with this server's LAN address (a bare `8000:8000` would listen on every interface). |

Only the address in `TRUSTED_PROXY` can connect, so get this one right. Several names or IPs can be listed, separated by
spaces. Not sure which address your proxy connects from? Open the dashboard through it once, then
`docker logs dash 2>&1 | grep "blocked direct connection"` shows the address that was turned away.

**Where do your services come from?**

| Your services are in | What to do |
|---|---|
| Nginx Proxy Manager | Keep the `proxy_host` volume and point it at NPM's `data/nginx/proxy_host` folder. |
| Plain Nginx config files | Delete the `proxy_host` line, uncomment the `/etc/nginx:/nginx:ro` volume and `NGINX_DIR: /nginx`. |
| A Caddyfile | Delete the `proxy_host` line, uncomment the `/etc/caddy:/caddy:ro` volume and `CADDYFILE: /caddy/Caddyfile`. |
| None of these, or the proxy is on another machine | Delete the `proxy_host` line and add your services on the settings page (step 7). |

The config files are only read, never changed. If your proxy is on another machine, you can copy or mount its config
folder onto this server and point the volume there, or just add the services by hand.

### 4. Start it

```sh
docker compose up -d
```

### 5. Connect your reverse proxy

Send your chosen address (for example `dash.example.com`) to Fishbowl: `dash:8000` if the proxy shares its Docker
network, otherwise `http://<this server's LAN address>:8000`.

The proxy must also tell Fishbowl each visitor's real IP and whether they came over HTTPS, and overwrite whatever the
visitor sent in those headers. For plain Nginx, add these two lines to the dashboard's `location`:

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
```

Nginx Proxy Manager, Caddy and the others need slightly different lines: see
[docs/SETTINGS.md](SETTINGS.md#signing-in-auth-login-the-default). The Setup page (step 7) shows which address Fishbowl
sees for your proxy and tells you if a header is missing.

### 6. Create your account

Open your dashboard's address. The first time, it asks for a one-time setup code. Get it from the container's log:

```sh
docker logs dash 2>&1 | grep "^SETUP CODE" | tail -n 1
```

Then choose a name and a password of at least 15 characters (a few random words work well).

Already have a login page in front of your services, like Authelia or Authentik? Then you can turn Fishbowl's own
sign-in off: add `AUTH: proxy` under `environment:` in `docker-compose.yml` and run `docker compose up -d` again.

### 7. Finish setup in the browser

Click the **⚙** button and open **Setup**. It checks your install and tells you how to fix anything that's missing.
Then connect your apps under **Integrations**: qBittorrent or Transmission, SABnzbd, Seerr and Tautulli each have a **Test** button.

**Adding services by hand** (Settings → Services → **Services added by hand**): click **+ Add service** once for each
service and fill in its name and the address to check. Typing a new name into an existing row renames that service, it
doesn't add a second one. Then put each one in a group in the list above. **Delete** removes a hand-added service;
services found from your proxy's config can only be hidden, because they'd come back on the next scan.

### Optional: install the collector

The collector is a small helper that runs directly on your server, outside Docker. It reads things the dashboard
can't see from inside its container: drives, the GPU, power use, and security logs.

```sh
sudo bash collector/install.sh
sudo nano /etc/homelab-dash/collector.toml
sudo systemctl restart homelab-dash-collector
```

In `collector.toml`, list your drives and, if you use them, your fail2ban jail and backup log. To remove the
collector: `sudo bash collector/install.sh --uninstall`.

#### How the collector works

Some of what Fishbowl shows can only be read as root: SMART data from your drives, the firewall and fail2ban, the
SSH login log, scheduled jobs, and backup logs. Instead of giving the dashboard those powers, a separate program
does that part:

1. **It runs on the host as a systemd service,** as root, once every few seconds.
2. **It writes what it found to files** in `/var/lib/homelab-dash/`.
3. **The dashboard only reads those files.** They're mounted into its container read-only.

The collector never connects to the network. The installer tests it under three systemd sandboxes and keeps the
strictest one it still works under: no network at all, a read-only system apart from its own folder, and only the
root permissions it needs.

You can change most of its settings on the dashboard's settings page. Simple switches, like which drives to show,
apply by themselves. Anything that would make root read a file or run a search pattern, like a new log file to
watch, waits until you approve it on the server:

```sh
sudo homelab-dash-collector --apply
```

It shows exactly what will change and asks first.

## Using it

- **Settings:** click ⚙ (or go to `/settings`). Everything from your services to themes to your account is set there,
  and changes apply right away.
- **Two-step sign-in:** go to ⚙ → **Account** to add an authenticator app. Keep the recovery codes it gives you.
- **Locked out or forgot your password:** run `docker exec -it dash python /app/auth.py reset` on the server. It
  removes the account (your settings stay) and prints a new setup code.

Every setting is explained in [docs/SETTINGS.md](SETTINGS.md).

### Wall mode and buttons

![Wall mode, dark](screenshot-wall-dark.png)

Wall mode is made for a TV or spare monitor that stays on: everything fits one 1920×1080 screen, it stays calm while all
is well and stands out when something needs you. Turn it on with `W`, the 🖥 button, or `?wall` at the end of the address
(for a screen that opens the dashboard at startup). The cursor and buttons hide after 3 seconds without movement, and
the page reloads itself every 6 hours to keep the browser light.

| Key | Button | What it does |
|---|---|---|
| `W` | 🖥 (top right) | Wall mode on or off |
| `T` | 🎨 | Next theme, remembered by this browser |
| `M` | 🫧 | Background animation on or off, remembered by this browser |
| | ⚙ | Settings (`Ctrl+S` saves there) |
| | **live** on Temperatures | Live view: temperatures, power, clocks, disks and every CPU thread, updated every second |
| | **⚡ breakdown** on System | What's using the CPU and memory, by container and program |
| | A title in Security & backups | The details behind that check |
| | The Plex tile | Gives Now Playing the whole row |
| | **Downloading** / **Seeding** | Switches the torrent list |
| `Esc` | ✕ | Closes the live view, the breakdown or a details view |

## Updating

```sh
cd fishbowl
git pull
docker compose pull
docker compose up -d
```

If you use the collector, update it too: `sudo bash collector/install.sh`.

Tip: if you changed `docker-compose.yml`, `git pull` may refuse to update it. Putting your changes in a file called
`docker-compose.override.yml` instead avoids that.
