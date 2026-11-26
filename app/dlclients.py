"""Transmission and SABnzbd clients: read-only views of the download clients for the dashboard.

Both clients only ever call read methods, and everything an upstream reports is typed and clipped before the page
sees it (the clients are attacker-controlled too: a taken-over container can answer anything)."""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from upstream import (API_MAX, QBIT_MAX, UA, _DeadlineHTTP, _DeadlineHTTPS, _NoRedirect, num, read_json, text)

MB = 1 << 20

_SESSION_ID = re.compile(r"[A-Za-z0-9]{1,128}")


# ---------- Transmission ----------

ALLOWED = ("torrent-get", "session-stats")

TR_FIELDS = [
    "name", "status", "labels", "percentDone", "rateDownload", "rateUpload", "eta", "sizeWhenDone",
    "peersSendingToUs", "peersGettingFromUs", "uploadRatio", "addedDate", "uploadedEver",
]

MAX_TORRENTS = 20_000

_TR_STATUS = {1: "checkingDL", 2: "checkingDL", 3: "queuedDL", 4: "downloading", 5: "queuedUP", 6: "uploading"}


class Transmission:
    """Transmission's RPC API over plain HTTP. Keeps the session id (a 409 hands it out) and retries once."""

    def __init__(self, url, user="", password=""):
        if bool(user) != bool(password):
            raise ValueError("Transmission needs both a username and a password, or neither")
        self.url = url.rstrip("/")
        self.user, self.password = user, password
        self.session_id = ""
        self.op = urllib.request.build_opener(_NoRedirect, _DeadlineHTTP, _DeadlineHTTPS())

    def _headers(self):
        """The request headers: JSON, the session id once known, and Basic auth only when a username is set."""
        headers = {"Content-Type": "application/json", "User-Agent": UA}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = "Basic " + token
        return headers

    def call(self, method, fields_or_args=None):
        """POST one allow-listed RPC method and return its "arguments" dict. A 409 carries the session id: store it
        (checked for charset and length) and retry exactly once."""
        if method not in ALLOWED:
            raise ValueError("Transmission only gets read methods")
        args = (fields_or_args if isinstance(fields_or_args, dict) else
                {"fields": list(fields_or_args)} if isinstance(fields_or_args, (list, tuple)) else {})
        body = json.dumps({"method": method, "arguments": args}).encode("utf-8")
        d = None
        for attempt in (1, 2):
            req = urllib.request.Request(self.url + "/transmission/rpc", data=body, headers=self._headers(),
                                         method="POST")
            try:
                with self.op.open(req, timeout=10) as r:
                    d = read_json(r, QBIT_MAX)
                break
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    e.close()
                    raise urllib.error.HTTPError(self.url + "/transmission/rpc", e.code, "", e.headers, None) from None
                sid = e.headers.get("X-Transmission-Session-Id") if e.headers else None
                e.close()
                if attempt == 2:
                    raise ValueError("Transmission kept asking for its session id") from None
                if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
                    raise ValueError("bad session id from Transmission") from None
                self.session_id = sid
        if not isinstance(d, dict):
            raise ValueError("unexpected answer from Transmission")
        if d.get("result") != "success":
            raise ValueError("Transmission refused the request")
        reply = d.get("arguments")
        if not isinstance(reply, dict):
            raise ValueError("unexpected answer from Transmission")
        return reply


def transmission_view(client, home_ip=""):
    """/api/qbit for Transmission: the same dict qbit() returns, with the status map, the same split/sort/caps
    and client="transmission". Transmission has no external IP, so the VPN check is always off."""
    stats = client.call("session-stats")
    args = client.call("torrent-get", {"fields": TR_FIELDS})
    rows = args.get("torrents")
    rows = rows if isinstance(rows, list) else []
    if len(rows) > MAX_TORRENTS:
        raise ValueError(f"Transmission reports more than {MAX_TORRENTS} torrents")
    slim = [_slim(x) for x in rows if isinstance(x, dict)]
    dl_states = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "checkingDL", "pausedDL", "stoppedDL"}
    downloading = [x for x in slim if x["state"] in dl_states]
    downloading.sort(key=lambda x: (-x["dlspeed"], -x["progress"]))
    seeding = [x for x in slim if x["upspeed"] > 0 and x["state"] not in dl_states]
    seeding.sort(key=lambda x: -x["upspeed"])
    cur = stats.get("current-stats")
    cur = cur if isinstance(cur, dict) else {}
    cum = stats.get("cumulative-stats")
    cum = cum if isinstance(cum, dict) else {}
    # The session speeds are top-level in session-stats; current-stats only has byte counters (#21). If a
    # Transmission leaves them out, add up the torrents' own rates instead.
    dl_speed, up_speed = num(stats.get("downloadSpeed"), None), num(stats.get("uploadSpeed"), None)
    if dl_speed is None:
        dl_speed = sum(x["dlspeed"] for x in slim)
    if up_speed is None:
        up_speed = sum(x["upspeed"] for x in slim)
    return {
        "connection": "connected",
        "external_ip": "",
        "vpn_check": False,
        "vpn_leak": False,
        "dl_speed": dl_speed, "up_speed": up_speed,
        "dl_total": num(cur.get("downloadedBytes")), "up_total": num(cur.get("uploadedBytes")),
        "dl_alltime": num(cum.get("downloadedBytes")), "up_alltime": num(cum.get("uploadedBytes")),
        "total": len(slim),
        "downloading": downloading, "seeding": seeding[:30], "seeding_active": len(seeding),
        "client": "transmission",
    }


