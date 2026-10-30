"""Fake data for DEMO=1: try the dashboard (or take screenshots) without any real services.

Titles are Blender open movies (CC-BY), hostnames use example.com, IPs are documentation ranges.
"""
import base64
import math
import random
import time

T0 = time.time()

SERVICES = [
    ("plex.example.com", False), ("requests.example.com", False), ("tautulli.example.com", True),
    ("jellyfin.example.com", False), ("immich.example.com", True), ("komga.example.com", True),
    ("sonarr.example.com", True), ("radarr.example.com", True), ("prowlarr.example.com", True),
    ("bazarr.example.com", True), ("qbit.example.com", True),
    ("vault.example.com", False), ("paperless.example.com", True), ("nextcloud.example.com", False),
    ("home.example.com", True), ("files.example.com", True),
    ("proxy.example.com", True), ("auth.example.com", False), ("cockpit.example.com", True),
    ("adguard.example.com", True), ("portainer.example.com", True), ("uptime.example.com", True),
    ("example.com", False), ("blog.example.com", False), ("new-thing.example.com", True),
]


def poster(title, hue):
    """A little gradient poster as an SVG data URI (no external images needed)."""
    words = title.split()
    lines = "".join(f'<text x="50" y="{120 + i * 22}" font-size="17" text-anchor="middle" fill="#fff" '
                    f'font-family="sans-serif" font-weight="700">{w}</text>' for i, w in enumerate(words[:3]))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 150"><defs><linearGradient id="g" x2="0" y2="1">'
           f'<stop offset="0" stop-color="hsl({hue},70%,55%)"/><stop offset="1" stop-color="hsl({(hue + 40) % 360},60%,25%)"/>'
           f'</linearGradient></defs><rect width="100" height="150" fill="url(#g)"/>'
           f'<circle cx="50" cy="60" r="26" fill="#fff" opacity=".18"/>{lines}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def wobble(base, amp, period=60):
    return max(0, base + amp * math.sin((time.time() - T0) / period * 2 * math.pi) + random.uniform(-amp / 4, amp / 4))


def services():
    out = []
    for name, auth in SERVICES:
        down = name == "uptime.example.com" and int(time.time() / 120) % 3 == 0   # flaps now and then
        out.append({"name": name, "auth": auth, "up": not down, "code": None if down else 200,
                    "ms": None if down else random.randint(2, 40), "err": "ConnectionRefusedError" if down else None})
    return {"checked": int(time.time()), "services": out}


def qbit():
    dl = [
        {"name": "Big.Buck.Bunny.2008.2160p.mkv", "state": "downloading", "progress": (time.time() / 600) % 1,
         "dlspeed": int(wobble(18e6, 6e6)), "upspeed": 0, "eta": 420, "size": 8.4e9, "num_seeds": 87, "num_leechs": 12,
         "ratio": 0.0, "category": "radarr"},
        {"name": "Sintel.2010.1080p.mkv", "state": "stalledDL", "progress": 0.42, "dlspeed": 0, "upspeed": 0,
         "eta": 8640000, "size": 2.1e9, "num_seeds": 0, "num_leechs": 3, "ratio": 0.0, "category": "radarr"},
    ]
    seed = [{"name": n, "state": "uploading", "progress": 1, "dlspeed": 0, "upspeed": int(wobble(s, s / 3)), "eta": 0,
             "size": z, "num_seeds": 40, "num_leechs": p, "ratio": r, "category": c}
            for n, s, z, p, r, c in [("Tears.of.Steel.2012.4K.mkv", 2.4e6, 6.3e9, 5, 3.1, "radarr"),
                                     ("Elephants.Dream.2006.1080p.mkv", 9e5, 1.2e9, 2, 7.8, "radarr"),
                                     ("Spring.2019.1080p.mkv", 3e5, 9e8, 1, 1.4, "radarr")]]
    return {"connection": "connected", "external_ip": "203.0.113.42", "vpn_check": True, "vpn_leak": False,
            "dl_speed": sum(t["dlspeed"] for t in dl), "up_speed": sum(t["upspeed"] for t in seed),
            "dl_total": 2.9e12, "up_total": 11.4e12, "total": 412,
            "downloading": dl, "seeding": seed, "seeding_active": len(seed)}


