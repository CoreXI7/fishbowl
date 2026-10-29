#!/usr/bin/env python3
"""Root-side stats collector for homelab-dash (installed as /usr/local/sbin/homelab-dash-collector).

Writes <out_dir>/hw.json (and sec-detail.json for the Security & backups detail views), which the dash container
mounts read-only. Gathers only what the
container can't (or shouldn't) see itself: host network rate, GPU, drive usage, SMART health,
container counts, and optional security/backup checks. It never listens on the network.
SMART uses `-n standby` so sleeping drives are never woken.

Config: /etc/homelab-dash/collector.toml (see collector.example.toml). Every key is optional.
"""
import base64
import datetime
import glob
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import tomllib

CONFIG_FILE = os.environ.get("HOMELAB_DASH_COLLECTOR_CONFIG", "/etc/homelab-dash/collector.toml")
try:
    with open(CONFIG_FILE, "rb") as f:
        CFG = tomllib.load(f)
except FileNotFoundError:
    CFG = {}

OUT_DIR = CFG.get("out_dir", "/var/lib/homelab-dash")
OUT = os.path.join(OUT_DIR, "hw.json")
DETAIL = os.path.join(OUT_DIR, "sec-detail.json")   # the Security & backups detail views (bigger, read on demand)
DASH_UID = int(CFG.get("dash_uid", 1000))         # the dash container's user/group: owns the request folder, can read hw.json
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
        for line in open("/proc/net/route").read().splitlines()[1:]:
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
        hits = sorted(glob.glob(p), key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)])
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
        st = os.statvfs(m)
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

def cron_files():
    """Every crontab's active lines (comments and blanks dropped), by file."""
    files = ["/etc/crontab"] + sorted(glob.glob("/etc/cron.d/*")) + sorted(glob.glob("/var/spool/cron/crontabs/*")) \
        + sorted(glob.glob("/var/spool/cron/*"))         # RHEL-style per-user crontabs (dirs are skipped below)
    snap = {}
    for f in files:
        if not os.path.isfile(f):
            continue
        try:
            lines = [l.strip() for l in open(f, errors="replace") if l.strip() and not l.strip().startswith("#")]
        except OSError:
            continue
        snap[f] = lines
    return snap


def cron_hash(snap):
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()


def redact(line):
    """Strip secrets from cron/log lines before they go into hw.json (and onto the dashboard)."""
    line = re.sub(r"https?://\S*(webhook|token|key|api|secret|sig)\S*", "[url]", line, flags=re.I)
    line = re.sub(r"(\w+://)[^/\s:@]+:[^@\s]+@", r"\1***@", line)                       # user:pass@host
    val = r"('[^']*'|\"[^\"]*\"|\S+)"
    line = re.sub(r"(?i)\b(\w*(pass(word|wd)?|token|secret|api_?key)\w*\s*[=:]\s*)" + val, r"\1***", line)   # PASSWORD=x
    line = re.sub(r"(?i)(--?[\w-]*(pass(word|wd)?|token|secret|api-?key|auth)[\w-]*[\s=]+)" + val, r"\1***", line)  # --password x
    line = re.sub(r"(\s-p)(?!\s)\S+", r"\1***", line)                                     # mysql -pSecret
    line = re.sub(r"(\s-u\s+[^\s:]+:)\S+", r"\1***", line)                               # curl -u user:pass
    line = re.sub(r"(sshpass\s+(?:-\w+\s+)*-p\s*)\S+", r"\1***", line)                   # sshpass -p pw
    line = re.sub(r"(\s-U\s+[^\s%]+%)\S+", r"\1***", line)                               # smbclient -U user%pw
    line = re.sub(r"(?i)((?:x-api-key|api-key|cookie|x-auth-token)\s*:\s*)[^\s'\"]+", r"\1***", line)
    line = re.sub(r"(?i)([?&](?:k|key|token|apikey|api_key|auth|sig|signature|secret|pass(?:word)?)=)[^&\s'\"]+", r"\1***", line)
    line = re.sub(r"(?i)(authorization:\s*\w+\s+)\S+", r"\1***", line)
    return re.sub(r"[A-Za-z0-9_\-+=]{28,}", "…", line)[:220]   # long random-looking tokens


def load_approved():
    try:
        return json.load(open(CRON_APPROVED))
    except Exception:
        return None