def _slim(x):
    """One Transmission torrent mapped to the slim keys the page shows (same as qbit()'s)."""
    status = num(x.get("status"), None)
    if status is None:
        state = "unknown"
    elif status == 0:
        state = "stoppedUP" if num(x.get("percentDone")) >= 1 else "stoppedDL"
    else:
        state = _TR_STATUS.get(int(status), "unknown")
    labels = x.get("labels")
    category = labels[0] if isinstance(labels, list) and labels and isinstance(labels[0], str) else ""
    return {
        "name": text(x.get("name")),
        "state": state,
        "category": text(category),
        "progress": min(max(num(x.get("percentDone")), 0), 1),
        "dlspeed": num(x.get("rateDownload")),
        "upspeed": num(x.get("rateUpload")),
        "eta": eta if (eta := num(x.get("eta"))) >= 0 else 8640000,   # (-1/-2: unknown; qBittorrent's "infinite")
        "size": num(x.get("sizeWhenDone")),
        "num_seeds": num(x.get("peersSendingToUs")),
        "num_leechs": num(x.get("peersGettingFromUs")),
        "ratio": num(x.get("uploadRatio")),
        "added_on": num(x.get("addedDate")),
        "uploaded": num(x.get("uploadedEver")),
    }


# ---------- SABnzbd ----------

class Sabnzbd:
    """SABnzbd's HTTP API. Only the queue and history (read) modes; the API key travels in the query string."""

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key
        self.op = urllib.request.build_opener(_NoRedirect, _DeadlineHTTP, _DeadlineHTTPS())

    def get(self, mode, **params):
        """GET one allow-listed mode as JSON and return the parsed object. Messages never carry the key or the URL."""
        if mode not in ("queue", "history"):
            raise ValueError("SABnzbd only gets read modes")
        if set(params) - {"limit"} or not all(type(v) is int for v in params.values()):
            raise ValueError("SABnzbd: only a numeric limit is passed")   # (queue/history also take delete/purge arguments)
        query = {"mode": mode, "output": "json", "apikey": self.key}
        query.update(params)
        req = urllib.request.Request(self.url + "/api?" + urllib.parse.urlencode(query),
                                     headers={"User-Agent": UA})
        try:
            with self.op.open(req, timeout=10) as r:
                d = read_json(r, API_MAX)
        except urllib.error.HTTPError as e:   # (its url and reason could carry the key: replace them)
            e.close()
            raise urllib.error.HTTPError(self.url + "/api", e.code, "", e.headers, None) from None
        if not isinstance(d, dict):
            raise ValueError("unexpected answer from SABnzbd")
        if d.get("status") is False:            # (its error text is not repeated: it can echo the key)
            raise ValueError("SABnzbd refused the request (wrong API key?)")
        return d


def sab_view(client):
    """/api/sab: the queue (up to 50 rows) and the last 10 history jobs, trimmed to what the page shows."""
    queue = client.get("queue")
    history = client.get("history", limit=10)
    qtop = queue.get("queue")
    qtop = qtop if isinstance(qtop, dict) else {}
    htop = history.get("history")
    htop = htop if isinstance(htop, dict) else {}
    qslots = qtop.get("slots")
    qslots = [s for s in qslots if isinstance(s, dict)] if isinstance(qslots, list) else []
    hslots = htop.get("slots")
    hslots = hslots if isinstance(hslots, list) else []
    paused = qtop.get("paused")
    total = max(num(qtop.get("noofslots")), 0)
    return {
        "client": "sabnzbd",
        "speed": num(qtop.get("kbpersec")) * 1024,
        "paused": paused if isinstance(paused, bool) else False,
        "size_left": num(qtop.get("mbleft")) * MB,
        "time_left": text(qtop.get("timeleft"), 40),
        "queue": [_sab_queue_row(s) for s in qslots[:50]],
        "queue_total": total if total else len(qslots),
        "history": [_sab_history_row(s) for s in hslots if isinstance(s, dict)][:10],
    }


def _sab_queue_row(s):
    """One queue slot: names and status text, sizes from mb/mbleft, percentage -> progress 0..1."""
    progress = num(s.get("percentage")) / 100
    return {
        "name": text(s.get("filename")),
        "status": text(s.get("status"), 40),
        "progress": min(max(progress, 0), 1),
        "size": num(s.get("mb")) * MB,
        "size_left": num(s.get("mbleft")) * MB,
        "time_left": text(s.get("timeleft"), 40),
        "category": text(s.get("cat"), 64),
    }


def _sab_history_row(s):
    """One history job: bytes as-is, the failure message and the Unix finish time."""
    return {
        "name": text(s.get("name")),
        "status": text(s.get("status"), 40),
        "size": num(s.get("bytes")),
        "fail": text(s.get("fail_message")),
        "completed": num(s.get("completed")),
        "category": text(s.get("category"), 64),
    }
