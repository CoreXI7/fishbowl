#!/usr/bin/env python3
"""Root-side stats collector for homelab-dash (installed as /usr/local/sbin/homelab-dash-collector).

Writes <out_dir>/hw.json (and sec-detail.json for the Security & backups detail views), which the dash container
mounts read-only. Gathers only what the
container can't (or shouldn't) see itself: host network rate, GPU, drive usage, SMART health,
container counts, and optional security/backup checks. It never listens on the network.
SMART uses `-n standby` so sleeping drives are never woken.

Config: /etc/homelab-dash/collector.toml (see collector.example.toml). Every key is optional. The dash's settings page
can change some of them too (see "settings from the dash's settings page" below): what can only change what's shown is
applied by itself; paths, patterns and container names wait for `sudo homelab-dash-collector --apply`.
"""
import base64
import collections
import contextlib
import copy
import datetime
import errno
import fcntl
import fnmatch
import glob
import hashlib
import heapq
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

CONFIG_FILE = os.environ.get("HOMELAB_DASH_COLLECTOR_CONFIG", "/etc/homelab-dash/collector.toml")
PAGE_TOML = os.path.join(os.path.dirname(CONFIG_FILE), "collector.page.toml")   # approved on the host with --apply


def read_toml(path, root_only=False):
    """A TOML config file ({} if missing). root_only: when running as root, only trusted if root owns it and nobody
    else can write it (it's written by --apply; a stray copy someone else can edit must not steer root)."""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if root_only and os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o022 or not stat.S_ISREG(st.st_mode)):
                raise PermissionError(1, "not a regular file owned by root and writable only by root")
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


FILE_CFG = read_toml(CONFIG_FILE)   # what you wrote; CFG = the settings in effect (see apply_config())
CFG = FILE_CFG

BUILD = 20261030   # written into hw.json: the dashboard asks for `sudo bash collector/install.sh` when it wants a newer one
OUT_DIR = FILE_CFG.get("out_dir", "/var/lib/homelab-dash")
OUT = os.path.join(OUT_DIR, "hw.json")
DETAIL = os.path.join(OUT_DIR, "sec-detail.json")   # the Security & backups detail views (bigger, read on demand)
DASH_UID = int(FILE_CFG.get("dash_uid", 1000))    # the dash container's user/group: owns the request folder, can read hw.json
SEC = CFG.get("security", {})
BAK = CFG.get("backup", {})
REQ_DIR = os.path.join(OUT_DIR, "req")                          # dash drops approval requests here
REQ_FILE = os.path.join(REQ_DIR, "cron-approve")
CRON_APPROVED = os.path.join(OUT_DIR, "cron-approved.json")   # root-only snapshot of approved cron lines


def run(cmd, timeout=20):
    """A command's stdout, or "" if it fails or times out (every check here is optional)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def have(cmd):
    return shutil.which(cmd) is not None


def default_iface():
    """The interface that carries the default route (e.g. eth0, enp3s0, br0)."""
    try:
        with open("/proc/net/route") as f:
            routes = f.read().splitlines()[1:]
        for line in routes:
            f = line.split()
            if f[1] == "00000000":
                return f[0]
    except OSError:
        pass
    return "eth0"


NET_IF = CFG.get("net_if") or default_iface()


# ---------- drives ----------

def mounts():
    """Mount points to show: config globs (default: / plus every real disk under /mnt and /media)."""
    pats = CFG.get("mounts") or ["/", "/mnt/*", "/media/*/*", "/srv/*"]
    out = []
    for p in pats:
        hits = [p] if os.path.lexists(p) else glob.glob(p)   # an existing path counts as it is ([ ] * ? in a name too)
        hits = sorted(hits, key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)])
        out += [h for h in hits if h not in out]
    return out


def mount_sources():
    """mount point -> device (e.g. /media/x/disk1 -> /dev/sdb1)."""
    src = {}
    for line in run(["findmnt", "-rno", "SOURCE,TARGET"]).splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            src[parts[1].replace("\\x20", " ")] = parts[0]
    return src


def whole_disk(dev):
    """/dev/sdb1 -> /dev/sdb, /dev/nvme0n1p2 -> /dev/nvme0n1 (SMART is per disk, not per partition)."""
    base = os.path.basename(dev)
    if base.startswith(("nvme", "mmcblk")):
        return "/dev/" + re.sub(r"p\d+$", "", base)
    return "/dev/" + re.sub(r"\d+$", "", base)


def disks():
    """Size/used/free per shown mount. A mount listed in the config but not mounted is reported as missing."""
    src = mount_sources()
    labels = CFG.get("mount_labels", {})
    out = []
    for m in mounts():
        name = labels.get(m) or ("system" if m == "/" else os.path.basename(m))
        if m not in src or (m != "/" and not os.path.ismount(m)):
            if m in (CFG.get("mounts") or []):   # only explicitly listed mounts count as "missing"
                out.append({"mount": m, "name": name, "missing": True})
            continue
        if not src[m].startswith("/dev/"):
            continue                              # skip tmpfs, overlay, network shares picked up by a glob
        try:
            st = os.statvfs(m)
        except OSError:                           # (a share whose server is gone: EIO; shown missing, not a crash)
            out.append({"mount": m, "name": name, "missing": True})
            continue
        dev = whole_disk(src[m])
        model_f = f"/sys/block/{os.path.basename(dev)}/device/model"
        model = open(model_f).read().strip() if os.path.exists(model_f) else ""
        out.append({"mount": m, "name": name, "dev": dev, "model": model, "system": m == "/",
                    "total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize,
                    "free": st.f_bavail * st.f_frsize})
    return out


def smart(dev):
    """SMART health + temperature for one disk. Sleeping disks are left asleep (-n standby)."""
    raw = run(["smartctl", "-n", "standby", "-i", "-H", "-A", "-j", dev], timeout=30)
    try:
        d = json.loads(raw)
    except Exception:
        return {"error": "no data"}
    msgs = " ".join(m.get("string", "") for m in d.get("smartctl", {}).get("messages", []))
    if "STANDBY" in msgs.upper():
        return {"standby": True}
    try:                                          # (odd output from a firmware or a smartctl version: no crash)
        attrs = {a["id"]: a["raw"]["value"] for a in d.get("ata_smart_attributes", {}).get("table", [])}
        nv = d.get("nvme_smart_health_information_log", {})
        return {
            "temp": d.get("temperature", {}).get("current"),
            "passed": d.get("smart_status", {}).get("passed"),
            "hours": d.get("power_on_time", {}).get("hours"),
            "realloc": attrs.get(5, nv.get("media_errors")),
            "pending": attrs.get(197),
            "uncorrect": attrs.get(198),
            "wear": nv.get("percentage_used"),
            "model": d.get("model_name"),
        }
    except (AttributeError, KeyError, TypeError):
        return {"error": "unexpected output"}


# ---------- GPU / containers / network ----------

def gpu():
    """NVIDIA GPU load, encoder/decoder use, VRAM, power, fan, encode sessions (None without nvidia-smi)."""
    if not have("nvidia-smi") or CFG.get("gpu") is False:
        return None
    q = "name,temperature.gpu,utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,power.draw,fan.speed,encoder.stats.sessionCount"
    line = run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"]).strip().splitlines()
    if not line:
        return None
    v = [x.strip() for x in line[0].split(",")]
    if len(v) < 10:                               # (an odd answer: no GPU tile, not a crash)
        return None
    num = lambda s: float(s) if re.fullmatch(r"[\d.]+", s) else None
    return {"name": v[0], "temp": num(v[1]), "util": num(v[2]), "enc": num(v[3]), "dec": num(v[4]),
            "mem_used": num(v[5]), "mem_total": num(v[6]), "power": num(v[7]), "fan": num(v[8]),
            "sessions": num(v[9])}


CONTAINER_NAMES = {}          # full container id -> name, refreshed with containers()


def containers():
    """Running/total container counts; also refreshes CONTAINER_NAMES for the usage breakdown."""
    if not have("docker"):
        return None
    rows = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}} {{.State}}"]).split("\n")
    rows = [r.split() for r in rows if r.strip()]
    CONTAINER_NAMES.clear()
    CONTAINER_NAMES.update({r[0]: r[1] for r in rows if len(r) >= 2})
    states = [r[2] for r in rows if len(r) >= 3]
    return {"running": states.count("running"), "total": len(states)}


# ---------- what is using the CPU / memory: every process, grouped by container (or program name) ----------

PAGE = os.sysconf("SC_PAGE_SIZE")
CONTAINER_RE = re.compile(r"(?:/docker/|docker-)([0-9a-f]{64})")


def read_text(path):
    with open(path) as f:
        return f.read()


class Usage:
    """Samples /proc every round; CPU % = share of the whole machine between two samples.

    Which container a process belongs to (from /proc/<pid>/cgroup) is looked up once per process and remembered,
    keyed by pid + start time so a recycled pid is looked up again. That halves the files read every round."""
    def __init__(self):
        self.prev, self.prev_total = {}, None
        self.ncpu = os.cpu_count() or 1
        self.container_of = {}            # pid -> (start time, container id or None)
        self.cgroup_of = {}               # container id -> its cgroup folder under /sys/fs/cgroup

    @staticmethod
    def total_jiffies():
        with open("/proc/stat") as f:
            fields = f.readline().split()[1:]
        return sum(int(x) for x in fields[:8])

    def container_id(self, pid, started):
        hit = self.container_of.get(pid)
        if hit and hit[0] == started:
            return hit[1]
        cg = read_text(f"/proc/{pid}/cgroup")
        m = CONTAINER_RE.search(cg)
        cid = m[1] if m else None
        if cid and cid not in self.cgroup_of:
            line = next((l for l in cg.splitlines() if l.startswith("0::")), "")   # cgroup v2: "0::/docker/<id>"
            self.cgroup_of[cid] = "/sys/fs/cgroup" + line[3:] if line else None
        self.container_of[pid] = (started, cid)
        return cid

    def container_memory(self, cid):
        """A container's real memory use, the way `docker stats` counts it (memory in use minus reclaimable file
        cache). Adding up each process's RSS overcounts badly: memory shared between processes (nginx's 30+ workers,
        gunicorn workers) is counted once per process. None if the cgroup files can't be read (cgroup v1 etc.)."""
        path = self.cgroup_of.get(cid)
        if not path:
            return None
        try:
            current = int(read_text(path + "/memory.current"))
            stat = dict(l.split() for l in read_text(path + "/memory.stat").splitlines())
            return max(current - int(stat.get("inactive_file", 0)), 0)
        except (OSError, ValueError):
            return None

    def sample(self):
        total = self.total_jiffies()
        cur, groups = {}, {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                raw = read_text(f"/proc/{pid}/stat")
                comm = raw[raw.index("(") + 1:raw.rindex(")")]       # the name can contain spaces and brackets
                f = raw[raw.rindex(")") + 2:].split()                 # fields after the name (see man 5 proc)
                jiff, ppid, rss = int(f[11]) + int(f[12]), int(f[1]), int(f[21]) * PAGE   # utime+stime, parent, RSS
                cid = self.container_id(pid, f[19])                   # f[19] = start time
            except (OSError, ValueError, IndexError):
                continue                  # process exited mid-read
            cur[pid] = (jiff, comm)
            if cid:
                key, kind, name = "c:" + cid, "container", CONTAINER_NAMES.get(cid, cid[:12])
            elif ppid == 2 or pid == "2":
                key, kind, name = "kernel", "kernel", "kernel threads"
            else:
                key, kind, name = "p:" + comm, "process", comm
            prev = self.prev.get(pid)
            d = jiff - prev[0] if prev and prev[1] == comm else 0
            g = groups.setdefault(key, {"name": name, "kind": kind, "jiffies": 0, "mem": 0, "procs": 0, "top": {}})
            g["jiffies"] += max(d, 0)
            g["mem"] += rss if kind != "kernel" else 0
            g["procs"] += 1
            if d > 0 and kind == "container":
                g["top"][comm] = g["top"].get(comm, 0) + d
        dt = total - self.prev_total if self.prev_total else 0
        self.prev, self.prev_total = cur, total
        for pid in self.container_of.keys() - cur.keys():
            del self.container_of[pid]    # forget processes that have exited
        for key, g in groups.items():     # containers: use the cgroup's real number instead of the RSS sum
            if g["kind"] == "container":
                real = self.container_memory(key[2:])
                if real is not None:
                    g["mem"] = real
        live = {key[2:] for key in groups if key.startswith("c:")}
        for cid in self.cgroup_of.keys() - live:
            del self.cgroup_of[cid]       # forget removed containers
        if dt <= 0:
            return None                   # first round: no delta yet
        out = []
        for g in groups.values():
            pct = 100 * g["jiffies"] / dt
            top = sorted(g.pop("top").items(), key=lambda kv: -kv[1])[:5]
            g.pop("jiffies")
            out.append({**g, "cpu": round(pct, 2), "top": [{"name": n, "cpu": round(100 * j / dt, 2)} for n, j in top]})
        out.sort(key=lambda g: -(g["cpu"] * 1e12 + g["mem"]))
        # keep the 45 biggest by CPU plus the 45 biggest by memory; fold the rest into "other"
        keep = {id(g) for g in out[:45]} | {id(g) for g in sorted(out, key=lambda g: -g["mem"])[:45]}
        rest = [g for g in out if id(g) not in keep]
        out = [g for g in out if id(g) in keep]
        if rest:
            out.append({"name": f"{len(rest)} other programs", "kind": "other", "cpu": round(sum(g["cpu"] for g in rest), 2),
                        "mem": sum(g["mem"] for g in rest), "procs": sum(g["procs"] for g in rest), "top": []})
        idle = 100 - sum(g["cpu"] for g in out)
        return {"ncpu": self.ncpu, "idle": round(max(idle, 0), 2), "groups": out}


USAGE = Usage()


RAPL = "/sys/class/powercap/intel-rapl:0"      # CPU package energy counter (Intel, and AMD Zen via the same interface)


def rapl_energy():
    """Package energy in microjoules since boot (root-only file), or None without RAPL."""
    try:
        return int(read_text(RAPL + "/energy_uj"))
    except (OSError, ValueError):
        return None


def rapl_limit_w():
    """The package power limit the CPU reports (long-term constraint), or None."""
    try:
        return int(read_text(RAPL + "/constraint_0_power_limit_uw")) / 1e6 or None
    except (OSError, ValueError):
        return None


def net_bytes():
    """Total bytes received/sent on the main interface so far (the rate is the change between rounds)."""
    base = f"/sys/class/net/{NET_IF}/statistics/"
    return int(open(base + "rx_bytes").read()), int(open(base + "tx_bytes").read())


# ---------- cron watch: flags any crontab change until you approve it on the dash ----------

CRON_GLOBS = ("/etc/crontab", "/etc/cron.d/*", "/var/spool/cron/crontabs/*",
              "/var/spool/cron/*")                       # (the last: RHEL-style per-user crontabs; dirs are skipped below)


def cron_files():
    """Every crontab's active lines (comments and blanks dropped), by file. Split on \\n only, as cron does: Python's
    universal newlines also split on \\r, so "approved line\\r#; payload" read as the approved line plus a comment
    while cron ran the payload. A \\r is kept, shown as the two characters \\r."""
    files = [f for g in CRON_GLOBS for f in (sorted(glob.glob(g)) if "*" in g else [g])]
    snap = {}
    for f in files:
        if not os.path.isfile(f):
            continue
        try:
            with open(f, "rb") as fh:
                text = fh.read(4 << 20).decode("utf-8", "replace").replace("\r", "\\r")
            lines = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("#")]
        except OSError:
            continue
        snap[f] = lines
    return snap


def cron_hash(snap):
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()