def requests_pending():
    now = time.time()
    iso = lambda h: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - h * 3600))
    return {"pending": [
        {"id": 101, "type": "movie", "is4k": False, "created": iso(3), "user": "alex", "seasons": [],
         "title": "Cosmos Laundromat", "year": "2015", "poster": poster("Cosmos Laundromat", 280),
         "overview": "On a desolate island, a suicidal sheep meets a mysterious salesman who offers him the gift of a lifetime."},
        {"id": 102, "type": "tv", "is4k": False, "created": iso(20), "user": "sam", "seasons": [1, 2],
         "title": "Agent 327", "year": "2017", "poster": poster("Agent 327", 20),
         "overview": "Agent 327 is investigating a clue that leads him to a shady barbershop in Amsterdam."},
    ]}


def plex():
    p1 = ((time.time() - T0) / 5400 * 100 + 31) % 100
    p2 = ((time.time() - T0) / 888 * 100 + 64) % 100
    sessions = [
        {"session_key": "1", "friendly_name": "alex", "full_title": "Sintel", "title": "Sintel", "year": 2010,
         "media_type": "movie", "state": "playing", "progress_percent": round(p1), "transcode_decision": "transcode",
         "video_decision": "transcode", "stream_video_full_resolution": "1080p", "bandwidth": 8200, "product": "Plex for Android",
         "player": "Living Room TV", "location": "wan", "transcode_hw_encoding": 1, "duration": 888000,
         "view_offset": 888000 * p1 / 100, "art": poster("Sintel", 200)},
        {"session_key": "2", "friendly_name": "sam", "full_title": "Charge - Episode 1", "title": "Pilot",
         "grandparent_title": "Charge", "parent_media_index": 1, "media_index": 1, "media_type": "episode",
         "state": "paused", "progress_percent": round(p2), "transcode_decision": "direct play", "video_decision": "direct play",
         "stream_video_full_resolution": "4k", "bandwidth": 21500, "product": "Plex Web", "player": "Firefox",
         "location": "lan", "duration": 2400000, "view_offset": 2400000 * p2 / 100, "art": poster("Charge", 140)},
    ]
    return {"stream_count": 2, "transcodes": 1, "bandwidth": 29700, "sessions": sessions}


def usage():
    """Fake 'what is using the CPU' data that moves a little each time."""
    G = [("plex", "container", 9, 1.9e9, [("Plex Transcoder", 7), ("Plex Media Serv", 1.6)]),
         ("qbittorrent", "container", 2.2, 3.1e9, [("qbittorrent-nox", 2.1)]),
         ("jellyfin", "container", 1.1, 1.2e9, [("jellyfin", 1)]), ("immich", "container", 1.6, 2.4e9, [("immich", 1.2), ("node", .3)]),
         ("sonarr", "container", .4, 4.1e8, [("Sonarr", .4)]), ("radarr", "container", .3, 3.8e8, [("Radarr", .3)]),
         ("homeassistant", "container", .8, 6.2e8, [("python3", .7)]), ("nextcloud", "container", .5, 7.4e8, [("php-fpm", .4)]),
         ("nginx-proxy-manager", "container", .2, 2.1e8, [("nginx", .2)]), ("authelia", "container", .05, 6e7, [("authelia", .05)]),
         ("paperless", "container", .3, 9.8e8, [("celery", .2)]), ("vaultwarden", "container", .02, 4e7, [("vaultwarden", .02)]),
         ("dockerd", "process", .6, 1.5e8, []), ("containerd", "process", .3, 9e7, []), ("sshd", "process", .01, 1.2e7, []),
         ("kernel threads", "kernel", .7, 0, [])]
    out = []
    for name, kind, cpu, mem, top in G:
        f = max(0.15, 1 + random.uniform(-.35, .35))
        out.append({"name": name, "kind": kind, "cpu": round(cpu * f * (1.4 if name == "plex" and int(time.time() / 20) % 2 else 1), 2),
                    "mem": int(mem * (1 + random.uniform(-.02, .02))), "procs": random.randint(1, 40) if kind != "kernel" else 310,
                    "top": [{"name": n, "cpu": round(c * f, 2)} for n, c in top]})
    out.sort(key=lambda g: -g["cpu"])
    return {"ncpu": 16, "idle": round(100 - sum(g["cpu"] for g in out), 2), "groups": out}