def save_approved(snap):
    tmp = CRON_APPROVED + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"snap": snap, "hash": cron_hash(snap), "approved_at": int(time.time())}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRON_APPROVED)


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
                            "added": [redact(l) for l in added][:20], "removed": [redact(l) for l in removed][:20]})
    return {"ok": not changes, "hash": cron_hash(snap), "approved_at": appr.get("approved_at"),
            "files": len(snap), "changes": changes}


def handle_cron_approval():
    """Approve only if the requested hash equals the current state (the dash can't approve anything else)."""
    os.makedirs(REQ_DIR, exist_ok=True)
    os.chown(REQ_DIR, DASH_UID, DASH_UID)
    os.chmod(REQ_DIR, 0o700)
    if not os.path.exists(REQ_FILE):
        return False
    try:
        want = open(REQ_FILE).read(100).strip()
    except OSError:
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
        logins = []
        for line in run(["journalctl", *units, "--since", "-7d", "-n", "100000", "-o", "short-iso", "--no-pager", "-q"], 30).splitlines():
            m = re.search(r"^(\S+) .*Accepted (\w+) for (\S+) from (\S+)", line)
            if m:
                logins.append({"time": m.group(1), "method": m.group(2), "user": m.group(3), "ip": m.group(4)})
        day = run(["journalctl", *units, "--since", "-24h", "--no-pager", "-q"], 30)
        s["ssh"] = {"logins": logins[-8:][::-1],
                    "failed_24h": len(re.findall(r"Failed password|Invalid user|authentication failure", day))}
    authelia = SEC.get("authelia_container", "")
    if authelia and have("docker"):
        fails = []
        out = subprocess.run(["docker", "logs", "--since", "24h", authelia], capture_output=True, text=True, timeout=30)
        for line in (out.stdout + out.stderr).splitlines():
            m = re.search(r'time="([^"]+)".*Unsuccessful (\w+) authentication attempt by user \'([^\']*)\'.*remote_ip=(\S+)', line)
            if m:
                fails.append({"time": m.group(1), "factor": m.group(2), "user": m.group(3), "ip": m.group(4)})
        fails.sort(key=lambda f: f["time"])
        s["authelia"] = {"failed_24h": len(fails), "recent": fails[-6:][::-1]}
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
            attrs = run(["lsattr", f]).split()
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
    try:
        lines = open(log).read().splitlines() if log else []
    except OSError:
        lines = []
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
        files = [f for f in glob.glob(a.get("glob", "")) if os.path.isfile(f)]
        if not files:
            b["artifacts"].append({"label": a.get("label", "files"), "count": 0, "fresh": False})
            continue
        newest = max(files, key=os.path.getmtime)
        mt = os.path.getmtime(newest)
        b["artifacts"].append({"label": a.get("label", "files"), "count": len(files), "newest": int(mt),
                               "size": os.path.getsize(newest), "fresh": time.time() - mt < max_age})
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