_RD_URL = re.compile(r"webhook|token|key|api|secret|sig", re.I)
_RD_VAL = r"('[^']{0,300}'|\"[^\"]{0,300}\"|\S+)"
# a name holding a key word, found by a look-ahead from the start of each word (once per word: linear)
_RD_NAME = re.compile(r"(?i)(?<!\w)(?=\w*?(?:pass(?:word|wd)?|token|secret|api_?key))(\w+)(\s*[=:]\s*)" + _RD_VAL)
_RD_OPT = re.compile(r"(?i)(?<![\w-])(?=--?[\w-]*?(?:pass(?:word|wd)?|token|secret|key|auth))(--?[\w-]+)([\s=]+)" + _RD_VAL)


def redact(line):
    """Strip secrets from cron/log lines before they go into hw.json (and onto the dashboard). The line is cut first,
    and a cut line says how much is left out. Every pattern takes a token once and checks it for key words afterwards
    (a pattern with the key word between two runs of letters was cubic: a 1,000-character line of "--pass" took 0.1 s,
    and a crontab can hold a thousand such lines)."""
    n, line = len(line), line[:1000]
    line = re.sub(r"(?i)https?://\S+", lambda m: "[url]" if _RD_URL.search(m.group(0)) else m.group(0), line)
    line = re.sub(r"(?<!\w)(\w+://)[^/\s:@]+:[^@\s]+@", r"\1***@", line)                 # user:pass@host
    line = _RD_NAME.sub(r"\1\2***", line)                                                     # PASSWORD=x
    line = _RD_OPT.sub(r"\1\2***", line)                                                      # --password x, --b2-key x
    line = re.sub(r"(\s-p)(?!\s)\S+", r"\1***", line)                                     # mysql -pSecret
    # -p pw (docker login, mongodump), -P pw (zip, mosquitto), -k pw (openssl): not when it's a path, a URL or a port
    line = re.sub(r"(\s-[pPk]\s+)(?![/~.$-]|\w+://|\d[\d.:]*(?:/(?:tcp|udp))?(?:\s|$))\S+", r"\1***", line)
    line = re.sub(r"(redis-cli\b[^|;&]*?\s-a\s+)\S+", r"\1***", line)                     # redis-cli -a pw
    line = re.sub(r"(--user[\s=]+[^\s:]+:)\S+", r"\1***", line)                               # curl --user u:pw
    line = re.sub(r"(\s-u\s+[^\s,:]+,)\S+", r"\1***", line)                                 # lftp -u user,pw
    line = re.sub(r"(\bpass:)\S+", r"\1***", line)                                           # openssl -pass pass:pw
    line = re.sub(r"(smbclient\s+\S+\s+)(?!-)\S+", r"\1***", line)                          # smbclient //nas/share pw
    line = re.sub(r"(\becho\s+)\S+(?=\s*\|[^|]*--password-stdin)", r"\1***", line)        # echo pw | ... --password-stdin
    # push and ping services: the path is the secret (ntfy topics, healthchecks ids, bot tokens, webhooks)
    line = re.sub(r"(?i)(https?://(?:ntfy\.[^/\s'\"]+|[^/\s'\"]*hc-ping\.com|[^/\s'\"]*healthchecks[^/\s'\"]*|api\.telegram\.org|"
                  r"(?:[\w.-]+\.)?discord(?:app)?\.com|hooks\.slack\.com|gotify[^/\s'\"]*)/)[^\s'\"]+", r"\1***", line)
    line = re.sub(r"(\s-u\s+[^\s:]+:)\S+", r"\1***", line)                               # curl -u user:pass
    line = re.sub(r"(sshpass\s+(?:-\w+\s+)*-p\s*)\S+", r"\1***", line)                   # sshpass -p pw
    line = re.sub(r"(\s-U\s+[^\s%]+%)\S+", r"\1***", line)                               # smbclient -U user%pw
    line = re.sub(r"(?i)((?:x-api-key|api-key|cookie|x-auth-token)\s*:\s*)[^\s'\"]+", r"\1***", line)
    line = re.sub(r"(?i)([?&](?:k|key|token|apikey|api_key|auth|sig|signature|secret|pass(?:word)?)=)[^&\s'\"]+", r"\1***", line)
    line = re.sub(r"(?i)(authorization:\s*\w+\s+)\S+", r"\1***", line)
    line = re.sub(r"[A-Za-z0-9_\-+=]{28,}", "…", line)   # long random-looking tokens
    return line if len(line) <= 220 and n <= 1000 else line[:200] + f"… (+{max(n, len(line)) - 200} characters)"


def load_approved():
    try:
        return json.load(open(CRON_APPROVED))
    except Exception:
        return None