def system():
    cores = [round(wobble(12, 10, 20 + i), 1) for i in range(16)]
    cpu = round(sum(cores) / len(cores), 1)
    TB = 1e12
    disks = [{"mount": "/", "name": "system", "dev": "/dev/nvme0n1", "model": "Samsung SSD 990 PRO 1TB", "system": True,
              "total": 1 * TB, "used": 0.31 * TB, "free": 0.64 * TB}]
    for i, (size, used, model) in enumerate([(18, 15.9, "WDC WD180EDGZ"), (18, 14.2, "WDC WD180EDGZ"),
                                             (12, 11.6, "ST12000VN0008"), (20, 6.1, "ST20000NM007D")], 1):
        disks.append({"mount": f"/mnt/disk{i}", "name": f"disk{i}", "dev": f"/dev/sd{'abcd'[i - 1]}", "model": model,
                      "total": size * TB, "used": used * TB, "free": (size - used) * TB})
    smart = {"/dev/nvme0n1": {"temp": 41, "passed": True, "hours": 9100, "realloc": 0, "wear": 3},
             "/dev/sda": {"temp": 36, "passed": True, "hours": 21000, "realloc": 0, "pending": 0},
             "/dev/sdb": {"temp": 37, "passed": True, "hours": 21000, "realloc": 0, "pending": 0},
             "/dev/sdc": {"temp": 39, "passed": True, "hours": 33000, "realloc": 8, "pending": 0},
             "/dev/sdd": {"standby": True}}
    now = int(time.time())
    hw = {
        "updated": now, "stale": False, "disks": disks, "smart": smart,
        "gpu": {"name": "NVIDIA GeForce GTX 1660", "temp": 52, "util": round(wobble(18, 8)), "enc": round(wobble(14, 6)),
                "dec": round(wobble(10, 5)), "mem_used": 640, "mem_total": 6144, "power": 38, "fan": 35, "sessions": 1},
        "containers": {"running": 31, "total": 33},
        "usage": usage(),
        "net": {"if": "eth0", "rx_bps": wobble(3.5e6, 1.5e6), "tx_bps": wobble(1.4e6, 6e5)},
        "security": {
            "f2b": {"ok": True, "jail": "sshd", "banned_now": 3, "banned_total": 57, "ips": ["198.51.100.23", "198.51.100.77", "192.0.2.199"]},
            "ssh": {"failed_24h": 212, "logins": [
                {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 5400)), "method": "publickey", "user": "admin", "ip": "192.168.1.20", "known": True},
                {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 90000)), "method": "publickey", "user": "admin", "ip": "192.168.1.20", "known": True}]},
            "authelia": {"failed_24h": 0, "recent": []},
            "procs": {"found": [], "top": [{"pid": 1234, "cpu": 21.0, "name": "Plex Transcoder"},
                                           {"pid": 999, "cpu": 4.0, "name": "qbittorrent-nox"},
                                           {"pid": 77, "cpu": 1.0, "name": "dockerd"}]},
            "cron": {"ok": True, "hash": "0" * 64, "approved_at": now - 3 * 86400, "files": 6, "changes": []},
            "firewall": {"name": "UFW", "status": "active"},
        },
        "backups": {"ok": True, "last_run": time.strftime("%Y-%m-%d 03:00:01"), "log": ["03:00:01 backup started", "03:14:52 backup ok"],
                    "artifacts": [{"label": "Nightly snapshot", "count": 14, "newest": now - 6 * 3600, "size": 3.2e9, "fresh": True},
                                  {"label": "DB dumps", "count": 7, "newest": now - 6 * 3600, "size": 4.1e7, "fresh": True}]},
    }
    return {"cpu": cpu, "cores": cores, "cpu_model": "AMD Ryzen 7 5700G", "load": [1.21, 1.05, 0.98],
            "uptime": 23 * 86400 + 5 * 3600, "mem_total": 32 * 2**30, "mem_avail": 19.5 * 2**30,
            "swap_total": 0, "swap_free": 0,
            "temps": {"cpu": wobble(54, 4), "cells": [{"label": "CPU", "c": wobble(54, 4), "warn": 80, "crit": 90},
                                                      {"label": "NVMe", "c": 41, "warn": 65, "crit": 75},
                                                      {"label": "RAM 1", "c": 38, "warn": 70, "crit": 80},
                                                      {"label": "RAM 2", "c": 39, "warn": 70, "crit": 80}]},
            "hw": hw}


