"""What the dash reads about the host itself: per-CPU load, CPU model and topology, clocks, temperature sensors and
disk I/O, from the host's /proc and /sys (mounted into the container). No settings, no network."""
import functools
import glob
import os
import re
import time


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


# ---------- for the live window (/api/live) ----------

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