def read_regular(path, limit):
    """A file's bytes, read without following a symlink at its name or blocking on a FIFO, and only if it's a regular
    file of at most limit bytes (for files in folders a container can write to)."""
    name = os.path.basename(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise CheckFailed(f"can't open {name}: {e.strerror}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CheckFailed(f"{name} isn't a regular file")
        if st.st_size > limit:
            raise CheckFailed(f"{name} is bigger than {limit >> 20} MB")
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


def tail_lines(path, max_bytes=2_000_000):
    """The last max_bytes of a log file as lines (some logs can be grown at will by anyone who can reach the box)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
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
    for line in tail_lines(log + ".1") + tail_lines(log):    # last week's file first, so events stay in time order
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


SSH_LINE = re.compile(r"^(\S+) \S+ sshd(?:-session)?\[(\d+)\]: (.*)$")      # OpenSSH 9.8+ logs as sshd-session
SSH_ACCEPT = re.compile(r"^Accepted (?P<method>[\w/-]+) for (?P<user>\S+) from (?P<ip>\S+) port \d+ ssh2(?:: \S+ (?P<fp>SHA256:\S+))?$")
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
    lines = out(["journalctl", *units, "--since", "-7d", "-n", "100000", "-o", "short-iso", "--no-pager", "-q"], 60).splitlines()
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
            "truncated": len(lines) >= 100000, "checked": int(time.time())}


AUTHELIA_TABLES = ("authentication_logs", "banned_user", "banned_ip")


def authelia_detail():
    """Sign-ins and lockouts from Authelia's SQLite database. Its folder is writable by the Authelia container, so SQLite
    never opens anything there: the file's bytes are read (no symlinks, no FIFOs, size-capped) into an in-memory copy,
    queried with SQLite's defensive settings (no triggers, no views, untrusted schema), real tables only, row limits and
    a 5-second time limit. A copy also means Authelia's own database is never locked by us."""
    db = SEC.get("authelia_db", "")
    if not db:
        return None
    now = int(time.time())
    if not no_symlink(db):
        raise CheckFailed("authelia_db goes through a symlink")
    data = read_regular(db, 64 << 20)
    if data[18:20] == b"\x02\x02":                        # a WAL-mode header can't be used in memory: read it as rollback mode
        data = data[:18] + b"\x01\x01" + data[20:]
    con = sqlite3.connect(":memory:")
    try:
        if hasattr(con, "setconfig"):                     # Python 3.12+
            for opt, on in (("SQLITE_DBCONFIG_DEFENSIVE", True), ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
                            ("SQLITE_DBCONFIG_ENABLE_TRIGGER", False), ("SQLITE_DBCONFIG_ENABLE_VIEW", False)):
                if hasattr(sqlite3, opt):
                    con.setconfig(getattr(sqlite3, opt), on)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        deadline = time.monotonic() + 5
        con.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)   # a true return stops the query
        con.deserialize(data)
        con.execute("PRAGMA query_only = ON")
        kinds = dict(con.execute("SELECT name, type FROM sqlite_schema WHERE name IN (?, ?, ?)", AUTHELIA_TABLES).fetchall())
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
    return {"events": events[:60], **n, "fail_by_user": top(by_user), "fail_by_ip": top(by_ip), "bans": bans, "checked": now}


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
    if isinstance(w, dict):
        beat, alerts = w.get("heartbeat", ""), w.get("alerts", "")
        every = float(w.get("every_minutes", 5))
        last = int(os.path.getmtime(beat)) if beat and os.path.exists(beat) else None
        hits, kinds = [], w.get("kinds", {}) if isinstance(w.get("kinds"), dict) else {}   # alert-line prefix -> label
        if alerts and os.path.isfile(alerts):
            for line in tail_lines(alerts, 200_000)[-200:]:
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
    lines = (tail_lines(log + ".1", 4_000_000) + tail_lines(log, 4_000_000)) if log else []   # + last rotation's file
    try:
        log_mtime = os.path.getmtime(log) if log else None
    except OSError:
        log_mtime = None
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
                if len(today) < need:
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
            found = glob.glob(g)
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
        if dest and os.path.isdir(dest):
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
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.chown(tmp, 0, DASH_UID)
    except PermissionError:               # not root (testing): leave ownership as is
        pass
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


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
        prev_net = (time.monotonic(), *net_bytes())
    except OSError:
        prev_net = None
    prev_energy = (time.monotonic(), rapl_energy())
    power_limit = rapl_limit_w()
    while True:
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
            if SEC.get("details", True) and not _detail["busy"]:   # in its own thread: hw.json never waits for it
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

        # a "Looks good" click on the dash: apply it now, so the cron box clears within one round
        try:
            if handle_cron_approval() and isinstance(data.get("security"), dict) and "cron" in data["security"]:
                data["security"]["cron"] = cron_state()
        except Exception:
            pass

        # network rate = bytes since the previous round / seconds since then
        try:
            t, rx, tx = time.monotonic(), *net_bytes()
            if prev_net:
                dt = t - prev_net[0]
                data["net"] = {"if": NET_IF, "rx_bps": (rx - prev_net[1]) / dt, "tx_bps": (tx - prev_net[2]) / dt}
            prev_net = (t, rx, tx)
        except OSError:
            pass

        # CPU package power = energy since the previous round / seconds since then (the counter wraps now and then: skip)
        t, e = time.monotonic(), rapl_energy()
        if e is not None and prev_energy[1] is not None and e >= prev_energy[1]:
            data["power"] = {"pkg_w": round((e - prev_energy[1]) / 1e6 / (t - prev_energy[0]), 1), "limit_w": power_limit}
        prev_energy = (t, e)

        data["updated"] = int(time.time())
        write_output(data)
        time.sleep(int(CFG.get("interval", 5)))


if __name__ == "__main__":
    main()
