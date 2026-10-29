# Performance baseline (2026-10-29, before the hardening work)

Measured on the reference install (Ryzen 9 9950X, 32 threads, Ubuntu 24.04, ~1,100 processes, ~55 containers,
a busy SSH log: one machine logs in ~400 times a day). Code: main at the start of the work. Every "after" number in
the hand-off is measured the same way, with the same tools (kept outside the repo: they're plain stdlib scripts).

## Method

| What | How |
|---|---|
| Collector CPU | cgroup `cpu.stat` `usage_usec` of `homelab-dash-collector.service`, sampled every second for 10 min (same counter as `systemctl show -p CPUUsageNSec`) |
| Collector memory | cgroup `memory.current` every second, `memory.peak` and `memory.events` (`max` = times it hit `MemoryMax`) |
| Collector round time | the mtime of `hw.json` / `sec-detail.json` polled every 50 ms for 10 min: a normal round writes every 5 s (the sleep) + the round's work |
| Each collector command | run once as an unprivileged user with the collector's exact arguments, warm cache: wall time, CPU time (`getrusage`), output size. Root-only commands (`fail2ban-client`, `ufw`) are not in this table |
| Dash container | cgroup `cpu.stat` / `memory.current` / `memory.peak` every second for 10 min with the real wall display (and one desktop browser) open, plus `docker stats --no-stream` at the end |
| Dash server per request | each endpoint function + `json.dumps`, 300 calls, `time.process_time()`, on a copy of the live `hw.json` / `sec-detail.json` |
| Browser | headless Chrome (`--headless=new`, software rendering, so absolute CPU runs high; compare runs, not to a GPU desktop), 1920×1080, `?wall`, 20 s to settle, then 180 s: CPU of Chrome's whole process tree from `/proc`, `Performance.getMetrics` deltas (after a forced GC), bytes received (`encodedDataLength`) per path, a 20 s trace for paint/raster counts, and the first load's bytes by type. Demo-mode runs use a DEMO=1 copy bound to 127.0.0.1, timed to the demo's all-up stretch (its fake "uptime" service is down 120 s of every 360 s, which starts the pulse animation) |
| Desktop wall (real GPU) | CPU ticks of the wall display's own Chrome profile (all its processes), same 10-min window |

## Results

### Root collector

| Metric | Value |
|---|---|
| CPU | **4.52 s per minute** (7.5% of one core) |
| Memory in use | 55–73 MiB (anon ~27 MiB; the rest is page cache) |
| `memory.peak` since its last restart | **400 MiB = `MemoryMax`**, 5,128 `max` events, none during the 10-min window: they happened during the nightly-style `du` of the backup folders right after the restart (kernel dentry/inode cache is charged to the service's cgroup) |
| Round interval | 5.02 s normally; **9.1–9.2 s once a minute**: the security round blocks the main loop for ~4.1 s, so every tile waits that long |
| Detail file interval | ~64.3 s |

Per command (security round runs once a minute; the detail thread once a minute in parallel):

| Command | Runs per minute | CPU | Wall | Output |
|---|---|---|---|---|
| `journalctl -u ssh -u sshd --since -7d -n 100000 -o short-iso` | 2 (card + details) | **1.47 s** each | 1.47 s | 1.2 MB, 9,000 lines |
| `journalctl -u ssh -u sshd --since -24h -n 100000` | 1 | **1.24 s** | 1.25 s | 0.4 MB |
| the same two without `-n` | | 0.29 s / 0.06 s | | same |
| `top -b -n 2 -d 1` | 1 | 0.03 s | **1.23 s** (sleeps 1 s) | 190 KB |
| `docker logs --since 24h authelia` | 1 | 0.02 s | 0.02 s | 2.0 MB (since its log cap) |
| `ps -eo ...` | 2 | 0.02 s | 0.02 s | 100 KB |
| `docker ps -a`, `nvidia-smi`, `findmnt`, `systemctl list-timers` | 2, 6, 1, 1 | ≤0.02 s | | small |

`journalctl` with `-n 100000` accounts for ~4.2 of the 4.5 CPU-seconds a minute: with `-n` it walks the whole
journal (2.1 GB here) backwards instead of seeking to `--since`.

### Dash server

| Endpoint | CPU per request | Response |
|---|---|---|
| `/api/system` (every 3 s per open page) | 0.80 ms | 13.8 KB, of which 6.2 KB is the CPU/memory breakdown data |
| `/api/live` (every 1 s while the live window is open) | 1.03 ms | 3.7 KB |
| `/api/security/detail` (every 30 s while a detail view is open) | 0.12 ms | 43 KB |
| `GET /` | 0.01 ms | 95 KB |
| gzip level 6 of `/api/system` | 0.07 ms | 13.8 KB → 3.2 KB |

The reverse proxy compresses only `text/html`: JSON and CSS go out uncompressed.

Container over 10 min (the real wall display open, 601 one-second samples, no restart during the window):

| Metric | Value |
|---|---|
| CPU | **1.19% of one core** (0.71 CPU-s/min; per-minute range 1.13–1.41%) |
| Memory | 40.0–51.4 MiB (avg 41.1); `memory.peak` since its start 68 MiB of the 256 MiB cap; 1 process |
| `docker stats` at the end | 0.01% CPU, 40.4 MiB |
| Public demo container, same window | 0.32% of one core, 35 MiB |
| Wall display's own Chrome (real GPU desktop) | 5.3% of one core |
| Root collector, same window | 4.50 CPU-s/min (matches the first sample) |
| Container network, received | **37.6 MB/min** (60 s of the container's `eth0` counter; 17.9 GB in its first 4.4 hours). Nearly all of it is qBittorrent's full `torrents/info` list (1,504 torrents, ~2.9 MB) fetched every 4 s for `/api/qbit` |

### Browser, wall view 1920×1080, 180 s

| Run | CPU (one core = 100%) | KB/min received | Requests/min | Layouts/min | Style recalcs/min | Paints in 20 s | First load |
|---|---|---|---|---|---|---|---|
| live data, Gura | **52.0%** | **337** (`/api/system` 270, `/api/qbit` 57) | 42 | 35 | 36 | 159 | 1.63 MB (icons 1.1 MB, fonts 204 KB, CSS 68 KB, HTML 37 KB) |

Demo mode (DEMO=1 copy on 127.0.0.1, same 1920×1080 `?wall`, 180 s each, timed to the demo's phases):

| Run | CPU | KB/min | Requests/min | Layouts/min | Style recalcs/min | Paints / rasters in 20 s | First load |
|---|---|---|---|---|---|---|---|
| Bubbles, all up | **18.7%** | 154 | 42 | 38 | 52 | 98 / 225 | 1.17 MB |
| Miku, all up | **27.9%** | 154 | 42 | 38 | **3,606** | **2,489** / 1,420 | 1.23 MB |
| Gura, all up | **47.1%** | 154 | 42 | 39 | 45 | 99 / 219 | 1.17 MB |
| Bubbles, one service down | **26.3%** | 152 | 42 | 36 | **3,606** | **2,432** / 1,410 | 1.16 MB |

What the numbers say:

- **Miku:** 3,606 style recalcs a minute is 60 a second: the equalizer bars animate `background-size`, which can't
  run on the compositor, so the main thread restyles and repaints every frame (+9 points over Bubbles).
- **"One service down":** the same 60-a-second pattern. The down tile's pulse animates `box-shadow`, so a single down
  service costs +7.6 points for as long as it stays down.
- **Gura:** restyles and paints are as low as Bubbles' (its two animations are transform-only), yet it costs 2.5× the
  CPU: the cost is compositing. The full-height animated layers (light rays, the swimmer) get re-composited every frame.
- Bytes and requests are identical across themes: polling, not theme, sets the network cost.

DOM nodes and event listeners stay flat over the run (no leaks), JS heap ~1 MB, script time ~0.016 s a minute:
the page's own JavaScript is cheap. Almost all of the cost is compositing and painting the animations.

## After the hardening (2026-10-29, the same measurements on the live install)

| Metric | Before | After |
|---|---|---|
| Root collector CPU (10 min) | 4.52 CPU-s/min | **0.95 CPU-s/min** |
| Collector memory | 55–73 MiB | 50–197 MiB; the high end is page cache (52 MB anon, 90 MB file, reclaimable), 0 `max` events |
| Round interval, once a minute | 9.1–9.2 s | **6.85–7.4 s** (normal rounds 5.02 s, as before) |
| Detail file interval | ~64.3 s | ~62.1 s |
| Collector minute, security round + SSH details (unprivileged run, same journal) | 3.85 + 1.47 s wall, 4.06 CPU-s | 1.55 + 0.03 s wall, 0.38 CPU-s |
| Dash container CPU | 1.19% of a core | **0.72%** |
| Dash container memory | 40–51 MiB | 25–39 MiB |
| Dash container network received | 37.6 MB/min | **1.77 MB/min** (the torrent feed ~0.3 MB of it; the rest is the 54 health checks) |
| `/api/system` per response | 13.8 KB | 3.2 KB (gzip) |
| Wall display's own Chrome (real GPU) | 5.3% | 5.6% (same) |

Headless Chrome, wall 1920×1080, 180 s:

| Run | Before | After |
|---|---|---|
| Live data, Gura: CPU / KB per min / first load | 52.0% / 337 / 1.63 MB | 48.0% / 101 / 1.52 MB (fonts 204 → 96 KB, CSS 68 → 18 KB gzipped; the rest is the service icons from the CDN) |
| Demo, Bubbles, all up | 18.7% | (unchanged look and cost) |
| Demo, Miku, all up | 27.9%, 3,606 restyles/min | **18.8%**, 50 restyles/min |
| Demo, Bubbles, one service down | 26.3%, 3,606 restyles/min | **17.7%**, 48 restyles/min |
| Demo, Gura, all up | 47.1% | 47.1%: the swaying light rays (a large masked layer) are re-composited every frame. Look-alike variants (stepped, smaller layer, rotation only) cost the same; only static rays reach 18.9% |