def security_detail():
    """What the Security & backups detail views show (the real one comes from the collector's sec-detail.json)."""
    now = int(time.time())
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
    day = lambda h, m=0: now - now % 86400 + h * 3600 + m * 60 - (86400 if now % 86400 < 3 * 3600 else 0)
    run = day(3)
    bans = ["198.51.100.23", "198.51.100.77", "192.0.2.199"]
    return {
        "updated": now - 20, "stale": False,
        "firewall": {"name": "UFW", "status": "active", "logging": "on (low)", "checked": now - 20,
                     "default": "deny (incoming), allow (outgoing), deny (routed)",
                     "rules": [{"to": "443/tcp", "action": "ALLOW IN", "from": "Anywhere", "note": "web"},
                               {"to": "22/tcp", "action": "ALLOW IN", "from": "Anywhere", "note": "ssh"},
                               {"to": "Anywhere", "action": "ALLOW IN", "from": "192.168.1.0/24", "note": "LAN"},
                               {"to": "Anywhere", "action": "DENY IN", "from": "203.0.113.66", "note": "blocked"},
                               {"to": "443/tcp (v6)", "action": "ALLOW IN", "from": "Anywhere (v6)", "note": ""}]},
        "fail2ban": {"checked": now - 20,
                     "jails": [{"jail": "sshd", "failed_now": 2, "failed_total": 230, "banned_now": 3, "banned_total": 57, "ips": bans}],
                     "events": [{"time": now - 1800 * (i + 1), "jail": "sshd", "action": "Ban" if i % 3 else "Unban", "ip": bans[i % 3]}
                                for i in range(8)],
                     "found_7d": [{"ip": "198.51.100.23", "jail": "sshd", "count": 41, "last": now - 1900},
                                  {"ip": "192.0.2.199", "jail": "sshd", "count": 12, "last": now - 7200}]},
        "ssh": {"checked": now - 20, "attempts_24h": 212, "attempts_7d": 1350, "probes_7d": 96, "failed_ip_count": 3, "login_count": 41,
                "sources": [{"user": "admin", "ip": "192.168.1.20", "key": "admin@laptop", "method": "publickey", "count": 38,
                             "first": now - 6 * 86400, "last": now - 5400, "known": True},
                            {"user": "admin", "ip": "203.0.113.45", "key": "admin-phone", "method": "publickey", "count": 3,
                             "first": now - 260000, "last": now - 250000, "known": False}],
                "logins": [{"time": now - 5400, "raw": iso(now - 5400), "user": "admin", "ip": "192.168.1.20", "method": "publickey", "key": "admin@laptop", "known": True},
                           {"time": now - 90000, "raw": iso(now - 90000), "user": "admin", "ip": "192.168.1.20", "method": "publickey", "key": "admin@laptop", "known": True},
                           {"time": now - 250000, "raw": iso(now - 250000), "user": "admin", "ip": "203.0.113.45", "method": "publickey", "key": "admin-phone",
                            "known": False, "acked": True}],
                "failed_ips": [{"ip": "198.51.100.23", "attempts": 820, "attempts_24h": 130, "probes": 12, "users": ["root", "admin", "ubuntu"],
                                "first": now - 6 * 86400, "last": now - 1900, "known": False},
                               {"ip": "198.51.100.77", "attempts": 410, "attempts_24h": 70, "probes": 4, "users": ["root", "test"],
                                "first": now - 4 * 86400, "last": now - 600, "known": False},
                               {"ip": "192.0.2.199", "attempts": 120, "attempts_24h": 12, "probes": 80, "users": ["oracle"],
                                "first": now - 2 * 86400, "last": now - 7200, "known": False}]},
        "authelia": {"checked": now - 20, "ok_24h": 3, "fail_24h": 1, "ok_7d": 19, "fail_7d": 2,
                     "events": [{"time": now - 3600, "ok": True, "banned": False, "user": "admin", "factor": "2FA", "ip": "192.168.1.20",
                                 "site": "dash.example.com", "known": True},
                                {"time": now - 3700, "ok": True, "banned": False, "user": "admin", "factor": "1FA", "ip": "192.168.1.20",
                                 "site": "dash.example.com", "known": True},
                                {"time": now - 40000, "ok": False, "banned": False, "user": "admin", "factor": "1FA", "ip": "203.0.113.9",
                                 "site": "auth.example.com", "known": False}],
                     "fail_by_user": [{"key": "admin", "count": 2, "last": now - 40000}],
                     "fail_by_ip": [{"key": "203.0.113.9", "count": 2, "last": now - 40000, "known": False}], "bans": []},
        "procs": {"checked": now - 20, "names": ["xmrig", "minerd", "kinsing"], "found": [],
                  "top": [{"pid": 1234, "user": "plex", "cpu": 21.0, "mem": 2.1, "name": "Plex Transcoder"},
                          {"pid": 999, "user": "abc", "cpu": 4.0, "mem": 1.2, "name": "qbittorrent-nox"},
                          {"pid": 77, "user": "root", "cpu": 1.0, "mem": 0.4, "name": "dockerd"}],
                  "watcher": {"label": "miner-watch.sh (every 5 min)", "last_run": now - 140, "every_minutes": 5, "late": False,
                              "checks": ["known miner names (killed on sight)", "programs running from /tmp or from memory",
                                         "connections to mining-pool ports", "one process above 90% CPU for 15 minutes"],
                              "alerts": [], "alert_count": 0}},
        "cron": {"checked": now - 20, "approved_at": now - 3 * 86400, "unapproved": 0, "removed": 0,
                 "files": [{"file": "/etc/crontab", "user": "", "new_file": False, "deleted": False, "removed": [],
                            "entries": [{"when": "17 * * * *", "user": "root", "cmd": "cd / && run-parts --report /etc/cron.hourly", "setting": False, "approved": True},
                                        {"when": "0 3 * * *", "user": "root", "cmd": "/usr/local/bin/backup.sh >> /var/log/backup.log 2>&1", "setting": False, "approved": True}]},
                           {"file": "/var/spool/cron/crontabs/admin", "user": "admin", "new_file": False, "deleted": False, "removed": [],
                            "entries": [{"when": "*/30 * * * *", "user": "admin", "cmd": "/home/admin/bin/sync-notes.sh", "setting": False, "approved": True}]}],
                 "folders": [{"folder": "/etc/cron.daily", "scripts": [{"name": "apt-compat", "mtime": now - 90 * 86400},
                                                                      {"name": "logrotate", "mtime": now - 90 * 86400}]}],
                 "timers": [{"unit": "logrotate.timer", "runs": "logrotate.service", "next": now + 7200, "last": now - 79200},
                            {"unit": "apt-daily.timer", "runs": "apt-daily.service", "next": now + 20000, "last": now - 60000}]},
        "locks": {"checked": now - 20, "files": [{"path": "/srv/app/config.ini", "exists": True, "locked": True, "attrs": "----i---------e-------",
                                                  "mtime": now - 40 * 86400, "symlink": False}]},
        "backups": {"checked": now - 20, "max_age_hours": 26,
                    "run": {"start": run, "end": run + 892, "errors": [], "warnings": [], "warning_count": 0},
                    "jobs": [{"label": "Nightly snapshot", "every": "nightly", "remote": "", "last_ok": run + 840, "stale": False, "max_age_hours": 26,
                              "items": [{"time": run + 840, "took": 812}],
                              "dest": {"path": "/mnt/backup", "free": 2.1e12, "total": 4e12, "size": {"bytes": 3.2e11, "at": run + 1500, "secs": 64}}},
                             {"label": "DB dumps", "every": "nightly, 7 kept", "remote": "", "last_ok": run + 30, "stale": False, "max_age_hours": 26,
                              "items": [{"time": run + 20, "took": 4, "name": "app-db", "size": "4.1M"}],
                              "files": {"count": 7, "newest": run + 30, "oldest": run - 6 * 86400, "bytes": 2.9e7, "list": []}},
                             {"label": "Offsite copy", "every": "nightly", "remote": "on another machine", "last_ok": run + 2300, "stale": False,
                              "max_age_hours": 26, "items": [{"time": run + 2300, "took": None, "name": "remote", "size": "1.2 GiB", "took_text": "6:12"}]}]},
    }


