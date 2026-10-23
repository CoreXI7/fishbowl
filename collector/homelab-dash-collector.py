#!/usr/bin/env python3
"""Root-side stats collector for homelab-dash (installed as /usr/local/sbin/homelab-dash-collector).

Writes <out_dir>/hw.json, which the dash container mounts read-only. Gathers only what the
container can't (or shouldn't) see itself: host network rate, GPU, drive usage, SMART health,
container counts, and optional security/backup checks. It never listens on the network.
SMART uses `-n standby` so sleeping drives are never woken.

Config: /etc/homelab-dash/collector.toml (see collector.example.toml). Every key is optional.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
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

    @staticmethod
    def total_jiffies():
        with open("/proc/stat") as f:
            fields = f.readline().split()[1:]
        return sum(int(x) for x in fields[:8])

    def container_id(self, pid, started):
        hit = self.container_of.get(pid)
        if hit and hit[0] == started:
            return hit[1]
        m = CONTAINER_RE.search(read_text(f"/proc/{pid}/cgroup"))
        cid = m[1] if m else None
        self.container_of[pid] = (started, cid)
        return cid

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
        for line in run(["journalctl", *units, "--since", "-7d", "-o", "short-iso", "--no-pager", "-q"], 30).splitlines():
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
        found = [p.split(None, 3)[2] for p in procs if any(m in p.lower() for m in watch) and "homelab-dash" not in p]
        top = []
        snap = run(["top", "-b", "-n", "2", "-d", "1", "-o", "%CPU", "-w", "200"]).split("\n\n")
        rows = [l.split() for l in (snap[-1].splitlines() if snap else []) if re.match(r"\s*\d+ ", l)]
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


def write_output(data):
    """Atomically replace hw.json, readable by root + the dash container's group only (not every user on the host)."""
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.chown(tmp, 0, DASH_UID)
    except PermissionError:               # not root (testing): leave ownership as is
        pass
    os.chmod(tmp, 0o640)
    os.replace(tmp, OUT)


def main():
    """Every round (default 5 s): refresh whatever is due, then write hw.json.
    Slow or expensive checks run less often: GPU 10 s, containers 30 s, drives + security + backups 60 s, SMART 5 min."""
    os.makedirs(OUT_DIR, exist_ok=True)
    os.chmod(OUT_DIR, 0o755)
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

        data["updated"] = int(time.time())
        write_output(data)
        time.sleep(int(CFG.get("interval", 5)))


if __name__ == "__main__":
    main()