def create_private(path):
    """A new file for writing, readable by root alone from its first byte (chmod afterwards left a moment when the
    unredacted crontab snapshot was readable by everyone, the dash's uid included)."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w")


def save_approved(snap):
    tmp = CRON_APPROVED + ".tmp"
    with create_private(tmp) as f:
        json.dump({"snap": snap, "hash": cron_hash(snap), "approved_at": int(time.time())}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRON_APPROVED)


def card_lines(lines, n=20):
    """At most n lines for the card, redacted, and a last one saying how many more there are (approving approves all)."""
    return [redact(l) for l in lines[:n]] + ([f"… and {len(lines) - n} more lines"] if len(lines) > n else [])


def cron_state():
    """Compare the crontabs with the approved snapshot: what was added/removed in which file."""
    snap = cron_files()
    appr = load_approved()
    if appr is None:                      # first run: today's crontabs become the baseline
        save_approved(snap)
        appr = load_approved()
    old = appr["snap"]
    changes = []
    for f in sorted(set(snap) | set(old)):
        added = [l for l in snap.get(f, []) if l not in old.get(f, [])]
        removed = [l for l in old.get(f, []) if l not in snap.get(f, [])]
        if added or removed or (f in snap) != (f in old):
            changes.append({"file": f, "new_file": f not in old, "deleted": f not in snap,
                            "added": card_lines(added), "removed": card_lines(removed)})
    return {"ok": not changes, "hash": cron_hash(snap), "approved_at": appr.get("approved_at"),
            "files": len(snap), "changes": changes}


def handle_cron_approval():
    """Approve only if the requested hash equals the current state (the dash can't approve anything else)."""
    os.makedirs(REQ_DIR, exist_ok=True)
    os.chown(REQ_DIR, 0 if os.geteuid() == 0 else -1, DASH_UID)   # root's (so root can remove requests without
    os.chmod(REQ_DIR, 0o770)                                     # overriding permissions), the dash's group writes
    if not os.path.lexists(REQ_FILE):
        return False
    try:   # the dash can write here: never follow a link it planted or wait on a FIFO (that would stop the main loop)
        want = read_regular(REQ_FILE, 100).decode("ascii", "replace").strip()
    except CheckFailed:
        want = ""
    try:
        os.remove(REQ_FILE)
    except OSError:
        pass
    snap = cron_files()
    if re.fullmatch(r"[0-9a-f]{64}", want) and want == cron_hash(snap):
        save_approved(snap)
        return True
    return False


# ---------- security checks (each one can be switched off in the config) ----------

JOURNAL_MAX_LINES = 100_000   # newest lines of the 7-day SSH journal kept (a flood of log lines can't fill memory)
JOURNAL_LINE_MAX = 1100       # bytes; no real sshd line is longer (longer ones are skipped)
_journal = {"at": float("-inf"), "lines": None, "lock": threading.Lock()}
LAST_SSH = [None]             # the card's last good SSH numbers: shown again when a read fails, not "all quiet"


def _journal_lines(stream, line_max=JOURNAL_LINE_MAX):
    """Lines from journalctl's binary output: split on \\n only (a \\r can't start a fake line), at most
    line_max bytes read per line (the rest of a longer one is skipped without ever holding it), kept as
    ASCII (sshd escapes 8-bit bytes anyway; anything else becomes "?"), so 100,000 lines stay under ~110 MB."""
    while True:
        line = stream.readline(line_max + 1)
        if not line:
            return
        if len(line) > line_max:
            while line and not line.endswith(b"\n"):    # skip the rest of an over-long line
                line = stream.readline(65536)
            continue
        yield line.rstrip(b"\n").decode("ascii", "replace").replace("\ufffd", "?")   # ASCII: 1 byte a character



def ssh_journal(units, timeout=60, wait=None):
    """The SSH units' journal for the last 7 days (short-iso), shared by the card (main loop) and the detail views
    (their thread): read at most once every 50 s. No -n: with it journalctl walked the whole journal backwards
    (1.5 s of CPU per read on a 2 GB journal); without it, it seeks to --since. The newest JOURNAL_MAX_LINES are
    kept while streaming, and lines over JOURNAL_LINE_MAX bytes (no real sshd line is) are skipped: at most ~110 MB,
    and only one copy at a time. On timeout journalctl's whole process group is killed (a child holding the pipe
    can't keep the read open). wait: seconds to wait for a read already running in the other thread; after that
    it raises CheckFailed("busy") (the old lines were let go for the re-read) and the card uses LAST_SSH.
    Raises CheckFailed like out()."""
    if not _journal["lock"].acquire(timeout=-1 if wait is None else wait):
        if _journal["lines"] is not None:
            return _journal["lines"]
        raise CheckFailed("journalctl is busy (still reading in the other thread)")
    try:
        if _journal["lines"] is not None and time.monotonic() - _journal["at"] < 50:
            return _journal["lines"]
        _journal["lines"] = None                          # let the old copy go before reading the new one
        try:
            p = subprocess.Popen(["journalctl", *units, "--since", "-7d", "-o", "short-iso", "--no-pager", "-q"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise CheckFailed(f"journalctl: {e.strerror}")
        killed = threading.Event()

        def kill_group():
            killed.set()
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        timer = threading.Timer(timeout, kill_group)
        try:
            timer.start()
            lines = list(collections.deque(_journal_lines(p.stdout), maxlen=JOURNAL_MAX_LINES))
        finally:
            timer.cancel()
            if timer.ident is not None:                   # start() itself can fail (no thread left)
                timer.join()
            p.stdout.close()
            try:
                rc = p.wait(5)
            except subprocess.TimeoutExpired:             # closed its output but won't exit
                kill_group()
                rc = p.wait()
        if killed.is_set():
            raise CheckFailed(f"journalctl timed out after {timeout} s")
        if rc != 0:
            raise CheckFailed(f"journalctl failed ({'signal ' + str(-rc) if rc < 0 else 'exit code ' + str(rc)})")
        _journal.update(at=time.monotonic(), lines=lines)
        return lines
    finally:
        _journal["lock"].release()


AUTHELIA_FAIL = re.compile(r'time="([^"]+)".*Unsuccessful (\w+) authentication attempt by user \'([^\']*)\'.*remote_ip=(\S+)')


def authelia_failures(container, timeout=30):
    """(how many, the newest 6) failed sign-ins in the Authelia container's last 24 h of log. Streamed line by line
    (lines over 2,000 bytes skipped without being held) and only those two kept: anyone on the internet can fill that
    log with failed sign-ins and long user names, and reading it whole got the collector OOM-killed (200 MB cap) at
    ~55 MB of log, again every minute. On timeout docker's process group is killed (a hung dockerd can't hold the
    security card)."""
    try:
        p = subprocess.Popen(["docker", "logs", "--since", "24h", container], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return 0, []

    def kill_group():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    timer = threading.Timer(timeout, kill_group)
    count, newest = 0, []                                 # a heap of the 6 newest (stdout and stderr interleave)
    try:
        timer.start()
        for n, line in enumerate(_journal_lines(p.stdout, 2000)):
            if "Unsuccessful" not in line:
                continue
            m = AUTHELIA_FAIL.search(line)
            if m:
                count += 1
                item = (m.group(1), n, {"time": m.group(1), "factor": m.group(2), "user": m.group(3), "ip": m.group(4)})
                (heapq.heappush if len(newest) < 6 else heapq.heappushpop)(newest, item)
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
        p.stdout.close()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            kill_group()
            p.wait()
    return count, [x[2] for x in sorted(newest, reverse=True)]


MINERS = ("xmrig", "minerd", "cryptonight", "kdevtmpfsi", "kinsing", "kworkerds", "xmr-stak")
LAST_TOP = []        # the 10 busiest processes from the latest security() round (shown in the Process watch details)
TOP_AT = [0]         # ... and when they were measured


def security():
    """fail2ban, SSH logins/failures, Authelia failures, miner process watch, cron watch, immutable files, firewall."""
    s = {}
    jail = SEC.get("fail2ban_jail", "sshd")
    if jail and have("fail2ban-client"):
        out = run(["fail2ban-client", "status", jail])
        num = lambda key: int((re.search(key + r":\s*(\d+)", out) or [0, 0])[1])
        ips = re.search(r"Banned IP list:\s*(.*)", out)
        s["f2b"] = {"ok": bool(out), "jail": jail, "banned_now": num("Currently banned"), "banned_total": num("Total banned"),
                    "ips": (ips.group(1).split() if ips else [])[:20]}
    unit = SEC.get("ssh_unit", "ssh")
    if unit and have("journalctl"):
        units = ["-u", unit] + (["-u", "sshd"] if unit == "ssh" else [])   # Debian/Ubuntu call it ssh, others sshd
        try:
            lines = ssh_journal(units, 30, wait=5)    # the main loop never waits long for the detail thread's read
        except CheckFailed:
            lines = None
        logins, failed, day_ago = [], 0, time.time() - 86400
        for line in lines or []:
            if SSH_FAILED.search(line) and (when(line) or 0) > day_ago:    # the last 24 h, from the same lines
                failed += 1                                                 # one per line, however it's worded
            # anchored, like the details: a user name such as "Accepted publickey for root from 10.0.0.2" in a failed
            # attempt must not show up as a login
            m = SSH_LINE.match(line)
            a = SSH_ACCEPT.match(m[3]) if m else None
            if a and valid_ip(a["ip"]):
                logins.append({"time": m[1], "method": a["method"], "user": clip(a["user"], 64), "ip": valid_ip(a["ip"])})
        if lines is not None:
            LAST_SSH[0] = {"logins": logins[-8:][::-1], "failed_24h": failed, "as_of": int(time.time())}
        # a failed read keeps the last good numbers, and says so (the card shows "as of …")
        s["ssh"] = {**(LAST_SSH[0] or {"logins": [], "failed_24h": 0, "as_of": None}), "stale": lines is None}
    authelia = SEC.get("authelia_container", "")
    if authelia and have("docker"):
        count, recent = authelia_failures(authelia)
        s["authelia"] = {"failed_24h": count, "recent": recent}
    if SEC.get("process_watch", True):
        procs = run(["ps", "-eo", "pid=,pcpu=,comm=,args=", "--sort=-pcpu"]).splitlines()
        watch = tuple(SEC.get("bad_process_names", MINERS))
        me = str(os.getpid())                                 # by PID: a name check could be dodged by a process naming itself
        found = [p.split(None, 3)[2] for p in procs if any(m in p.lower() for m in watch) and p.split(None, 1)[0] != me]
        top = []
        snap = run(["top", "-b", "-n", "2", "-d", "1", "-o", "%CPU", "-w", "200"]).split("\n\n")
        rows = [l.split() for l in (snap[-1].splitlines() if snap else []) if re.match(r"\s*\d+ ", l)]
        try:                              # for the Process watch details; never allowed to break this tile
            LAST_TOP[:] = [{"pid": int(f[0]), "user": f[1][:16], "cpu": float(f[8].replace(",", ".")), "mem": float(f[9].replace(",", ".")),
                            "name": f[11][:40]} for f in rows[:10] if len(f) >= 12]
            TOP_AT[0] = time.time()
        except (ValueError, IndexError):
            pass
        for f in rows[:3]:                # live %CPU (2nd sample), not lifetime average
            if len(f) >= 12:
                top.append({"pid": int(f[0]), "cpu": float(f[8].replace(",", ".")), "name": f[11]})
        s["procs"] = {"found": found, "top": top}
    if SEC.get("cron_watch", True):
        s["cron"] = cron_state()
    locked = SEC.get("immutable_files", [])
    if locked:
        locks = []
        for f in locked:
            attrs = run(["lsattr", "-d", f]).split()
            locks.append({"file": os.path.basename(f), "locked": bool(attrs) and "i" in attrs[0], "exists": os.path.exists(f)})
        s["locks"] = locks
    if SEC.get("firewall", True):
        if have("ufw"):
            s["firewall"] = {"name": "UFW", "status": (run(["ufw", "status"]).splitlines() or ["unknown"])[0].replace("Status: ", "")}
        elif have("firewall-cmd"):
            s["firewall"] = {"name": "firewalld", "status": run(["firewall-cmd", "--state"]).strip() or "unknown"}
    return s


# ---------- backups: read your backup script's log + check that fresh files exist ----------

def backups():
    """Did the last backup run cleanly (from its log) and are its output files fresh?"""
    log = BAK.get("log", "")
    arts = BAK.get("artifacts", [])
    if not log and not arts:
        return None
    b = {"log": [], "last_run": None, "ok": None, "artifacts": []}
    lines = tail_lines(log, 4_000_000, "backup.log") if log else []   # never a FIFO, never a whole huge file
    if lines:
        last_day = lines[-1][:10]
        run_lines = [l for l in lines if l.startswith(last_day)] if re.fullmatch(r"\d{4}-\d\d-\d\d", last_day) else lines[-30:]
        b["log"] = [redact(l) for l in run_lines[-12:]]
        b["last_run"] = run_lines[0][:19]
        txt = "\n".join(run_lines)
        ok_re, bad_re = BAK.get("success_regex", ""), BAK.get("failure_regex", r"(?i)\berror|failed\b")
        b["ok"] = (not ok_re or bool(re.search(ok_re, txt))) and not (bad_re and re.search(bad_re, txt))
        b["checks"] = []
        for c in BAK.get("checks", []):       # e.g. "offsite copies": count matching lines in the last run
            n = len(re.findall(c.get("regex", "^$"), txt, flags=re.M))
            b["checks"].append({"label": c.get("label", "check"), "count": n, "ok": n >= int(c.get("min_count", 1))})
        if any(not c["ok"] for c in b["checks"]):
            b["ok"] = False
    max_age = float(BAK.get("max_age_hours", 26)) * 3600
    for a in arts:
        strict = "backup.artifacts" in APPROVED_NOW
        files = [st for st in (regular_stat(f, strict) for f in glob.glob(a.get("glob", ""))) if st]
        if not files:
            b["artifacts"].append({"label": a.get("label", "files"), "count": 0, "fresh": False})
            continue
        newest = max(files, key=lambda st: st.st_mtime)
        mt = newest.st_mtime
        b["artifacts"].append({"label": a.get("label", "files"), "count": len(files), "newest": int(mt),
                               "size": newest.st_size, "fresh": time.time() - mt < max_age})
    if b["artifacts"] and b["ok"] is not False:
        b["ok"] = all(a["fresh"] for a in b["artifacts"])
    return b


# ---------- details for the dashboard's Security & backups views: <out_dir>/sec-detail.json, every minute ----------
# Read-only by design: logs, command output and databases go in, JSON comes out, nothing on the system changes, and
# nothing from the dashboard reaches this code (the views only read the file). It runs in its own thread, so a slow
# or stuck check never holds up hw.json. Text from logs (user names, IPs, process names) can be attacker-controlled:
# it's length-limited and cleaned here, and the page escapes everything it shows.

class CheckFailed(Exception):
    pass


def out(cmd, timeout=20, ok=(0,)):
    """A command's stdout. Unlike run() (used by the tiles) it raises CheckFailed when the command fails or times out,
    so a broken check shows up as an error in the details instead of looking like "all quiet"."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CheckFailed(f"{cmd[0]} timed out after {timeout} s")
    except OSError as e:
        raise CheckFailed(f"{cmd[0]}: {e.strerror}")
    if p.returncode not in ok:
        raise CheckFailed(f"{cmd[0]} failed: " + (clip((p.stderr or p.stdout).strip(), 120) or f"exit code {p.returncode}"))
    return p.stdout


def clip(text, n=64):
    """Text from a log, at most n characters, with control characters and text-direction marks replaced."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]", "?", str(text))[:n]


def when(text):
    """Unix time of a timestamp at the start of text: '2026-10-28 02:00:01', '2026-10-28T02:00:01-04:00' or
    '2026-10-28 02:00:01.123456789-04:00' (Authelia). Local time when it has no offset. None if it isn't one."""
    m = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:[.,]\d+)?(Z|[+-]\d\d:?\d\d)?(?![A-Za-z0-9])", str(text or ""))
    if not m:
        return None
    off = m[3] or ""
    if off == "Z":
        off = "+00:00"
    elif off and ":" not in off:
        off = off[:3] + ":" + off[3:]
    try:
        return int(datetime.datetime.fromisoformat(f"{m[1]}T{m[2]}{off}").timestamp())   # naive = local time
    except ValueError:
        return None


def no_symlink(path):
    """True if no part of path is a symlink."""
    return bool(path) and os.path.realpath(path) == os.path.abspath(path)


def read_regular(path, limit, no_links=False):
    """A file's bytes, read without following a symlink at its name (no_links: anywhere on the way) or blocking on a
    FIFO, and only if it's a regular file of at most limit bytes (for files in folders a container can write to)."""
    name = os.path.basename(path)
    try:
        fd = open_regular(path, strict=no_links)
    except NotRegular:
        raise CheckFailed(f"{name} isn't a regular file")
    except OSError as e:
        raise CheckFailed(f"can't open {name}: {e.strerror}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CheckFailed(f"{name} isn't a regular file")
        if st.st_size > limit:
            raise CheckFailed(f"{name} is bigger than {limit >> 20} MB" if limit >= 1 << 20 else f"{name} is bigger than {limit} bytes")
        chunks, left = [], st.st_size
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


LINK_FOLDERS = ("/", "/usr", "/var")   # where a link is the system's own (/bin, /var/run, Fedora Atomic's /home)


def _root_only(dir_fd):
    st = os.fstat(dir_fd)
    return st.st_uid == 0 and not st.st_mode & 0o022


def _walk(path, strict=False):
    """An O_PATH descriptor of path if it's a regular file, reached without being steered: every part is opened as a
    path only (O_PATH runs no driver code: opening a device node a container planted would, e.g. /dev/watchdog resets
    the host) and checked first. A link is followed only where it sits in one of the system's own folders (LINK_FOLDERS,
    owned by root, writable by root alone: /var/run, /bin, Fedora Atomic's /home): anywhere else (a container's folder,
    even one Docker made root:root 0755) it could lead root anywhere. strict: no links at all."""
    path = os.path.normpath(path)
    if not path.startswith("/") or path == "/":
        raise OSError(errno.EINVAL, "not an absolute file path")
    parts, done, hops = path.strip("/").split("/"), "/", 0
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        while parts:
            part = parts.pop(0)
            nfd = os.open(part, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
            mode = os.fstat(nfd).st_mode
            if stat.S_ISLNK(mode):
                os.close(nfd)
                hops += 1
                if strict or hops > 40 or done not in LINK_FOLDERS or not _root_only(fd):
                    raise OSError(errno.ELOOP, "a link outside the system's own folders")
                target = os.path.normpath(os.path.join(done, os.readlink(part, dir_fd=fd)))
                parts = [x for x in target.strip("/").split("/") if x] + parts
                os.close(fd)
                fd, done = os.open("/", os.O_PATH | os.O_DIRECTORY), "/"
                continue
            os.close(fd)
            fd, done = nfd, os.path.join(done, part)
            if parts and not stat.S_ISDIR(mode):
                raise OSError(errno.ENOTDIR, "not a folder")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegular(errno.EINVAL, "not a regular file")
        out, fd = fd, None
        return out
    finally:
        if fd is not None:
            os.close(fd)


class NotRegular(OSError):
    pass


def open_regular(path, strict=False):
    """A read-only, non-blocking descriptor of path (see _walk), reopened from the checked O_PATH one, so nothing can
    be swapped in between."""
    pfd = _walk(path, strict)
    try:
        return os.open(f"/proc/self/fd/{pfd}", os.O_RDONLY | os.O_NONBLOCK)
    finally:
        os.close(pfd)


def regular_stat(path, strict=False):
    """os.stat of path if it's a regular file reached as _walk allows, else None (a link's target isn't a backup file)."""
    try:
        pfd = _walk(path, strict)
    except OSError:
        return None
    try:
        return os.fstat(pfd)
    finally:
        os.close(pfd)


APPROVED_NOW = frozenset()   # the approval keys whose value came from the settings page (--apply), not collector.toml


def page_path(key, path):
    """False for a path the settings page set that goes through a link: the dash can write in some folders on the
    host (its data folder), and a link planted there must never lead root elsewhere. Paths from collector.toml: True."""
    return key not in APPROVED_NOW or no_symlink(path)


def tail_lines(path, max_bytes=2_000_000, key=None):
    """The last max_bytes of a log file as lines (some logs can be grown at will by anyone who can reach the box).
    key = the setting it came from: when the settings page set it, no part of the path may be a link."""
    try:
        fd = open_regular(path, strict=key in APPROVED_NOW)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):          # a FIFO would block the details thread for good
            return []
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = [l[:8192] for l in data.decode("utf-8", "replace").splitlines()]   # (no log line needs more; regexes stay quick)
    return lines[1:] if size > max_bytes else lines         # the first line was cut in the middle


def valid_ip(text):
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


UFW_RULE = re.compile(r"^(?P<to>.+?)\s+(?P<action>(?:ALLOW|DENY|REJECT|LIMIT)(?: (?:IN|OUT|FWD))?)\s+(?P<frm>.+?)(?:\s+#\s?(?P<note>.*))?$")


def firewall_detail():
    if not SEC.get("firewall", True):
        return None
    now = int(time.time())
    if have("ufw"):
        fw = {"name": "UFW", "status": "unknown", "default": "", "logging": "", "rules": [], "checked": now}
        in_rules = False
        for line in out(["ufw", "status", "verbose"]).splitlines():
            if line.startswith("Status:"):
                fw["status"] = line.split(":", 1)[1].strip()
            elif line.startswith("Default:"):
                fw["default"] = line.split(":", 1)[1].strip()
            elif line.startswith("Logging:"):
                fw["logging"] = line.split(":", 1)[1].strip()
            elif re.match(r"\s*-+\s+-+\s+-+", line):
                in_rules = True
            elif in_rules and line.strip():
                m = UFW_RULE.match(line.strip())          # anchored on the action: a long To column can be followed by one space
                if m:
                    fw["rules"].append({"to": clip(m["to"], 80), "action": m["action"], "from": clip(m["frm"], 80),
                                        "note": redact(m["note"] or "")[:80]})
        fw["rules"] = fw["rules"][:200]
        return fw
    if have("firewall-cmd"):
        return {"name": "firewalld", "status": out(["firewall-cmd", "--state"], ok=(0, 252)).strip() or "unknown", "rules": [],
                "checked": now, "zones": redact(run(["firewall-cmd", "--list-all"]))[:2000]}
    return None


# fail2ban.observer repeats "Found" lines (with bantime.increment): only the filter's and the actions' lines count
F2B_EVENT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ fail2ban\.(?:filter|actions)\s*\[\d+\]: \w+\s+\[([\w.-]+)\] "
                       r"(Ban|Unban|Restore Ban|Increase Ban|Found) ([0-9a-fA-F.:]+)(?=\s|$)")


def fail2ban_detail():
    if not SEC.get("fail2ban_jail", "sshd") or not have("fail2ban-client"):
        return None
    m = re.search(r"Jail list:\s*(.*)", out(["fail2ban-client", "status"]))
    names = [j.strip() for j in (m[1] if m else "").split(",") if re.fullmatch(r"[\w.-]{1,64}", j.strip())]
    jails = []
    for j in names[:20]:
        o = out(["fail2ban-client", "status", j])
        num = lambda key: int((re.search(key + r":\s*(\d+)", o) or [0, 0])[1])
        ips = re.search(r"Banned IP list:\s*(.*)", o)
        jails.append({"jail": j, "failed_now": num("Currently failed"), "failed_total": num("Total failed"),
                      "banned_now": num("Currently banned"), "banned_total": num("Total banned"),
                      "ips": [ip for ip in (valid_ip(x) for x in (ips[1].split() if ips else [])) if ip][:200]})
    events, found = [], {}
    log = SEC.get("fail2ban_log", "/var/log/fail2ban.log")
    since = time.time() - 7 * 86400
    for line in (tail_lines(log + ".1", key="security.fail2ban_log") + tail_lines(log, key="security.fail2ban_log")) if log else []:   # last week's first
        e = F2B_EVENT.match(line)
        t = when(e[1]) if e else None
        ip = valid_ip(e[4]) if e else None
        if not ip or (t or 0) < since:
            continue
        if e[3] == "Found":
            f = found.setdefault((e[2], ip), {"ip": ip, "jail": e[2], "count": 0})
            f["count"] += 1
            f["last"] = t
        else:
            events.append({"time": t, "jail": e[2], "action": e[3], "ip": ip})
    return {"jails": jails, "events": events[-60:][::-1], "found_7d": sorted(found.values(), key=lambda f: -f["count"])[:40],
            "checked": int(time.time())}


SSH_FAILED = re.compile(r"Failed password|Invalid user|authentication failure")   # the card's "SSH failures, 24h"
SSH_LINE = re.compile(r"^(\S+) \S+ sshd(?:-session)?\[(\d+)\]: (.*)$")      # OpenSSH 9.8+ logs as sshd-session
SSH_ACCEPT = re.compile(r"^Accepted (?P<method>[\w/-]+) for (?P<user>\S+) from (?P<ip>\S+) port \d+ ssh2(?:: \S+ (?P<fp>SHA256:\S+)(?: .*)?)?$")
# User names can contain spaces (even "x from 192.168.1.5 port 1"), so the IP is taken from the END of the line.
_END = r" port \d+(?: ssh2)?(?: \[preauth\])?$"
SSH_FAIL = [re.compile(x + _END) for x in (               # someone tried to log in (with a user name)
    r"^Invalid user (?P<user>.*?) from (?P<ip>\S+)",
    r"^Failed \S+ for (?:invalid user )?(?P<user>.*?) from (?P<ip>\S+)",
    r"^Connection closed by (?:authenticating|invalid) user (?P<user>.*?) (?P<ip>\S+)",
    r"^Disconnected from (?:authenticating|invalid) user (?P<user>.*?) (?P<ip>\S+)",
    r"^error: maximum authentication attempts exceeded for (?:invalid user )?(?P<user>.*?) from (?P<ip>\S+)")]
SSH_PROBE = [re.compile(x) for x in (                     # connected but left before giving a user name (scanners)
    r"^Connection (?:closed|reset) by (?P<ip>\S+)" + _END,
    r"^banner exchange: Connection from (?P<ip>\S+) port \d+: ",
    r"^Unable to negotiate with (?P<ip>\S+) port \d+: ")]
KEY_TYPE = re.compile(r"(?:ssh-[\w-]+|ecdsa-sha2-[\w-]+|sk-[\w@.-]+)")


def key_names(user, cache):
    """SHA256 fingerprint -> comment of a user's authorized_keys, so a login shows WHICH key was used (e.g. the phone's).
    The file is read without following a symlink and fingerprinted here (no ssh-keygen on a user-controlled path)."""
    if user not in cache:
        names = {}
        try:
            data = read_regular(os.path.join(pwd.getpwnam(user).pw_dir, ".ssh", "authorized_keys"), 1 << 20)
        except (KeyError, CheckFailed):
            data = b""
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            for i, part in enumerate(parts[:-1]):          # options (from="...",command="...") can come before the key type
                if KEY_TYPE.fullmatch(part):
                    try:
                        blob = base64.b64decode(parts[i + 1], validate=True)
                    except ValueError:
                        break
                    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
                    names[fp] = clip(" ".join(parts[i + 2:]) or "(no comment)", 60)
                    break
        cache[user] = names
    return cache[user]


def ssh_detail():
    unit = SEC.get("ssh_unit", "ssh")
    if not unit or not have("journalctl"):
        return None
    units = ["-u", unit] + (["-u", "sshd"] if unit == "ssh" else [])
    lines = ssh_journal(units, 60)
    logins, by_ip, seen, sources, keys = [], {}, set(), {}, {}
    day_ago = time.time() - 86400
    for line in lines:
        m = SSH_LINE.match(line)
        if not m:
            continue
        t, pid, msg = when(m[1]), m[2], m[3]
        a = SSH_ACCEPT.match(msg)
        if a:
            ip = valid_ip(a["ip"])
            if not ip:
                continue
            user = clip(a["user"], 64)
            key = key_names(a["user"], keys).get(a["fp"], a["fp"][:20] + "…") if a["fp"] else ""
            logins.append({"time": t, "raw": m[1][:40], "user": user, "ip": ip, "method": clip(a["method"], 40), "key": key})
            g = sources.setdefault((user, ip, key), {"user": user, "ip": ip, "key": key, "method": clip(a["method"], 40), "count": 0, "first": t})
            g["count"] += 1
            g["last"] = t
            continue
        hit, probe = None, False
        for rx in SSH_FAIL:
            hit = rx.match(msg)
            if hit:
                break
        if not hit:
            for rx in SSH_PROBE:
                hit = rx.match(msg)
                if hit:
                    probe = True
                    break
        ip = valid_ip(hit["ip"]) if hit else None
        if not ip:
            continue
        g = by_ip.setdefault(ip, {"ip": ip, "attempts": 0, "probes": 0, "attempts_24h": 0, "users": [], "first": t, "last": t})
        g["last"] = t
        user = None if probe else clip(hit["user"] or "(empty)", 32)
        if user and user not in g["users"] and len(g["users"]) < 10:
            g["users"].append(user)
        key = (pid, ip)                                    # one connection logs several lines: count it once
        if key in seen:
            if not probe and (pid, ip, "login") not in seen:   # a scan-looking line came first, then a user name: a login try
                seen.add((pid, ip, "login"))
                g["probes"] -= 1
                g["attempts"] += 1
                g["attempts_24h"] += (t or 0) > day_ago
            continue
        seen.add(key)
        if probe:
            g["probes"] += 1
        else:
            seen.add((pid, ip, "login"))
            g["attempts"] += 1
            g["attempts_24h"] += (t or 0) > day_ago
    fails = sorted(by_ip.values(), key=lambda g: (-g["attempts"], -g["probes"], -(g["last"] or 0)))
    # every (user, IP, key) that logged in during the week, so a rare login can't hide behind thousands of routine ones
    src = sorted(sources.values(), key=lambda g: -(g["last"] or 0))
    return {"logins": logins[-30:][::-1], "login_count": len(logins), "sources": src[:100], "failed_ips": fails[:60],
            "failed_ip_count": len(fails), "attempts_7d": sum(g["attempts"] for g in fails),
            "attempts_24h": sum(g["attempts_24h"] for g in fails), "probes_7d": sum(g["probes"] for g in fails),
            "truncated": len(lines) >= JOURNAL_MAX_LINES, "checked": int(time.time())}


AUTHELIA_TABLES = ("authentication_logs", "banned_user", "banned_ip")


def plain_tables(con, names):
    """{name: "table"} for each of names that is a plain table: virtual tables (FTS, R-tree) also say "table" in
    sqlite_schema but run their module's C code on the file's bytes, and generated columns run expressions stored in the
    file; a crafted database could hold root's query far past its time limit with either (the progress handler only
    runs between steps). Anything else: "other"."""
    out = {}
    for name, kind, sql in con.execute(f"SELECT name, type, sql FROM sqlite_schema WHERE name IN ({','.join('?' * len(names))})",
                                       names).fetchall():
        plain = kind == "table" and isinstance(sql, str) and re.match(r"\s*CREATE\s+TABLE\b", sql, re.I) is not None
        if plain:   # (only now: pragma_table_xinfo on a virtual table would run its module)
            plain = all(h == 0 for (h,) in con.execute("SELECT hidden FROM pragma_table_xinfo(?)", (name,)))
        out[name] = "table" if plain else "other"
    return out


def authelia_detail():
    """Sign-ins and lockouts from Authelia's SQLite database. Its folder is writable by the Authelia container, so SQLite
    never opens anything there: the file's bytes are read (no symlinks, no FIFOs, size-capped) into an in-memory copy,
    queried with SQLite's defensive settings (no triggers, no views, untrusted schema), plain tables only, row limits and
    a 5-second time limit. A copy also means Authelia's own database is never locked by us."""
    db = SEC.get("authelia_db", "")
    if not db:
        return None
    now = int(time.time())
    if not no_symlink(db):
        raise CheckFailed("authelia_db goes through a symlink")
    data = bytearray(read_regular(db, 16 << 20, no_links="security.authelia_db" in APPROVED_NOW))
    if data[18:20] == b"\x02\x02":                        # a WAL-mode header can't be used in memory: read it as rollback mode
        data[18:20] = b"\x01\x01"                          # (sign-ins still in the -wal file show up after its next checkpoint)
    con = sqlite3.connect(":memory:")
    con.text_factory = lambda b: b.decode("utf-8", "replace")   # (a row that isn't UTF-8 mustn't blank the whole view)
    try:
        if hasattr(con, "setconfig"):                     # Python 3.12+
            for opt, on in (("SQLITE_DBCONFIG_DEFENSIVE", True), ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
                            ("SQLITE_DBCONFIG_ENABLE_TRIGGER", False), ("SQLITE_DBCONFIG_ENABLE_VIEW", False)):
                if hasattr(sqlite3, opt):
                    con.setconfig(getattr(sqlite3, opt), on)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        deadline = time.monotonic() + 5
        con.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)   # a true return stops the query
        con.deserialize(bytes(data))
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA cell_size_check = ON")       # (SQLite's advice for files from someone else)
        kinds = plain_tables(con, AUTHELIA_TABLES)
        if kinds.get("authentication_logs") != "table":
            raise CheckFailed("authentication_logs isn't a table in that database")
        rows = con.execute("SELECT substr(time, 1, 40), successful, banned, substr(username, 1, 100), substr(auth_type, 1, 8), "
                           "substr(remote_ip, 1, 45), substr(request_uri, 1, 300) FROM authentication_logs "
                           "ORDER BY id DESC LIMIT 5000").fetchall()
        bans = []
        for table, col in (("banned_user", "username"), ("banned_ip", "ip")):
            if kinds.get(table) != "table":               # older Authelia: no ban tables
                continue
            for t, exp, src, reason, who in con.execute(
                    f"SELECT substr(time, 1, 40), substr(expires, 1, 40), substr(source, 1, 20), substr(reason, 1, 100), substr({col}, 1, 100) "
                    f"FROM {table} WHERE revoked = 0 AND expired IS NULL ORDER BY id DESC LIMIT 50"):
                e = when(exp) if exp else None
                if e is None or e > now:
                    bans.append({"kind": "user" if col == "username" else "ip", "who": clip(who, 64), "since": when(t), "until": e,
                                 "source": clip(src or "", 20), "reason": clip(reason or "", 100)})
    except sqlite3.Error as e:
        raise CheckFailed(f"Authelia's database: {clip(e, 100)}")
    finally:
        con.close()
    week, day = now - 7 * 86400, now - 86400
    events, by_user, by_ip = [], {}, {}
    n = {"ok_24h": 0, "fail_24h": 0, "ok_7d": 0, "fail_7d": 0}
    for t, ok, banned, user, kind, ip, uri in rows:
        ts = when(t)
        if ts is None or ts < week:
            continue
        ok, kind = bool(ok), clip(kind, 8)
        if not ok or kind == "1FA":                       # a sign-in = its password step (2FA adds a second row for the same sign-in)
            for span in ("7d",) + (("24h",) if ts > day else ()):
                n[("ok_" if ok else "fail_") + span] += 1
        events.append({"time": ts, "ok": ok, "banned": bool(banned), "user": clip(user, 64), "factor": kind,
                       "ip": valid_ip(ip or "") or clip(ip or "", 45), "site": urlhost(uri)})
        if not ok:
            for key, d in ((clip(user, 64), by_user), (valid_ip(ip or "") or "?", by_ip)):
                g = d.setdefault(key, {"key": key, "count": 0, "last": ts})
                g["count"] += 1
                g["last"] = max(g["last"], ts)
    events.sort(key=lambda e: -e["time"])                   # newest first
    top = lambda d: sorted(d.values(), key=lambda g: (-g["count"], -g["last"]))[:30]
    return {"events": events[:60], **n, "fail_by_user": top(by_user), "fail_by_ip": top(by_ip), "bans": bans,
            "truncated": len(rows) >= 5000, "checked": now}


def urlhost(uri):
    m = re.match(r"https?://([^/?#\s]+)", uri or "")
    return clip(m[1], 80) if m else ""





def procs_detail():
    if not SEC.get("process_watch", True):
        return None
    names = list(SEC.get("bad_process_names", MINERS))
    me = str(os.getpid())
    found = [clip(p.split(None, 2)[1], 40) for p in out(["ps", "-eo", "pid=,comm=,args="]).splitlines()
             if len(p.split(None, 2)) > 1 and any(n in p.lower() for n in names) and p.split(None, 1)[0] != me]
    fresh = time.time() - TOP_AT[0] < 180
    d = {"names": names, "found": found, "top": list(LAST_TOP) if fresh else [], "top_at": int(TOP_AT[0]) or None,
         "checked": int(time.time()), "watcher": None}
    w = SEC.get("watcher")
    if isinstance(w, dict) and w:   # {} = off, like not set
        beat, alerts = w.get("heartbeat", ""), w.get("alerts", "")
        every = float(w.get("every_minutes", 5))
        beat_st = regular_stat(beat, "security.watcher" in APPROVED_NOW) if beat else None
        last = int(beat_st.st_mtime) if beat_st else None
        hits, kinds = [], w.get("kinds", {}) if isinstance(w.get("kinds"), dict) else {}   # alert-line prefix -> label
        if alerts:
            for line in tail_lines(alerts, 200_000, "security.watcher")[-200:]:
                kind = line.split(":", 1)[0]
                hits.append({"kind": clip(kinds.get(kind, kind), 60), "detail": redact(clip(line.split(":", 1)[-1], 160))})
        d["watcher"] = {"label": clip(w.get("label", "watch script"), 60), "last_run": last, "every_minutes": every,
                        "late": last is None or time.time() - last > every * 60 * 3, "checks": [clip(c, 200) for c in w.get("checks", [])][:20],
                        "alerts": hits[::-1][:60], "alert_count": len(hits)}   # (the alerts file is touched every run: no date)
    return d


CRON_FOLDERS = ("/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly")


def cron_detail():
    """Every active crontab line, marked approved (in the snapshot the dash's cron check compares with) or not;
    plus the cron.daily-style folders and systemd timers, which that check doesn't cover (shown for information)."""
    if not SEC.get("cron_watch", True):
        return None
    snap = cron_files()
    appr = load_approved() or {"snap": {}}
    old = appr.get("snap", {})
    files = []
    for f in sorted(set(snap) | set(old)):
        user_file = f.startswith("/var/spool/cron")
        entries = []
        for line in snap.get(f, []):
            fld = line.split()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", line):        # PATH=..., SHELL=...: a change here can hijack every job
                sched, user, n = "", "", 0
            elif line.startswith("@"):
                sched, n = fld[0], 1
                user = os.path.basename(f) if user_file else (fld[1] if len(fld) > 1 else "")
                n += 0 if user_file else 1
            else:
                sched, n = " ".join(fld[:5]), 5
                user = os.path.basename(f) if user_file else (fld[5] if len(fld) > 5 else "")
                n += 0 if user_file else 1
            cmd = re.sub(r"^\s*(?:\S+\s+){%d}" % n, "", line, count=1) if n else line   # the rest of the line, spacing kept
            entries.append({"when": clip(sched, 40), "user": clip(user, 32), "cmd": redact(cmd), "setting": n == 0,
                            "approved": line in old.get(f, [])})
        removed = [redact(l) for l in old.get(f, []) if l not in snap.get(f, [])]
        files.append({"file": f, "user": os.path.basename(f) if user_file else "", "entries": entries[:100], "removed": removed[:20],
                      "new_file": f not in old, "deleted": f not in snap})
    folders = []
    for d in CRON_FOLDERS:
        try:
            names = sorted(n for n in os.listdir(d) if not n.startswith("."))
        except OSError:
            continue
        folders.append({"folder": d, "scripts": [{"name": clip(n, 80), "mtime": int(os.lstat(os.path.join(d, n)).st_mtime)} for n in names][:60]})
    timers = []
    try:
        for t in json.loads(run(["systemctl", "list-timers", "--all", "--output=json", "--no-pager"]) or "[]")[:80]:
            us = lambda k: int(t[k] / 1e6) if isinstance(t.get(k), (int, float)) and t[k] > 0 else None
            timers.append({"unit": clip(t.get("unit") or "", 80), "runs": clip(t.get("activates") or "", 80), "next": us("next"), "last": us("last")})
    except ValueError:
        pass
    return {"files": files, "approved_at": appr.get("approved_at"), "folders": folders, "timers": timers,
            "unapproved": sum(not e["approved"] for f in files for e in f["entries"]),   # settings lines count too
            "removed": sum(len(f["removed"]) for f in files), "checked": int(time.time())}


def locks_detail():
    files = SEC.get("immutable_files", [])
    if not files:
        return None
    res = []
    for f in files:
        exists = os.path.lexists(f)
        attrs = out(["lsattr", "-d", f]).split() if exists and not os.path.islink(f) else []   # -d: a folder's own flag
        res.append({"path": f, "exists": exists, "locked": bool(attrs) and "i" in attrs[0], "attrs": attrs[0] if attrs else "",
                    "mtime": int(os.lstat(f).st_mtime) if exists else None, "symlink": os.path.islink(f)})
    return {"files": res, "checked": int(time.time())}


# Backup sizes need du over hundreds of GB: one du for all destinations (so hard-linked snapshots count once, in the
# first folder listed), in a background thread at idle disk/CPU priority, once the backup log has been quiet for 15 min
# after it changed (so after the night's run, not during it) or when a size is a day old. Kept in sizes.json, so a
# restart doesn't measure again.
SIZES = {}
SIZES_FILE = os.path.join(OUT_DIR, "sizes.json")
_sizes = {"busy": False, "log_mtime": None}


def load_sizes():
    try:
        d = json.load(open(SIZES_FILE))
        SIZES.update({k: v for k, v in d.get("sizes", {}).items() if isinstance(v, dict)})
        _sizes["log_mtime"] = d.get("log_mtime")
    except (OSError, ValueError, AttributeError):
        pass


def _measure(paths, log_mtime):
    try:
        t0 = time.monotonic()
        got = dict((p, int(n)) for n, p in re.findall(r"^(\d+)\t(.*)$", run(
            ["nice", "-n", "19", "ionice", "-c", "3", "du", "-sB1", "--one-file-system", *paths], 3600), flags=re.M))
        for p in paths:
            SIZES[p] = {"bytes": got.get(p), "at": int(time.time()), "secs": round(time.monotonic() - t0)}
        write_output({"sizes": SIZES, "log_mtime": log_mtime}, SIZES_FILE)
    except Exception as e:
        print(f"backup sizes: {type(e).__name__}: {e}", flush=True)
    finally:
        _sizes["busy"] = False


def measure_sizes(paths, log_mtime):
    paths = list(dict.fromkeys(paths))                    # the same folder twice would come back once from du
    if _sizes["busy"] or not paths:
        return
    now = time.time()
    quiet = log_mtime is None or now - log_mtime > 900
    oldest = min((SIZES[p].get("at") or 0) if p in SIZES else 0 for p in paths)
    if quiet and (log_mtime != _sizes["log_mtime"] or now - oldest > 86400):
        _sizes.update(busy=True, log_mtime=log_mtime)
        try:
            threading.Thread(target=_measure, args=(paths, log_mtime), daemon=True).start()
        except RuntimeError:
            _sizes["busy"] = False


def backup_detail():
    """Each backup job from [[backup.jobs]]: its last success and what it reported (from the log), how long it took,
    its files and destination (free space, size), and whether it's overdue, partly done or failed."""
    jobs_cfg, log = BAK.get("jobs", []), BAK.get("log", "")
    if not jobs_cfg:
        return None
    lines = (tail_lines(log + ".1", 4_000_000, "backup.log") + tail_lines(log, 4_000_000, "backup.log")) if log else []   # + last rotation's
    log_st = regular_stat(log, "backup.log" in APPROVED_NOW) if log else None
    log_mtime = log_st.st_mtime if log_st else None
    now = time.time()
    stamped = [(when(l), l) for l in lines]
    run_info, run_lines = None, []
    day = next((l[:10] for t, l in reversed(stamped) if t), None)
    if day:
        start = next(i for i, (t, l) in enumerate(stamped) if l.startswith(day))
        run_lines = lines[start:]
        times = [t for t, _ in stamped[start:] if t]
        bad_re = BAK.get("failure_regex", r"(?i)\berror|failed\b")
        warn = lambda l: re.search(r"(?i)warning|vanished", l)
        run_info = {"start": times[0], "end": times[-1], "errors": [redact(l) for l in run_lines if bad_re and re.search(bad_re, l)][:10],
                    "warnings": [redact(l) for l in run_lines if warn(l)][:6], "warning_count": sum(bool(warn(l)) for l in run_lines)}
    max_age = float(BAK.get("max_age_hours", 26))
    jobs, sized = [], []
    for j in jobs_cfg:
        job = {"label": clip(j.get("label", "backup"), 80), "every": clip(j.get("every", ""), 160), "remote": clip(j.get("remote", ""), 160)}
        limit = float(j.get("max_age_hours", max_age))
        job["max_age_hours"] = limit
        problems = []
        try:
            hits, prev = [], None
            rx = re.compile(j["regex"]) if j.get("regex") else None
            for t, l in stamped:
                if t is None:
                    continue
                m = rx.search(l) if rx else None
                if m:
                    took = t - prev if prev and 0 <= t - prev < 6 * 3600 else None   # seconds since the log's line before
                    hits.append({"time": t, "took": took,
                                 **{("took_text" if k == "took" else k): redact(clip(v, 120)) for k, v in m.groupdict().items() if v}})
                prev = t
            last = hits[-1] if hits else None
            job["last_ok"] = last["time"] if last else None
            if last:
                lday = time.strftime("%Y-%m-%d", time.localtime(last["time"]))
                today = [h for h in hits if time.strftime("%Y-%m-%d", time.localtime(h["time"])) == lday]
                job["items"] = today[-15:]
                first = today[0]                          # the whole step: from the line before its first hit to its last hit
                job["took"] = last["time"] - first["time"] + (first["took"] or 0) if len(today) > 1 else last["took"]
                need = int(j.get("min_count", 1))
                running = lday == time.strftime("%Y-%m-%d") and log_mtime and now - log_mtime < 900   # the run may still be going
                if len(today) < need and not running:
                    problems.append(f"only {len(today)} of {need} done on {lday}")
            if j.get("fail_regex"):                       # its own failure line in the latest run (e.g. inside "ERRORS: ...")
                bad = [l for l in run_lines if re.search(j["fail_regex"], l)]
                if bad:
                    problems.append("failed in the last run")
                    job["fail_lines"] = [redact(l) for l in bad][:5]
        except re.error as e:
            problems.append(f"bad regex in collector.toml: {e}")
            job["last_ok"] = None
        files, missing = [], []
        for g in j.get("files", []):
            found = [f for f in glob.glob(g) if page_path("backup.jobs", f)]
            if not found and not re.search(r"[*?\[]", g):  # a file named exactly (not a pattern) that isn't there
                missing.append(clip(g, 200))
            for f in found:
                try:
                    st = os.lstat(f)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):              # no symlinks, no FIFOs
                    files.append((f, st.st_mtime, st.st_size, not re.search(r"[*?\[]", g)))
        if files:
            files.sort(key=lambda x: x[1])
            job["files"] = {"count": len(files), "newest": int(files[-1][1]), "oldest": int(files[0][1]), "bytes": sum(x[2] for x in files),
                            "list": [{"name": clip(os.path.basename(f), 90), "mtime": int(mt), "bytes": sz} for f, mt, sz, _ in files[-30:][::-1]]}
            if not j.get("regex"):
                job["last_ok"] = int(files[-1][1])
            old = [f for f, mt, _, exact in files if exact and now - mt > limit * 3600]   # each named file must be fresh
            if old:
                problems.append(f"{len(old)} older than {limit:g} h")
                job["old_files"] = [clip(f, 200) for f in old][:30]
        if missing:
            problems.append(f"{len(missing)} missing")
        job["missing"] = missing[:30]
        dest = j.get("dest", "")
        if dest and os.path.isdir(dest) and page_path("backup.jobs", dest):
            st = os.statvfs(dest)
            job["dest"] = {"path": dest, "free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
            if j.get("size"):
                sized.append(dest)
                job["dest"]["size"] = SIZES.get(dest)
        elif dest:
            job["dest"] = {"path": dest, "missing": True}
            problems.append("destination missing")
        if job.get("last_ok") is None and "bad regex" not in " ".join(problems):
            problems.insert(0, "never ran")
        elif job.get("last_ok") and now - job["last_ok"] > limit * 3600:
            problems.insert(0, f"over {limit:g} h old")
        job["problems"] = problems
        job["stale"] = bool(problems)
        jobs.append(job)
    measure_sizes(sized, log_mtime)
    return {"run": run_info, "jobs": jobs, "max_age_hours": max_age, "checked": int(now)}


def security_detail():
    d = {"updated": int(time.time())}
    for key, fn in (("firewall", firewall_detail), ("fail2ban", fail2ban_detail), ("ssh", ssh_detail), ("authelia", authelia_detail),
                    ("procs", procs_detail), ("cron", cron_detail), ("locks", locks_detail), ("backups", backup_detail)):
        try:
            d[key] = fn()
        except Exception as e:                            # one broken check shows as an error; the others still run
            msg = str(e) if isinstance(e, CheckFailed) else f"{type(e).__name__}: {e}"
            d[key] = {"error": clip(msg, 200), "checked": int(time.time())}
    return d


_detail = {"busy": False}


def detail_round():
    """One round of details, in its own thread (started once a minute unless the last one is still running)."""
    try:
        write_output(security_detail(), DETAIL)
    except Exception as e:
        print(f"security details: {type(e).__name__}: {e}", flush=True)
    finally:
        _detail["busy"] = False

def write_output(data, path=OUT):
    """Atomically replace hw.json (or sec-detail.json), readable by root + the dash container's group only."""
    tmp = path + ".tmp"
    with create_private(tmp) as f:
        json.dump(data, f)
    try:
        os.chown(tmp, 0, DASH_UID)
    except PermissionError:               # not root (testing): leave ownership as is
        pass
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


# ---------- settings from the dash's settings page ----------
# The dash writes what its settings page changed to req/collector-settings.json:
#   {"v": 3, "id": "<random>", "changes": {"interval": {"from": "<fingerprint>", "to": 9}, ...}}
# (from = the fingerprint of the value the page showed as in effect, "" if it wasn't set; see digest())
# The dash runs in a container and must never steer root, so the COLLECTOR decides, key by key, and checks every value:
#   SAFE      applied by itself: switches, bounded numbers, labels, and choices from what the collector detected itself
#             (a real mount point, an existing network interface, a jail fail2ban lists, ...). These only change what is
#             measured or shown, which a hijacked dash controls anyway.
#   APPROVAL  paths, patterns and the watcher/backup tables: they make root read files or run regexes, so they wait for a
#             person at the host: `sudo homelab-dash-collector --apply` shows the change and asks.
# A request is handled once (by its hash), and a change only while the setting still has the value the page showed
# (otherwise it was changed on the host meanwhile, and the page has to be reloaded first). Never from the page: out_dir,
# dash_uid. Settings in effect = collector.toml, then what --apply wrote (collector.page.toml, root only), then the page's
# safe values (page-state.json, root only). Both remember collector.toml's value they replaced: when that setting is
# edited in collector.toml later, the edit wins (the last change wins, wherever it was made).
PAGE_REQ = os.path.join(REQ_DIR, "collector-settings.json")
PAGE_STATE_FILE = os.path.join(OUT_DIR, "page-state.json")
PAGE_LAST_FILE = os.path.join(OUT_DIR, "page-last")      # the hash of the last request handled (tiny, written on its own)
PAGE_LOCK = os.path.join(OUT_DIR, ".page.lock")
PAGE_MAX = 256 * 1024
STATE_MAX = 1024 * 1024
MISSING = object()
NO_READ = ("/proc", "/sys", "/dev", os.path.normpath(OUT_DIR))   # never a page path: kernel files, and req/ (the dash writes there)


class PageInvalid(Exception):
    pass


def shown(v):
    """Text for root's terminal: printable ASCII as it is, everything else escaped (\\u....), so nothing from the page
    can move the cursor, repaint the screen, set the clipboard, hide itself or pass for other letters."""
    return "".join(c if " " <= c <= "~" and c != "\\" else "\\\\" if c == "\\"
                   else f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}" for c in str(v))


def same(a, b):
    """Equal values, kind for kind: True isn't 1, 26 is 26.0, not set is None."""
    a, b = (None if x is MISSING else x for x in (a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _text(v, n, what):
    if not isinstance(v, str) or len(v) > n or not v.isprintable():
        raise PageInvalid(f"{what}: text too long or with control characters")
    return v.strip()


def _bool(what):
    def check(v, det):
        if not isinstance(v, bool):
            raise PageInvalid(f"{what}: must be on or off")
        return v
    return check


def _int(lo, hi, what):
    def check(v, det):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise PageInvalid(f"{what}: a whole number from {lo} to {hi}")
        return v
    return check


def _num(lo, hi, what):
    def check(v, det):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise PageInvalid(f"{what}: a number from {lo} to {hi}")
        return v
    return check


def _pick(kind, what, blank=True):
    """One of the values the collector found itself (det[kind]), or "" (off)."""
    def check(v, det):
        if not isinstance(v, str) or not ((blank and v == "") or v in det.get(kind, [])):
            raise PageInvalid(f"{what}: {str(v)[:60]!r} isn't one found on this machine")
        return v
    return check


def _reaches(pattern, folder):
    """Could this path or pattern name something in folder (or folder itself)? Compared part by part, so a wildcard
    like /p*/self or /var/lib/*/req counts too."""
    p, d = pattern.strip("/").split("/"), folder.strip("/").split("/")
    return len(p) >= len(d) and all(fnmatch.fnmatchcase(dp, pp) for dp, pp in zip(d, p))


def _path(v, what, glob_ok=False, blank=False):
    """An absolute, normalized path (no '..', no '//', nothing unprintable), or "" (off) where blank allows it. Any
    other path, since a person approves it, except kernel files and the collector's own folder (the dash writes in
    its req/ folder: a link or a FIFO there must never become something root reads)."""
    v = _text(v, 512, what)
    if blank and v == "":
        return v
    if not v.startswith("/") or os.path.normpath(v) != v:
        raise PageInvalid(f"{what}: {v[:80]!r} isn't a plain absolute path")
    if not glob_ok and re.search(r"[*?\[]", v):
        raise PageInvalid(f"{what}: {v[:80]!r}: no wildcards here")
    if re.search(r"[*?\[]", v.split("/")[1]):
        raise PageInvalid(f"{what}: {v[:80]!r}: wildcards only after the first folder")
    if any(_reaches(v, d) for d in NO_READ):
        raise PageInvalid(f"{what}: {v[:80]!r}: not in /proc, /sys, /dev or the collector's own folder")
    return v


def _regex(v, what):
    v = _text(v, 300, what)
    try:
        re.compile(v)
    except re.error as e:
        raise PageInvalid(f"{what}: not a valid pattern ({e})")
    return v


def _list(v, n, what, item):
    if not isinstance(v, list) or len(v) > n:
        raise PageInvalid(f"{what}: a list of at most {n}")
    return [item(x) for x in v]


def _table(v, what, fields):
    """{key: check(value)} for the keys given; unknown keys are refused (a typo shouldn't vanish quietly)."""
    if not isinstance(v, dict):
        raise PageInvalid(f"{what}: must be a table")
    bad = sorted(set(v) - set(fields))
    if bad:
        raise PageInvalid(f"{what}: unknown {', '.join(map(repr, bad))[:80]}")
    return {k: fields[k](x) for k, x in v.items()}


def _unique(items, what):
    """Each entry needs its own name: it's what the page and --apply show (two with one name couldn't be told apart)."""
    names = [i.get("label", "") for i in items]
    dup = next((n for n in names if names.count(n) > 1), None)
    if dup is not None:
        raise PageInvalid(f"{what}: two are called {dup[:60]!r}: give each its own name")
    return items


def _mount(m, det):
    if not isinstance(m, str) or m not in det.get("mounts", []):
        raise PageInvalid(f"mounts: {str(m)[:80]!r} isn't a mount point on this machine")
    return m


def _labels(v, det):
    if not isinstance(v, dict) or len(v) > 64:
        raise PageInvalid("mount labels: at most 64")
    return {(k if k in det.get("named", []) else _mount(k, det)): _text(x, 40, f"label for {str(k)[:60]}") for k, x in v.items()}


def _name(x):
    if not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", x):
        raise PageInvalid(f"process names: {str(x)[:40]!r} isn't a process name")
    return x


def _kinds(x):
    if not isinstance(x, dict) or len(x) > 30:
        raise PageInvalid("alert kinds: a table of at most 30")
    for k in x:
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", k):
            raise PageInvalid("alert kind: letters, digits, _ and - only")
    return {k: _text(d, 80, f"label for alert kind {k}") for k, d in x.items()}


def _watcher(v, det):
    return _table(v, "watch script", {
        "label": lambda x: _text(x, 80, "watch script name"),
        "heartbeat": lambda x: _path(x, "watch script heartbeat file"),
        "alerts": lambda x: _path(x, "watch script alerts file"),
        "every_minutes": lambda x: _int(1, 1440, "watch script runs every (minutes)")(x, det),
        "checks": lambda x: _list(x, 30, "watch script checks", lambda c: _text(c, 200, "watch script check")),
        "kinds": _kinds,
    })


def _job(j, det):
    return _table(j, "backup job", {
        "label": lambda x: _text(x, 80, "backup job name"),
        "regex": lambda x: _regex(x, "backup job 'done' line"),
        "fail_regex": lambda x: _regex(x, "backup job 'failed' line"),
        "min_count": lambda x: _int(1, 1000, "backup job lines needed")(x, det),
        "files": lambda x: _list(x, 30, "backup job files", lambda f: _path(f, "backup job file", glob_ok=True)),
        "dest": lambda x: _path(x, "backup job destination"),
        "size": lambda x: _bool("measure the destination's size")(x, det),
        "every": lambda x: _text(x, 160, "backup job schedule"),
        "max_age_hours": lambda x: _num(1, 8760, "backup job overdue after (hours)")(x, det),
        "remote": lambda x: _text(x, 200, "backup job note"),
    })


SAFE_KEYS = {
    ("interval",): _int(2, 60, "update interval (s)"),
    ("gpu",): _bool("GPU"),
    ("usage",): _bool("CPU and memory breakdown"),
    ("net_if",): _pick("ifaces", "network interface"),   # "" = the one with the default route
    ("mounts",): lambda v, det: _list(v, 64, "mounts", lambda m: _mount(m, det)),   # [] = every drive it finds
    ("mount_labels",): _labels,
    ("security", "process_watch"): _bool("process watch"),
    ("security", "cron_watch"): _bool("cron watch"),
    ("security", "firewall"): _bool("firewall"),
    ("security", "details"): _bool("detail views"),
    ("security", "fail2ban_jail"): _pick("jails", "fail2ban jail"),
    ("security", "ssh_unit"): _pick("ssh_units", "SSH service"),
    ("security", "authelia_container"): _pick("authelia", "Authelia container"),
    ("security", "bad_process_names"): lambda v, det: _list(v, 50, "process names", _name),
    ("backup", "max_age_hours"): _num(1, 8760, "backups overdue after (hours)"),
}
APPROVAL_KEYS = {   # "" / [] / {} turn a setting off: that too waits for --apply (one rule, easy to explain)
    ("security", "authelia_db"): lambda v, det: _path(v, "Authelia database", blank=True),
    ("security", "fail2ban_log"): lambda v, det: _path(v, "fail2ban log", blank=True),
    ("security", "immutable_files"): lambda v, det: _list(v, 100, "locked files", lambda f: _path(f, "locked file")),
    ("security", "watcher"): _watcher,
    ("backup", "log"): lambda v, det: _path(v, "backup log", blank=True),
    ("backup", "success_regex"): lambda v, det: _regex(v, "backup 'ok' line"),
    ("backup", "failure_regex"): lambda v, det: _regex(v, "backup 'failed' line"),
    ("backup", "checks"): lambda v, det: _unique(_list(v, 30, "backup checks", lambda c: _table(c, "backup check", {
        "label": lambda x: _text(x, 80, "backup check name"),
        "regex": lambda x: _regex(x, "backup check pattern"),
        "min_count": lambda x: _int(1, 1000, "backup check lines needed")(x, det)})), "backup checks"),
    ("backup", "artifacts"): lambda v, det: _unique(_list(v, 30, "backup files", lambda a: _table(a, "backup file", {
        "label": lambda x: _text(x, 80, "backup file name"),
        "glob": lambda x: _path(x, "backup file pattern", glob_ok=True)})), "backup files"),
    ("backup", "jobs"): lambda v, det: _unique(_list(v, 30, "backup jobs", lambda j: _job(j, det)), "backup jobs"),
}
PATHS = {".".join(p): p for p in (*SAFE_KEYS, *APPROVAL_KEYS)}   # "security.watcher" -> ("security", "watcher")
LABELS = {   # what --apply calls them (the page uses the same words)
    "security.authelia_db": "Authelia's database", "security.fail2ban_log": "fail2ban's log",
    "security.immutable_files": "Files that must stay locked", "security.watcher": "Your own watch script",
    "backup.log": "Your backup script's log", "backup.success_regex": "A line that means the run went fine",
    "backup.failure_regex": "A line that means it failed", "backup.checks": "Backup checks",
    "backup.artifacts": "Backup files", "backup.jobs": "Backup jobs",
}


def _get(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return MISSING
        d = d[k]
    return d


def _put(d, path, v):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = v


def layer(base, over, keys):
    """A copy of base with over's values for these keys (and only these)."""
    out = copy.deepcopy(base)
    for path in keys:
        v = _get(over, path)
        if v is not MISSING:
            _put(out, path, copy.deepcopy(v))
    return out


def _flat(d, keys):
    return {".".join(p): _get(d, p) for p in keys if _get(d, p) is not MISSING}


@contextlib.contextmanager
def page_lock():
    """The service and --apply both update page-state.json: one at a time."""
    fd = os.open(PAGE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def new_state():
    return {"v": 2, "last": "", "safe": {}, "pending": {}, "superseded": {}, "rejected": [], "rejected_more": 0, "rejected_at": 0}


def read_last():
    try:
        st = os.stat(PAGE_LAST_FILE)
        if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o077):
            return ""
        h = read_regular(PAGE_LAST_FILE, 100).decode("ascii").strip()
    except (CheckFailed, OSError, UnicodeDecodeError):
        return ""
    return h if re.fullmatch(r"[0-9a-f]{64}", h) else ""


def read_state():
    """What the page set so far (root-only files: ignored if anyone else could have written them). Every part is
    checked for its shape, so a damaged file means "nothing from the page", never a crash."""
    s = new_state()
    s["last"] = read_last()
    try:
        raw = read_regular(PAGE_STATE_FILE, STATE_MAX)
        st = os.stat(PAGE_STATE_FILE)
        if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o077):
            return s
        d = json.loads(raw)
    except (CheckFailed, OSError, ValueError, RecursionError):
        return s
    if not isinstance(d, dict) or d.get("v") != 2:
        return s
    safe, pend = d.get("safe"), d.get("pending")
    s["safe"] = {k: o for k, o in (safe.items() if isinstance(safe, dict) else ())
                 if k in PATHS and PATHS[k] in SAFE_KEYS and isinstance(o, dict) and "v" in o and set(o) <= {"v", "file"}}
    s["pending"] = {k: o for k, o in (pend.items() if isinstance(pend, dict) else ())
                    if k in PATHS and PATHS[k] in APPROVAL_KEYS and isinstance(o, dict) and set(o) == {"from", "to"}}
    sup = d.get("superseded")
    s["superseded"] = {k: v for k, v in (sup.items() if isinstance(sup, dict) else ())
                       if k in PATHS and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{16}", v)}
    rej = d.get("rejected")
    s["rejected"] = [[str(r[0])[:80], str(r[1])[:300]] for r in (rej if isinstance(rej, list) else [])
                     if isinstance(r, list) and len(r) == 2][:20]
    s["rejected_more"] = d["rejected_more"] if isinstance(d.get("rejected_more"), int) else 0
    s["rejected_at"] = d["rejected_at"] if isinstance(d.get("rejected_at"), int) else 0
    return s


def write_root_only(path, text, mode=0o600):
    """Atomically replace a root-owned file: a fresh temp file in the same folder, flushed to disk, then renamed."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def state_text(s):
    return json.dumps({k: v for k, v in s.items() if k != "last"}, ensure_ascii=False, default=str)   # (a TOML date: text)


def write_state(s, before=None):
    """Save what changed: the state file only when its content did (a request that changes nothing costs one tiny
    write), the last request's hash on its own. before = (state_text, last) as read."""
    text = state_text(s)
    if len(text.encode()) > STATE_MAX:
        raise CheckFailed("the page's settings got too big to keep: nothing changed")
    if before is None or text != before[0]:
        write_root_only(PAGE_STATE_FILE, text)
    if before is None or s["last"] != before[1]:
        write_root_only(PAGE_LAST_FILE, s["last"])


def read_page_toml():
    """(values, against) from collector.page.toml (written by --apply, root only): every approval key in it, and
    collector.toml's values they replaced. A file that can't be read is ignored and reported, never a crash (and
    --apply --undo still works)."""
    PAGE_TOML_ERR[0] = ""
    try:
        d = read_toml(PAGE_TOML, root_only=True)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        why = e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__
        PAGE_TOML_ERR[0] = f"{PAGE_TOML} can't be used ({why}): ignored. sudo homelab-dash-collector --apply --undo starts it over"
        return {}, {}
    against = d.get("_against") if isinstance(d.get("_against"), dict) else {}
    return layer({}, d, APPROVAL_KEYS), {k: v for k, v in against.items() if k in PATHS}


PAGE_TOML_ERR = [""]


def digest(v):
    """A short fingerprint of a setting's value: the page sends back the one it was shown (any value, any shape)."""
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def approved_values(vals, against, state):
    """The approved values that count: collector.toml must still have the value each replaced (a later edit there
    wins), and one that lost that way stays out until --apply writes the file again (restoring an old collector.toml
    mustn't bring it back unseen)."""
    out = {}
    for path in APPROVAL_KEYS:
        k, v = ".".join(path), _get(vals, path)
        if v is not MISSING and same(_get(FILE_CFG, path), against.get(k, MISSING)) and state["superseded"].get(k) != digest(v):
            _put(out, path, v)
    return out


def read_approved(state=None):
    vals, against = read_page_toml()
    return approved_values(vals, against, read_state() if state is None else state)


def effective(approved=None, state=None):
    approved = read_approved() if approved is None else approved
    state = read_state() if state is None else state
    safe = {}
    for k, o in state["safe"].items():
        _put(safe, PATHS[k], o["v"])
    return layer(layer(FILE_CFG, approved, APPROVAL_KEYS), safe, SAFE_KEYS)


def apply_config(cfg, approved=None):
    """Make cfg the settings in effect (every check looks these names up when it runs). approved = the values in it
    that came from the settings page through --apply (their paths are read without following links)."""
    global CFG, SEC, BAK, NET_IF, APPROVED_NOW
    # the detail thread reads these while this runs: each is one assignment, and while the settings change hands, the
    # paths from both the old and the new settings are read link-proof
    new = frozenset(_flat(approved or {}, APPROVAL_KEYS))
    APPROVED_NOW = APPROVED_NOW | new
    CFG, SEC, BAK = cfg, cfg.get("security", {}), cfg.get("backup", {})
    NET_IF = CFG.get("net_if") or default_iface()
    APPROVED_NOW = new


REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "ntfs", "ntfs3", "fuseblk", "vfat", "exfat",
           "nfs", "nfs4", "cifs", "smb3", "fuse.mergerfs", "fuse.sshfs", "bcachefs"}
NOT_DATA = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/var/lib/docker", "/var/snap")


SYSTEM_DIRS = ("/etc", "/usr", "/root", "/tmp", "/var/tmp", "/efi")   # never data drives (systemd's sandbox binds these)


def real_mounts(mountinfo=None):
    """Mount points of real file systems, the same inside the collector's sandbox as outside. It reads the host's own
    table (PID 1's), where systemd's sandbox mounts don't exist; if that can't be read, its own, and then leaves out a
    folder bound onto its own path over the same file system (mountinfo: root field = the mount point, same device as
    the mount it sits on), which is how the sandbox shows its folders. System folders are never drives."""
    if mountinfo is None:
        try:
            with open("/proc/1/mountinfo"):
                pass
            return real_mounts_from("/proc/1/mountinfo", binds=False)
        except OSError:
            mountinfo = "/proc/self/mountinfo"
    return real_mounts_from(mountinfo, binds=True)


def real_mounts_from(mountinfo, binds):
    rows, skip = [], tuple(d + "/" for d in (*NOT_DATA, *SYSTEM_DIRS, os.path.normpath(OUT_DIR)))
    unesc = lambda s: re.sub(r"\\([0-7]{3})", lambda x: chr(int(x.group(1), 8)), s)   # \040 = space
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as f:   # (a path is bytes: not always UTF-8)
            for line in f:
                pre, _, post = line.partition(" - ")
                p, q = pre.split(), post.split()
                if len(p) >= 5 and q:
                    rows.append((unesc(p[4]), unesc(p[3]), p[2], q[0]))   # mount point, root, major:minor, type
    except OSError:
        pass

    def under(m):   # the device of the mount m sits on (the longest mount point above it)
        above = [r for r in rows if r[0] != m and (m.startswith(r[0].rstrip("/") + "/"))]
        return max(above, key=lambda r: len(r[0]))[2] if above else None

    found = []
    for m, root, dev, fstype in rows:
        if fstype not in REAL_FS:
            continue
        if m == "/" or (not (m + "/").startswith(skip) and not (binds and root == m and under(m) == dev)):
            found.append(m)
    return sorted(dict.fromkeys(found))


def detect():
    """What this machine has: the page offers these as choices, and the page's safe choices are checked against them."""
    det = {"mounts": real_mounts(), "ifaces": [], "jails": [], "ssh_units": [], "authelia": [], "authelia_db": {},
           "gpu": have("nvidia-smi"), "firewall": "ufw" if have("ufw") else "firewalld" if have("firewall-cmd") else "",
           "default_iface": default_iface()}
    try:
        det["ifaces"] = sorted(n for n in os.listdir("/sys/class/net")
                               if n != "lo" and not n.startswith("veth") and not re.fullmatch(r"br-[0-9a-f]{12}", n))
    except OSError:
        pass
    if have("fail2ban-client"):
        m = re.search(r"Jail list:\s*(.*)", run(["fail2ban-client", "status"], timeout=10))
        det["jails"] = sorted(j for j in (x.strip() for x in (m.group(1).split(",") if m else [])) if re.fullmatch(r"[\w.-]{1,64}", j))
    units = run(["systemctl", "list-unit-files", "--no-legend", "ssh.service", "sshd.service"], timeout=10)
    det["ssh_units"] = sorted(set(re.findall(r"^(sshd?)\.service", units, re.M)))
    if have("docker"):
        for line in run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"], timeout=15).splitlines():
            name, _, image = line.partition("\t")
            repo = image.split("@")[0].rsplit(":", 1)[0] if ":" in image.split("/")[-1] else image.split("@")[0]
            if repo.split("/")[-1] == "authelia" and re.fullmatch(r"[\w.-]{1,128}", name):
                det["authelia"].append(name)
                src = run(["docker", "inspect", "-f", '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}',
                           name], timeout=15).strip()
                db = os.path.join(src, "db.sqlite3")
                if src.startswith("/") and os.path.isfile(db):
                    det["authelia_db"][name] = db   # offered on the page; it still needs --apply (it's a path)
    det["authelia"].sort()
    return det


_req_seen = {"sig": None, "err": ""}


def take_request(state, det, cfg, force=False):
    """Handle the page's request once (by its hash): each change only while the setting still has the value the page
    showed; safe values apply now, the rest wait for --apply. Returns True if state changed."""
    try:
        st = os.lstat(PAGE_REQ)
    except FileNotFoundError:
        _req_seen.update(sig=None, err="")
        return False
    sig = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if sig == _req_seen["sig"] and not force:
        return False
    _req_seen.update(sig=sig, err="")
    try:
        raw = read_regular(PAGE_REQ, PAGE_MAX)
    except CheckFailed as e:
        _req_seen["err"] = f"the page's request: {e}"
        return False
    h = hashlib.sha256(raw).hexdigest()
    if h == state["last"]:
        return False
    state["last"] = h
    try:
        req = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        req = None
    changes = req.get("changes") if isinstance(req, dict) and req.get("v") == 3 else None
    if not isinstance(changes, dict):
        state["rejected"], state["rejected_more"] = [["request", "not a settings request this collector understands"]], 0
        state["rejected_at"] = int(time.time())
        return True   # what the page set before stays as it is
    # drives it already shows stay allowed while unplugged (that's when their NOT MOUNTED alarm matters)
    known = [m for m in (cfg.get("mounts") or []) if isinstance(m, str) and m.startswith("/")]
    named = [m for m in (cfg.get("mount_labels") or {}) if isinstance(m, str)]   # a name can stay (or go) while unplugged
    det = {**det, "mounts": list(dict.fromkeys(det.get("mounts", []) + known)), "named": named}
    rejected = []
    for k, ch in changes.items():
        path = PATHS.get(k) if isinstance(k, str) else None
        if path is None:
            rejected.append([str(k)[:60], "not something the page can set"])
            continue
        if not isinstance(ch, dict) or ch.get("to") is None or not isinstance(ch.get("from"), str):
            rejected.append([k, "no new value"])
            continue
        cur = _get(cfg, path)
        shown_now = "" if cur is MISSING else digest(cur)
        if same(ch["to"], cur):           # already in effect (taken before, or another tab made the same change)
            if ch["from"] == shown_now:
                state["pending"].pop(k, None)   # set back to what's in effect: a waiting change is cancelled
            continue
        if ch["from"] != shown_now:
            rejected.append([k, "changed on the host after the page was opened: reload the page and change it again"])
            continue
        try:
            v = (SAFE_KEYS.get(path) or APPROVAL_KEYS[path])(ch["to"], det)
        except PageInvalid as e:
            rejected.append([k, str(e)[:300]])
            continue
        except Exception as e:   # a check must never stop the collector
            rejected.append([k, f"refused ({type(e).__name__})"])
            continue
        if path in SAFE_KEYS:
            fv = _get(FILE_CFG, path)
            if same(v, fv):
                state["safe"].pop(k, None)   # collector.toml has it: no page value needed
            else:
                state["safe"][k] = {"v": v} if fv is MISSING else {"v": v, "file": fv}
        else:
            state["pending"][k] = {"from": None if cur is MISSING else cur, "to": v}
    if (rejected[:20], max(0, len(rejected) - 20)) != (state["rejected"], state["rejected_more"]):   # (a request that changes
        state["rejected"], state["rejected_more"] = rejected[:20], max(0, len(rejected) - 20)      # nothing rewrites nothing)
        state["rejected_at"] = int(time.time())
    return True


def tidy(state, approved, page_toml=None):
    """Drop what no longer applies: a page value whose setting was edited in collector.toml since (the edit wins), and
    a waiting change that's done or whose setting was changed on the host meanwhile. Approved values that lost to an
    edit of collector.toml are remembered as such. Returns True if state changed."""
    changed = False
    vals, against = page_toml if page_toml is not None else read_page_toml()
    for path in APPROVAL_KEYS:
        k, v = ".".join(path), _get(vals, path)
        if v is not MISSING and not same(_get(FILE_CFG, path), against.get(k, MISSING)) and state["superseded"].get(k) != digest(v):
            state["superseded"][k] = digest(v)
            changed = True
    for k, o in list(state["safe"].items()):
        if not same(_get(FILE_CFG, PATHS[k]), o.get("file", MISSING)):
            del state["safe"][k]
            changed = True
    if state["rejected"] and time.time() - state["rejected_at"] > 86400:   # refusals are news for a day
        state["rejected"], state["rejected_more"] = [], 0
        changed = True
    if approved is None:
        return changed
    now = layer(FILE_CFG, approved, APPROVAL_KEYS)
    for k, p in list(state["pending"].items()):
        cur = _get(now, PATHS[k])
        if same(cur, p["to"]) or not same(cur, p["from"]):
            if not same(cur, p["to"]) and len(state["rejected"]) < 20:
                state["rejected"].append([k, "changed on the host before it was approved: change it again on the page"])
                state["rejected_at"] = int(time.time())
            del state["pending"][k]
            changed = True
    return changed


PAGE_STATE = {"state": None}   # what hw.json reports to the page (see page_round)


def page_round(det):
    """Take the page's request (when it changed), drop what no longer applies, put the settings in effect, and report
    them to the page. det = the latest detection (None until the first one is in: the request waits)."""
    st = {"v": 3, "detected": det or {}, "error": ""}
    with page_lock():   # (collector.page.toml too: --apply may be writing it)
        page_toml = read_page_toml()
        state = read_state()
        before = (state_text(state), state["last"])
        try:
            changed = tidy(state, None, page_toml)
            approved = approved_values(*page_toml, state)
            changed = (take_request(state, det, effective(approved, state)) if det is not None else False) or changed
            changed = tidy(state, approved, page_toml) or changed
            if changed:
                write_state(state, before)
        except CheckFailed as e:
            st["error"] = str(e)
    approved = approved_values(*page_toml, state)
    cfg = effective(approved, state)
    apply_config(cfg, approved)
    st["error"] = st["error"] or _req_seen["err"] or PAGE_TOML_ERR[0]
    st["settings"] = {**_flat(cfg, SAFE_KEYS), **_flat(cfg, APPROVAL_KEYS)}   # what's in effect, for the page
    st["fp"] = {k: digest(v) for k, v in st["settings"].items()}           # the page sends back the one it showed
    st["from_page"] = sorted(state["safe"]) + sorted(_flat(approved, APPROVAL_KEYS))
    st["pending"] = sorted(state["pending"])
    sizes = {k: len(json.dumps(p["to"], ensure_ascii=False)) for k, p in state["pending"].items()}
    st["pending_values"] = {k: p["to"] for k, p in state["pending"].items() if sizes[k] <= 16_384}   # a big one: the page
                                                                                                      # just says it waits
    st["rejected"], st["rejected_more"], st["last"] = state["rejected"], state["rejected_more"], state["last"]
    st["sandbox"] = sandbox_level()
    PAGE_STATE["state"] = st
    return st


SANDBOX_DROPIN = "/etc/systemd/system/homelab-dash-collector.service.d/10-sandbox.conf"


def sandbox_level():
    """Which sandbox level collector/sandbox.sh chose (from its drop-in), "none" if it found none, "" if never run."""
    try:
        with open(SANDBOX_DROPIN) as f:
            m = re.search(r"this machine \(([\w-]{1,40})\)", f.readline())
        return m.group(1) if m else "unknown"
    except FileNotFoundError:
        return "none" if os.path.exists(os.path.dirname(SANDBOX_DROPIN) + "/.sandbox-checked") else ""
    except OSError:
        return "unknown"


# --- `homelab-dash-collector --apply`: a person at the host approves the page's paths and patterns ---

def toml_str(v):
    """A TOML basic string. Everything unprintable (control characters, escape sequences, bidi marks) is escaped."""
    out = []
    for c in v:
        if c in '"\\':
            out.append("\\" + c)
        elif c.isprintable():
            out.append(c)
        else:
            out.append(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
    return '"' + "".join(out) + '"'


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if v == v and abs(v) != float("inf") else ("nan" if v != v else "inf" if v > 0 else "-inf")
    if isinstance(v, str):
        return toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{toml_str(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v).__name__)


def tkey(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else toml_str(k)


def toml_dumps(d, against=None):
    """The approved settings as TOML (only the shapes the APPROVAL_KEYS allow), plus the collector.toml values they
    were approved against."""
    lines = []
    for sec in ("security", "backup"):
        t = d.get(sec) or {}
        plain = {k: v for k, v in t.items() if not isinstance(v, dict) and not (isinstance(v, list) and v and isinstance(v[0], dict))}
        if plain:
            lines.append(f"[{sec}]")
            lines += [f"{tkey(k)} = {toml_value(v)}" for k, v in plain.items()]
            lines.append("")
        for k, v in t.items():
            if isinstance(v, dict):
                lines.append(f"[{sec}.{tkey(k)}]")
                lines += [f"{tkey(kk)} = {toml_value(vv)}" for kk, vv in v.items()]
                lines.append("")
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                for item in v:
                    lines.append(f"[[{sec}.{tkey(k)}]]")
                    lines += [f"{tkey(kk)} = {toml_value(vv)}" for kk, vv in item.items()]
                    lines.append("")
    if against:
        lines.append("# collector.toml's values these replaced: when one of them is edited there, collector.toml wins again")
        lines.append("[_against]")
        lines += [f"{toml_str(k)} = {toml_value(v)}" for k, v in against.items()]
    return "\n".join(lines) + "\n"


def fmt(v):
    return '"' + shown(v) + '"' if isinstance(v, str) else shown(toml_value(v)) if v is not None and v is not MISSING else "(not set)"


def _items_key(old, new):
    """How to pair up table entries: by name when every name is unique on both sides, else by position."""
    labels = lambda xs: [x.get("label") if isinstance(x, dict) else None for x in xs]
    lo, ln = labels(old), labels(new)
    if None not in lo + ln and len(set(lo)) == len(lo) and len(set(ln)) == len(ln):
        return lambda i, x: ("label", x["label"])
    return lambda i, x: ("index", i)


def diff_lines(old, new, indent="    "):
    """Only what changes, marked: + added, - removed, ~ changed (old -> new), with unchanged entries counted, not
    shown. Whole values, never cut; every string through shown()."""
    old = None if old is MISSING else old
    if isinstance(new, list) and (old is None or isinstance(old, list)):
        old = old or []
        if any(isinstance(x, dict) for x in old + new):
            key = _items_key(old, new)
            before = {key(i, x): x for i, x in enumerate(old)}
            after = {key(i, x): x for i, x in enumerate(new)}
            out, same_n = [], 0
            for k, x in after.items():
                name = shown(x.get("label", "(no name)")) if isinstance(x, dict) else fmt(x)
                if k not in before:
                    out.append(f"{indent}+ new entry: {name}")
                    out += diff_lines(None, x, indent + "    ")
                elif not same(before[k], x):
                    out.append(f"{indent}~ {name}:")
                    out += diff_lines(before[k], x, indent + "    ")
                else:
                    same_n += 1
            for k, x in before.items():
                if k not in after:
                    out.append(f"{indent}- removed entry: {shown(x.get('label', '(no name)')) if isinstance(x, dict) else fmt(x)}")
            return out + ([f"{indent}({same_n} unchanged)"] if same_n else [])
        added = [x for x in new if not any(same(x, y) for y in old)]
        removed = [x for x in old if not any(same(x, y) for y in new)]
        kept = len(new) - len(added)
        out = [f"{indent}+ {fmt(x)}" for x in added] + [f"{indent}- {fmt(x)}" for x in removed]
        if not out and not same(old, new):
            out = [f"{indent}~ same entries, new order"]
        return out + ([f"{indent}({kept} unchanged)"] if kept and out else []) or [f"{indent}(no change)"]
    if isinstance(new, dict) and (old is None or isinstance(old, dict)):
        old = old or {}
        out = []
        for k in list(new) + [k for k in old if k not in new]:
            a, b = old.get(k, MISSING), new.get(k, MISSING)
            if same(a, b):
                continue
            if b is MISSING:
                out.append(f"{indent}- {shown(k)}: {fmt(a)}" if not isinstance(a, (list, dict)) else f"{indent}- {shown(k)}")
            elif isinstance(b, (list, dict)):
                out.append(f"{indent}{'+' if a is MISSING else '~'} {shown(k)}:")
                out += diff_lines(a, b, indent + "    ")
            elif a is MISSING:
                out.append(f"{indent}+ {shown(k)}: {fmt(b)}")
            else:
                out.append(f"{indent}~ {shown(k)}: {fmt(a)} -> {fmt(b)}")
        return out or [f"{indent}(no change)"]
    return [f"{indent}~ {fmt(old)} -> {fmt(new)}"]


def summary(old, new):
    """A few words on the size of a change, for the first line --apply prints."""
    old = None if old is MISSING else old
    if isinstance(new, list):
        old = old if isinstance(old, list) else []
        if any(isinstance(x, dict) for x in old + new):
            key = _items_key(old, new)
            before, after = {key(i, x): x for i, x in enumerate(old)}, {key(i, x): x for i, x in enumerate(new)}
            add, rem = sum(k not in before for k in after), sum(k not in after for k in before)
            chg = sum(k in before and not same(before[k], x) for k, x in after.items())
            parts = [f"{add} new" if add else "", f"{chg} changed" if chg else "", f"{rem} removed" if rem else ""]
        else:
            add = sum(1 for x in new if not any(same(x, y) for y in old))
            rem = sum(1 for x in old if not any(same(x, y) for y in new))
            parts = [f"{add} added" if add else "", f"{rem} removed" if rem else ""]
        return ", ".join(x for x in parts if x) or "new order"
    return "changed" if old is not None else "set"


def paths_in(k, v):
    """The paths and patterns in an approval value (for the folder warnings)."""
    if k in ("security.authelia_db", "security.fail2ban_log", "backup.log"):
        return [v] if v else []
    if k == "security.immutable_files":
        return list(v)
    if k == "security.watcher":
        return [v[f] for f in ("heartbeat", "alerts") if v.get(f)]
    if k == "backup.artifacts":
        return [a["glob"] for a in v if a.get("glob")]
    if k == "backup.jobs":
        return [f for j in v for f in j.get("files", [])] + [j["dest"] for j in v if j.get("dest")]
    return []


def folder_warnings(p):
    """Why a person might not want root reading p: a folder on the way the dash's user owns or anyone can write to
    (they could swap what's there), or a link (it leads somewhere else; a path the page set is never read through one).
    For a pattern: the folders up to the one it lists."""
    out, parts = [], p.split("/")
    magic = next((i for i, x in enumerate(parts) if re.search(r"[*?\[]", x)), None)
    last = magic if magic is not None else len(parts) - 1      # folders up to (and including) the one holding it
    user = ""
    try:
        user = f" ({pwd.getpwuid(DASH_UID).pw_name})"
    except KeyError:
        pass
    for i in range(1, last + 1):
        d = "/".join(parts[:i]) or "/"
        try:
            st = os.lstat(d)
        except OSError:
            break
        if stat.S_ISLNK(st.st_mode):
            out.append(f"{shown(d)} is a link to {shown(os.path.realpath(d))}: the settings page's paths are never read through one")
            break
        if st.st_uid == DASH_UID and st.st_uid != 0:
            out.append(f"{shown(d)} belongs to uid {DASH_UID}{shown(user)}, the user the dashboard container runs as: "
                       "anything that runs as that user can change what's in there")
        elif st.st_mode & 0o002 and not st.st_mode & stat.S_ISVTX:
            out.append(f"anyone can write in {shown(d)}")
    if magic is None and os.path.islink(p):
        out.append(f"{shown(p)} is a link to {shown(os.path.realpath(p))}: the settings page's paths are never read through one")
    return out


def _confirm(question):
    return input(question).strip().lower() == "y"


def cli_apply(undo=False):
    """--apply: show what the settings page wants changed in paths and patterns, ask, then write collector.page.toml
    and restart. --apply --undo: drop everything the page set (back to collector.toml only)."""
    if os.geteuid() != 0:
        sys.exit("Run it with sudo: sudo homelab-dash-collector --apply")
    if not sys.stdin.isatty():
        sys.exit("--apply asks before it changes anything, so it needs a terminal.")
    if not os.path.isdir(OUT_DIR):
        sys.exit(f"The collector hasn't run yet ({OUT_DIR} doesn't exist): start it first.")
    if undo:
        return cli_undo()
    det = detect()
    with page_lock():
        page_toml = read_page_toml()
        state = read_state()
        before = (state_text(state), state["last"])
        tidy(state, None, page_toml)
        approved = approved_values(*page_toml, state)
        take_request(state, det, effective(approved, state), force=True)
        tidy(state, approved, page_toml)
        write_state(state, before)
    if _req_seen["err"]:
        sys.exit(shown(_req_seen["err"]))
    if PAGE_TOML_ERR[0]:
        print(shown(PAGE_TOML_ERR[0]))
    for k, why in state["rejected"][:10]:
        print(f"refused (not applied): {shown(k)}: {shown(why)}")
    more = len(state["rejected"]) - 10 + state["rejected_more"]
    if more > 0:
        print(f"... and {more} more refused")
    now = layer(FILE_CFG, approved, APPROVAL_KEYS)
    plan, refused = [], []
    for k, p in sorted(state["pending"].items()):
        try:
            plan.append((k, APPROVAL_KEYS[PATHS[k]](p["to"], det)))   # checked again here, with today's rules
        except PageInvalid as e:
            print(f"refused (not applied): {shown(k)}: {shown(e)}")
            refused.append([k, str(e)[:300]])
    if refused:   # off the waiting list, and the page says why
        with page_lock():
            st = read_state()
            for k, why in refused:
                st["pending"].pop(k, None)
            st["rejected"], st["rejected_at"] = (st["rejected"] + refused)[:20], int(time.time())
            write_state(st)
            state["pending"] = st["pending"]
    if not plan:
        print("Nothing from the settings page waits for your OK.")
        return
    print("\nThe settings page wants to change: " + ", ".join(f"{LABELS.get(k, k)} ({summary(_get(now, PATHS[k]), v)})" for k, v in plan))
    print("Root will read these files and run these patterns. + added, - removed, ~ changed:\n")
    for k, v in plan:
        print(f"  {LABELS.get(k, k)} ({k})")
        print("\n".join(diff_lines(_get(now, PATHS[k]), v)))
        for w in dict.fromkeys(w for p in paths_in(k, v) for w in folder_warnings(p)):
            print(f"    ! {w}")
        print()
    answer = input("Apply these? y = yes, N = not now, d = drop them from the waiting list [y/N/d] ").strip().lower()
    if answer == "d":
        with page_lock():
            st = read_state()
            for k, _ in plan:
                st["pending"].pop(k, None)
            write_state(st)
        print("Dropped: they no longer wait (nothing else changed).")
        return
    if answer != "y":
        print("Nothing changed.")
        return
    try:
        approve(plan, state["pending"])
    except CheckFailed as e:
        sys.exit(f"{e} Nothing was applied.")
    print(f"Saved {PAGE_TOML}.")
    restart_service()


def approve(plan, seen):
    """Write the approved values into collector.page.toml (with collector.toml's values they replace) and take them
    off the waiting list. seen = the waiting list the person was shown: if the page changed it meanwhile, nothing."""
    with page_lock():
        if read_state()["pending"] != seen:
            raise CheckFailed("The settings page changed these while you were reading: run it again.")
        vals, against = read_page_toml()
        state = read_state()
        keep = approved_values(vals, against, state)   # values that lost to an edit of collector.toml go for good
        vals, against = keep, {k: v for k, v in against.items() if _get(keep, PATHS[k]) is not MISSING}
        for k, v in plan:
            path = PATHS[k]
            fv = _get(FILE_CFG, path)
            sub = vals
            for part in path[:-1]:
                sub = sub.setdefault(part, {})
            if same(v, fv):
                sub.pop(path[-1], None)   # collector.toml has it: no page value needed
                against.pop(k, None)
            else:
                sub[path[-1]] = v
                if fv is MISSING:
                    against.pop(k, None)
                else:
                    against[k] = fv
        try:
            text = ("# Written by `homelab-dash-collector --apply` (settings approved from the dash's settings page).\n"
                    "# It overrides collector.toml for these keys. Undo: sudo homelab-dash-collector --apply --undo\n\n"
                    + toml_dumps(vals, against))
            back = tomllib.loads(text)
        except (TypeError, ValueError) as e:
            raise CheckFailed(f"The approved settings can't be written as TOML ({type(e).__name__}).")
        if not same(layer({}, back, APPROVAL_KEYS), layer({}, vals, APPROVAL_KEYS)) or not same(back.get("_against", {}), against):
            raise CheckFailed("The approved settings don't survive a round trip through TOML.")
        if os.path.exists(PAGE_TOML):
            shutil.copy2(PAGE_TOML, f"{PAGE_TOML}.bak.{time.strftime('%Y-%m-%d-%H%M%S')}")
        write_root_only(PAGE_TOML, text, 0o644)
        for k, _ in plan:
            state["pending"].pop(k, None)
        state["superseded"] = {}
        write_state(state)


def cli_undo():
    have_any = [p for p in (PAGE_TOML, PAGE_STATE_FILE, PAGE_LAST_FILE, PAGE_REQ) if os.path.lexists(p)]
    if not have_any:
        print("Nothing from the settings page is in use: the collector runs on collector.toml alone.")
        return
    print("This drops every collector setting made on the settings page (approved and automatic), so the collector")
    print(f"runs on {CONFIG_FILE} alone again. The next time someone saves the collector's settings on the page, those")
    print("changes apply again (switches by themselves, paths and patterns after --apply).")
    if not _confirm("Go ahead? [y/N] "):
        print("Nothing changed.")
        return
    try:
        with page_lock():
            problems, aside = undo_page_settings()
        if aside:   # the old request folder: out of the dash's reach already, removed now (the lock isn't held)
            try:
                remove_tree(aside)
            except Exception as e:
                problems.append(f"{aside} (left there, root's folder: {e})")
        for p in problems:
            print(f"Couldn't remove {shown(p)}")
    finally:
        restart_service()


def undo_page_settings():
    """Drop everything the page set: collector.page.toml (a copy is kept), the page's state and its request. Each
    file on its own, so one that can't be removed doesn't stop the rest. Returns (what couldn't be removed, a folder
    the request was moved into, to remove afterwards)."""
    problems = []
    for f in (PAGE_STATE_FILE, PAGE_LAST_FILE):
        try:
            if os.path.lexists(f):
                os.remove(f)
        except Exception as e:
            problems.append(f"{f}: {e}")
    if os.path.lexists(PAGE_TOML):
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        try:
            os.replace(PAGE_TOML, f"{PAGE_TOML}.bak.{stamp}")
            print(f"Kept a copy: {PAGE_TOML}.bak.{stamp}")
        except Exception as e:
            problems.append(f"{PAGE_TOML}: {e}")
    aside = None
    try:
        if os.path.lexists(PAGE_REQ):
            if os.path.isdir(PAGE_REQ) and not os.path.islink(PAGE_REQ):
                # out of the dash's reach (OUT_DIR is root's, and read-only for the dash); the caller removes it
                aside = tempfile.mkdtemp(dir=OUT_DIR, prefix=".undo-")
                os.replace(PAGE_REQ, os.path.join(aside, "request"))
            else:
                os.remove(PAGE_REQ)
    except Exception as e:
        problems.append(f"{PAGE_REQ}: {e}")
    return problems, aside


def remove_tree(top):
    """Remove a folder tree without following links, recursion or long paths: each folder's subfolders are first moved
    up next to it (so nothing is ever deep), then it's emptied and removed. A folder thousands of levels deep is fine."""
    top_fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    n = 0
    try:
        for _ in range(2_000_000):   # something still filling it (an open folder handle) can't keep this going forever
            with os.scandir(top_fd) as it:
                entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
            if not entries:
                break
            for name, is_dir in entries:
                if not is_dir:
                    os.unlink(name, dir_fd=top_fd)
                    continue
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top_fd)
                try:
                    with os.scandir(fd) as it:
                        kids = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
                    for kid, kid_dir in kids:
                        if kid_dir:
                            n += 1
                            os.rename(kid, f".moved-{n}", src_dir_fd=fd, dst_dir_fd=top_fd)
                        else:
                            os.unlink(kid, dir_fd=fd)
                finally:
                    os.close(fd)
                os.rmdir(name, dir_fd=top_fd)
        else:
            raise OSError(16, "still being filled")
    finally:
        os.close(top_fd)
    os.rmdir(top)


def restart_service():
    r = subprocess.run(["systemctl", "restart", "homelab-dash-collector"], capture_output=True, text=True)
    print("Collector restarted." if r.returncode == 0 else f"Restart failed: {shown(r.stderr.strip()[:300])}")


_approved = read_approved()
apply_config(effective(_approved), _approved)   # what --apply and the page set, from the start


_det = {"det": None, "busy": False}


def detect_round():
    try:
        _det["det"] = detect()
    except Exception as e:
        print(f"detect: {type(e).__name__}: {e}", flush=True)
    finally:
        _det["busy"] = False


def req_signature():
    try:
        st = os.lstat(PAGE_REQ)
        return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        return None


def main():
    """Every round (default 5 s): refresh whatever is due, then write hw.json.
    Slow or expensive checks run less often: GPU 10 s, containers 30 s, drives + security + backups 60 s, SMART 5 min."""
    os.makedirs(OUT_DIR, exist_ok=True)
    os.chmod(OUT_DIR, 0o755)
    load_sizes()
    data = {"disks": [], "smart": {}, "gpu": None, "containers": None, "net": None}
    last_run = {}

    def due(name, every):
        now = time.monotonic()
        if now - last_run.get(name, float("-inf")) >= every:
            last_run[name] = now
            return True
        return False

    try:
        prev_net = (time.monotonic(), *net_bytes(), NET_IF)
    except OSError:
        prev_net = None
    prev_energy = (time.monotonic(), rapl_energy())
    power_limit = rapl_limit_w()
    page_sig = None
    while True:
        # what the machine has (for the settings page): every 5 min, in its own thread (fail2ban or docker can hang)
        if not _det["busy"] and due("detect", 300):
            _det["busy"] = True
            try:
                threading.Thread(target=detect_round, daemon=True).start()
            except RuntimeError:
                _det["busy"] = False
        # the settings page's settings: once a minute, and right away when the page saved something
        sig = req_signature()
        if due("page", 60) or (sig != page_sig and due("page-request", 10)):   # a new request: at most every 10 s
            page_sig = sig
            try:
                data["collector"] = page_round(_det["det"])
            except Exception as e:
                data["collector"] = {"v": 3, "error": f"page settings: {type(e).__name__}: {e}"[:300]}
        if due("disks", 60):
            data["disks"] = disks()
        if have("smartctl") and due("smart", 300):
            devs = {d["dev"] for d in data["disks"] if d.get("dev")}
            data["smart"] = {dev: smart(dev) for dev in sorted(devs)}
        if due("gpu", 10):
            data["gpu"] = gpu()
        if due("security", 60):
            try:
                data["security"] = security()
            except Exception as e:
                data["security"] = {"error": str(e)}
            try:
                data["backups"] = backups()
            except Exception as e:
                data["backups"] = {"ok": None, "log": [f"collector error: {e}"], "artifacts": []}
            if not SEC.get("details", True):          # switched off: say so, so the page doesn't show old details
                try:
                    write_output({"updated": int(time.time()), "off": True}, DETAIL)
                except OSError:
                    pass
            elif not _detail["busy"]:                     # in its own thread: hw.json never waits for it
                _detail["busy"] = True
                try:
                    threading.Thread(target=detail_round, daemon=True).start()
                except RuntimeError:
                    _detail["busy"] = False
        if due("containers", 30):
            data["containers"] = containers()
        if CFG.get("usage", True):
            try:
                data["usage"] = USAGE.sample() or data.get("usage")   # first round has no delta yet: keep the old one
            except Exception as e:
                data["usage"] = {"error": str(e)}
        else:
            data.pop("usage", None)

        # a "Looks good" click on the dash: apply it now, so the cron box clears within one round
        try:
            if handle_cron_approval() and isinstance(data.get("security"), dict) and "cron" in data["security"]:
                data["security"]["cron"] = cron_state()
        except Exception:
            pass

        # network rate = bytes since the previous round / seconds since then
        try:   # (after a change of interface, the first round has nothing to compare with)
            t, rx, tx = time.monotonic(), *net_bytes()
            if prev_net and prev_net[3] == NET_IF:
                dt = t - prev_net[0]
                data["net"] = {"if": NET_IF, "rx_bps": (rx - prev_net[1]) / dt, "tx_bps": (tx - prev_net[2]) / dt}
            prev_net = (t, rx, tx, NET_IF)
        except OSError:   # the interface is gone: no rate rather than an old one
            data["net"], prev_net = None, None

        # CPU package power = energy since the previous round / seconds since then (the counter wraps now and then: skip)
        t, e = time.monotonic(), rapl_energy()
        if e is not None and prev_energy[1] is not None and e >= prev_energy[1]:
            data["power"] = {"pkg_w": round((e - prev_energy[1]) / 1e6 / (t - prev_energy[0]), 1), "limit_w": power_limit}
        prev_energy = (t, e)

        data["updated"], data["build"] = int(time.time()), BUILD
        write_output(data)
        time.sleep(int(CFG.get("interval", 5)))


def write_probe():
    """What main() writes, tried in a throwaway folder in out_dir (removed again): a folder root owns and the dash's
    group can write to (like req/), a file made and removed in it, and output files written like hw.json and the
    page's settings are."""
    d = tempfile.mkdtemp(dir=OUT_DIR, prefix=".selftest-")
    try:
        write_output({"probe": True}, os.path.join(d, "probe.json"))   # while only root can get in
        write_root_only(os.path.join(d, "state.json"), "{}")
        if os.geteuid() == 0:
            os.chown(d, 0, DASH_UID)
        os.chmod(d, 0o770)
        f = os.path.join(d, "request")
        with open(f, "w") as fh:
            fh.write("x")
        os.remove(f)
        return "ok"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def selftest():
    """--selftest: every check once, then one line of JSON with what worked: facts only (read / error / off and
    counts), never messages or health, so two runs on the same machine give the same line. The installer runs it under
    the plain unit and under stricter sandboxes and keeps the strictest that gives the same line (a sandbox that blocks
    something shows up here, not as a quietly empty tile later). Settings: collector.toml and what --apply approved
    (never the page's automatic ones, which a hijacked dash could flip between runs). It writes nothing except a
    throwaway folder in out_dir (removed again)."""
    global save_approved, load_approved
    approved = read_approved()
    apply_config(layer(FILE_CFG, approved, APPROVAL_KEYS), approved)
    _sizes["busy"] = True                                  # no du in the background
    mem, real_load = {}, load_approved                     # the cron check's first-run baseline stays in memory
    save_approved = lambda snap: mem.update(appr={"snap": snap, "approved_at": None})
    load_approved = lambda: mem.get("appr") or real_load()
    res = {}

    def t(name, fn, summary=lambda v: "read"):
        try:
            res[name] = summary(fn())
        except Exception:
            res[name] = "error"

    ds = []
    t("disks", lambda: ds.extend(disks()) or ds, lambda v: f"{sum(1 for d in v if d.get('total'))} with sizes")
    devs = sorted({d["dev"] for d in ds if d.get("dev")})
    if have("smartctl"):
        t("smart", lambda: [smart(x) for x in devs],
          lambda v: f"{sum(1 for x in v if x.get('temp') is not None or x.get('standby'))}/{len(v)} read")
    t("gpu", gpu, lambda v: "found" if v else "none")
    if have("docker"):
        t("containers", lambda: out(["docker", "ps", "-a", "-q"], timeout=15))
    t("power", rapl_energy, lambda v: "read" if v is not None else "none")
    t("net", net_bytes)
    t("usage", lambda: (USAGE.sample(), time.sleep(1.2), USAGE.sample())[2], lambda v: "read" if v else "none")
    sec = {}
    t("security", lambda: sec.update(security()))
    if "firewall" in sec:
        res["security.firewall"] = sec["firewall"].get("status", "unknown")
    if "f2b" in sec:
        res["security.fail2ban"] = "read" if sec["f2b"].get("ok") else "error"
    if "procs" in sec:
        res["security.procs"] = "read" if sec["procs"].get("top") else "error"
    if "locks" in sec:
        res["security.locks"] = ",".join(str(x.get("locked")) for x in sec["locks"])
    if SEC.get("authelia_container") and have("docker"):
        t("security.authelia", lambda: out(["docker", "logs", "--since", "1m", SEC["authelia_container"]], timeout=30))
    if SEC.get("cron_watch", True):
        def crontabs():
            names = ["/etc/crontab"] + sorted(glob.glob("/etc/cron.d/*")) + sorted(glob.glob("/var/spool/cron/crontabs/*"))
            got = 0
            for n in names:
                try:
                    with open(n, "rb") as f:
                        f.read(1)
                    got += 1
                except OSError:
                    pass
            return f"{got}/{len(names)} read"
        t("security.cron", crontabs, lambda v: v)
    if BAK.get("log"):
        t("backups.log", lambda: tail_lines(BAK["log"], 4096, "backup.log"), lambda v: "read" if v else "empty")
    for a in BAK.get("artifacts", []):
        t(f"backups.files.{clip(a.get('label', ''), 30)}", lambda a=a: [f for f in glob.glob(a.get("glob", "")) if page_path("backup.artifacts", f)],
          lambda v: f"{len(v)} found")
    for key, v in security_detail().items():
        if key != "updated":
            res[f"detail.{key}"] = "error" if isinstance(v, dict) and "error" in v else "off" if v is None else "read"
    det = detect()
    res["detect.mounts"] = " ".join(det["mounts"])
    res["detect"] = " ".join(f"{k}={len(v)}" for k, v in det.items() if isinstance(v, (list, dict)) and k != "mounts")
    t("write", write_probe, lambda v: v)
    print(json.dumps(res, sort_keys=True))


if __name__ == "__main__":
    os.umask(0o077)       # whatever else it writes: root's alone unless it says otherwise (chmod)
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif sys.argv[1:] in (["--apply"], ["--apply", "--undo"]):
        cli_apply(undo=len(sys.argv) > 2)
    elif sys.argv[1:]:
        sys.exit("usage: homelab-dash-collector [--apply [--undo] | --selftest]")
    else:
        main()