def live():
    """Fake /api/live for the demo: an 8-core / 16-thread single-CCD CPU wandering around."""
    s = system()
    threads = [{"use": round(wobble(12, 10, 20 + i), 1), "mhz": int(wobble(3900, 600, 7 + i)), "group": 0, "core": i % 8, "smt": i >= 8}
               for i in range(16)]
    disks = [{"dev": "sda", "name": "disk1", "read_bps": wobble(9e7, 6e7, 30), "write_bps": wobble(2e6, 2e6), "busy": wobble(35, 25, 30)},
             {"dev": "nvme0n1", "name": "system", "read_bps": wobble(4e6, 3e6), "write_bps": wobble(6e6, 5e6), "busy": wobble(4, 3)},
             {"dev": "sdb", "name": "disk2", "read_bps": 0, "write_bps": 0, "busy": 0}]
    return {"cpu": s["cpu"], "cpu_model": s["cpu_model"], "threads": threads, "temps": s["temps"],
            "mem_total": s["mem_total"], "mem_avail": s["mem_avail"], "load": s["load"],
            "power": {"pkg_w": round(wobble(48, 18, 25), 1), "limit_w": 88.0}, "gpu": s["hw"]["gpu"],
            "io": {"read_bps": sum(d["read_bps"] for d in disks), "write_bps": sum(d["write_bps"] for d in disks),
                   "disks": sorted(disks, key=lambda d: -d["busy"])}}


# The collector's side of the settings page (the real one comes from hw.json's "collector" block).
COLLECTOR = {
    "installed": True, "supported": True, "error": "", "wanted": {}, "pending": [], "pending_values": {}, "rejected": [],
    "rejected_more": 0, "from_page": [], "sandbox": "strict", "fp": {},
    "detected": {"mounts": ["/", "/mnt/disk1", "/mnt/disk2", "/mnt/parity"], "ifaces": ["eth0"], "jails": ["sshd"],
                 "ssh_units": ["ssh"], "authelia": ["authelia"], "authelia_db": {"authelia": "/srv/authelia/config/db.sqlite3"},
                 "gpu": True, "firewall": "ufw", "default_iface": "eth0"},
    "settings": {"interval": 5, "mount_labels": {"/": "system", "/mnt/parity": "parity"},
                 "security.fail2ban_jail": "sshd", "security.ssh_unit": "ssh", "security.authelia_container": "authelia",
                 "security.process_watch": True, "security.cron_watch": True, "security.firewall": True,
                 "security.immutable_files": ["/opt/app/config.ini"], "backup.log": "/var/log/backup.log",
                 "backup.max_age_hours": 26,
                 "backup.jobs": [{"label": "Nightly snapshot", "regex": "snapshot done", "dest": "/mnt/backup", "size": True, "every": "nightly"},
                                 {"label": "DB dumps", "regex": "dumped (?P<name>\\S+)", "files": ["/srv/backups/db/*.sql.gz"]}]},
}
