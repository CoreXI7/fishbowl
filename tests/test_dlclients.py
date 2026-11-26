"""Transmission and SABnzbd (app/dlclients.py): read-only clients whose answers are untrusted, plus how the app wires
them in (settings rules, which client the torrents card uses, the /api/sab feature switch)."""
import json
import sys
import unittest
import urllib.error

from helpers import APP_DIR, Upstream, load_app

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
import dlclients  # noqa: E402

QBIT_KEYS = {"client", "connection", "external_ip", "vpn_check", "vpn_leak", "dl_speed", "up_speed", "dl_total", "up_total",
             "dl_alltime", "up_alltime", "total", "downloading", "seeding", "seeding_active"}
SLIM_KEYS = {"name", "state", "category", "progress", "dlspeed", "upspeed", "eta", "size", "num_seeds", "num_leechs",
             "ratio", "added_on", "uploaded"}


def tor(name, **kw):
    return {"name": name, "status": 4, "labels": ["tv"], "percentDone": 0.5, "rateDownload": 100, "rateUpload": 0,
            "eta": 60, "sizeWhenDone": 1000, "peersSendingToUs": 3, "peersGettingFromUs": 1, "uploadRatio": 0.2,
            "addedDate": 1, "uploadedEver": 5, **kw}


class FakeTransmission:
    """Answers 409 with a session id until the request carries it, then whatever .reply() returns for the method."""
    def __init__(self, torrents=(), sid="abc123", always_409=False, stats=None):
        self.torrents, self.sid, self.always_409 = list(torrents), sid, always_409
        self.stats = stats or {}                   # extra / overriding top-level session-stats fields
        self.up = Upstream({"/transmission/rpc": self.route})

    def route(self, h):
        if self.always_409 or h.headers.get("X-Transmission-Session-Id") != self.sid:
            return 409, {"X-Transmission-Session-Id": self.sid}, "need session id"
        method = json.loads(self.up.bodies[-1])["method"]
        if method == "session-stats":
            args = {"current-stats": {"downloadedBytes": 10, "uploadedBytes": 20},
                    "cumulative-stats": {"downloadedBytes": 1000, "uploadedBytes": 2000}, "downloadSpeed": 0}
            args.update(self.stats)
        else:
            args = {"torrents": self.torrents}
        return 200, {}, {"result": "success", "arguments": args}

    def methods(self):
        return [json.loads(b)["method"] for b in self.up.bodies if b]


class TransmissionClient(unittest.TestCase):
    def tearDown(self):
        for f in getattr(self, "fakes", []):
            f.up.close()

    def fake(self, *a, **kw):
        f = FakeTransmission(*a, **kw)
        self.fakes = getattr(self, "fakes", []) + [f]
        return f

    def test_session_id_handshake_retries_once(self):
        f = self.fake([tor("A")])
        v = dlclients.transmission_view(dlclients.Transmission(f.up.url))
        self.assertEqual(v["total"], 1)
        self.assertEqual(f.up.hits[0][2].get("X-Transmission-Session-Id"), None)
        self.assertEqual(f.up.hits[1][2].get("X-Transmission-Session-Id"), "abc123")
        self.assertEqual(len(f.up.hits), 3)          # 409, session-stats, torrent-get (the id is kept)

    def test_409_twice_is_an_error(self):
        f = self.fake(always_409=True)
        with self.assertRaises(ValueError):
            dlclients.Transmission(f.up.url).call("session-stats")
        self.assertEqual(len(f.up.hits), 2)

    def test_bad_session_id_refused(self):
        for bad in ("abc def", "x" * 129, "<script>", ""):
            f = self.fake(sid=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                dlclients.Transmission(f.up.url).call("session-stats")
            self.assertEqual(len(f.up.hits), 1, repr(bad))   # never sent back

    def test_only_read_methods(self):
        c = dlclients.Transmission("http://127.0.0.1:9")
        for m in ("torrent-remove", "torrent-add", "session-set", "torrent-start"):
            with self.assertRaises(ValueError):
                c.call(m)

    def test_basic_auth_only_when_set(self):
        f = self.fake([])
        dlclients.Transmission(f.up.url).call("session-stats")
        self.assertTrue(all("Authorization" not in h for _, _, h in f.up.hits))
        f2 = self.fake([])
        dlclients.Transmission(f2.up.url, "me", "pw").call("session-stats")
        self.assertTrue(all(h.get("Authorization") == "Basic bWU6cHc=" for _, _, h in f2.up.hits))
        with self.assertRaises(ValueError):
            dlclients.Transmission(f2.up.url, "me", "")

    def test_session_speeds_are_read_from_the_top_level(self):          # issue #21
        f = self.fake([tor("A", rateDownload=7)], stats={"downloadSpeed": 1500, "uploadSpeed": 300})
        v = dlclients.transmission_view(dlclients.Transmission(f.up.url))
        self.assertEqual((v["dl_speed"], v["up_speed"]), (1500, 300))

    def test_speeds_fall_back_to_the_torrents_rates(self):
        f = self.fake([tor("A", rateDownload=7), tor("B", rateDownload=5), tor("S", status=6, rateDownload=0, rateUpload=40, percentDone=1)],
                      stats={"downloadSpeed": None})
        v = dlclients.transmission_view(dlclients.Transmission(f.up.url))
        self.assertEqual((v["dl_speed"], v["up_speed"]), (12, 40))

    def test_shape_matches_qbit_and_status_map(self):
        f = self.fake([tor("dl"), tor("seed", status=6, rateUpload=50, percentDone=1), tor("stopped-done", status=0, percentDone=1),
                       tor("stopped-half", status=0), tor("check", status=2), tor("queued", status=3),
                       tor("odd", status=99, eta=-1, percentDone=7)])
        v = dlclients.transmission_view(dlclients.Transmission(f.up.url))
        self.assertEqual(set(v), QBIT_KEYS)
        self.assertEqual(v["client"], "transmission")
        self.assertFalse(v["vpn_check"] or v["vpn_leak"])
        self.assertEqual((v["dl_alltime"], v["up_alltime"], v["dl_total"]), (1000, 2000, 10))
        states = {x["name"]: x["state"] for x in v["downloading"] + v["seeding"]}
        self.assertEqual(states, {"dl": "downloading", "seed": "uploading", "stopped-half": "stoppedDL",
                                  "check": "checkingDL", "queued": "queuedDL"})
        for x in v["downloading"] + v["seeding"]:
            self.assertEqual(set(x), SLIM_KEYS)
        self.assertEqual(f.methods(), ["session-stats", "session-stats", "torrent-get"])

    def test_hostile_values_are_typed_and_clipped(self):
        f = self.fake([tor("<img src=x onerror=alert(1)>" + "y" * 1000, status="4", labels=[{"x": 1}], percentDone=9e99,
                           rateDownload="lots", eta=-2)])
        t = dlclients.transmission_view(dlclients.Transmission(f.up.url))["downloading"][0]
        self.assertTrue(t["name"].startswith("<img src=x"))          # escaping is the page's job (esc())
        self.assertLessEqual(len(t["name"]), 300)
        self.assertEqual((t["category"], t["dlspeed"], t["progress"], t["eta"]), ("", 0, 1, 8640000))

    def test_non_dict_and_failed_replies(self):
        for body in ([], "null", {"result": "error"}, {"result": "success", "arguments": []}):
            up = Upstream({"/transmission/rpc": (200, {}, body)})
            try:
                with self.assertRaises(ValueError, msg=repr(body)):
                    dlclients.Transmission(up.url).call("session-stats")
            finally:
                up.close()

    def test_too_many_torrents(self):
        f = self.fake([tor(str(i)) for i in range(dlclients.MAX_TORRENTS + 1)])
        with self.assertRaises(ValueError):
            dlclients.transmission_view(dlclients.Transmission(f.up.url))

    def test_redirect_not_followed(self):
        up = Upstream({"/transmission/rpc": (302, {"Location": "/elsewhere"}, ""), "/elsewhere": (200, {}, {})})
        try:
            with self.assertRaises(urllib.error.HTTPError):
                dlclients.Transmission(up.url).call("session-stats")
            self.assertEqual([p for _, p, _ in up.hits], ["/transmission/rpc"])
        finally:
            up.close()


KEY = "sabkey-0123456789abcdef"


def queue(n=2, **kw):
    return {"queue": {"kbpersec": "100.5", "paused": False, "mbleft": "2.0", "timeleft": "0:10:00", "noofslots": n,
                      "slots": [{"filename": f"job{i}", "status": "Downloading", "percentage": "25", "mb": "4.0",
                                 "mbleft": "3.0", "timeleft": "0:05:00", "cat": "tv"} for i in range(n)], **kw}}


def history(n=3):
    return {"history": {"slots": [{"name": f"done{i}", "status": "Failed" if i == 0 else "Completed", "bytes": 1000,
                                   "fail_message": "Repair failed" if i == 0 else "", "completed": 1700000000, "category": "tv"}
                                  for i in range(n)]}}


class SabnzbdClient(unittest.TestCase):
    def sab(self, q, h):
        self.up = Upstream({"/api": lambda r: (200, {}, q if "mode=queue" in r.path else h)})
        self.addCleanup(self.up.close)
        return dlclients.Sabnzbd(self.up.url, KEY)

    def test_view(self):
        v = dlclients.sab_view(self.sab(queue(), history()))
        self.assertEqual(v["client"], "sabnzbd")
        self.assertEqual((v["speed"], v["size_left"], v["queue_total"]), (100.5 * 1024, 2 * 1048576, 2))
        self.assertEqual(v["queue"][0], {"name": "job0", "status": "Downloading", "progress": 0.25, "size": 4 * 1048576,
                                         "size_left": 3 * 1048576, "time_left": "0:05:00", "category": "tv"})
        self.assertEqual((v["history"][0]["status"], v["history"][0]["fail"]), ("Failed", "Repair failed"))
        paths = [p for _, p, _ in self.up.hits]
        self.assertTrue(any("mode=queue" in p and "output=json" in p for p in paths))
        self.assertTrue(any("mode=history" in p and "limit=10" in p for p in paths))

    def test_caps(self):
        v = dlclients.sab_view(self.sab(queue(80), history(30)))
        self.assertEqual((len(v["queue"]), len(v["history"]), v["queue_total"]), (50, 10, 80))

    def test_only_read_modes(self):
        c = dlclients.Sabnzbd("http://127.0.0.1:9", KEY)
        for m in ("addurl", "delete", "pause", "config"):
            with self.assertRaises(ValueError):
                c.get(m)

    def test_wrong_key_and_bad_replies_never_show_the_key(self):
        for q in ({"status": False, "error": f"API Key Incorrect {KEY}"}, [], "null"):
            c = self.sab(q, history())
            with self.assertRaises(ValueError) as cm:
                dlclients.sab_view(c)
            self.assertNotIn(KEY, str(cm.exception))

    def test_extra_params_refused(self):
        c = dlclients.Sabnzbd("http://127.0.0.1:9", KEY)
        for kw in ({"name": "delete"}, {"value": "all"}, {"limit": "10"}):
            with self.assertRaises(ValueError):
                c.get("queue", **kw)

    def test_negative_and_junk_slots(self):
        v = dlclients.sab_view(self.sab(queue(1, noofslots=-5, slots=[None] * 60 + [{"filename": "real"}]), history(0)))
        self.assertEqual(([r["name"] for r in v["queue"]], v["queue_total"]), (["real"], 1))

    def test_http_error_never_shows_the_key(self):
        self.up = Upstream({"/api": (500, {}, "boom")})
        self.addCleanup(self.up.close)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            dlclients.Sabnzbd(self.up.url, KEY).get("queue")
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn(KEY, cm.exception.url)

    def test_redirect_not_followed(self):
        self.up = Upstream({"/api": (302, {"Location": "/x"}, ""), "/x": (200, {}, queue())})
        self.addCleanup(self.up.close)
        with self.assertRaises(urllib.error.HTTPError):
            dlclients.Sabnzbd(self.up.url, KEY).get("queue")
        self.assertEqual(len(self.up.hits), 1)

    def test_hostile_values(self):
        v = dlclients.sab_view(self.sab(queue(1, slots=[{"filename": "<b>x</b>", "percentage": "250", "mb": {"a": 1},
                                                         "cat": "c" * 500}]), history(0)))
        row = v["queue"][0]
        self.assertEqual((row["name"], row["progress"], row["size"], len(row["category"])), ("<b>x</b>", 1, 0, 64))


class AppWiring(unittest.TestCase):
    def test_transmission_used_when_qbittorrent_is_not_set_up(self):
        f = FakeTransmission([tor("A")])
        self.addCleanup(f.up.close)
        app = load_app(TRANSMISSION_URL=f.up.url)
        self.assertTrue(app.TRANSMISSION["on"])
        self.assertEqual(app.qbit()["client"], "transmission")
        cfg = app.page_config()["features"]
        self.assertEqual((cfg["qbit"], cfg["sab"], cfg["downloads"]), (True, False, True))

    def test_transmission_login_both_or_neither(self):
        app = load_app(TRANSMISSION_URL="http://127.0.0.1:9", TRANSMISSION_USER="me")
        self.assertFalse(app.TRANSMISSION["on"])
        cfg = app.cfgstore
        old = cfg.load()
        new = json.loads(json.dumps(old))
        new["site"]["title"] = "other"                        # a half login from the environment doesn't block other saves
        cfg.apply_secret_changes(new, old, {}, {})            # (the Setup check warns about it instead)
        self.assertIn("transmission_login", {c["id"] for c in app.setup_checks()})
        app2 = load_app()
        cfg2 = app2.cfgstore
        old = cfg2.load()
        with self.assertRaises(cfg2.Invalid):
            cfg2.apply_secret_changes(json.loads(json.dumps(old)), old, {"tr_user": "me"}, {})
        store = cfg2.apply_secret_changes(json.loads(json.dumps(old)), old, {"tr_user": "me", "tr_pass": "pw"}, {})
        self.assertEqual((store["tr_user"], store["tr_pass"]), ("me", "pw"))

    def test_url_change_needs_the_sab_key_again(self):
        app = load_app()
        cfg = app.cfgstore
        old = cfg.load()
        old["sabnzbd"]["url"] = "http://sab:8080"
        new = json.loads(json.dumps(old))
        new["sabnzbd"]["url"] = "http://attacker:8080"
        with self.assertRaises(cfg.Invalid):
            cfg.apply_secret_changes(new, old, {}, {"sab_key": "saved"})
        self.assertEqual(cfg.apply_secret_changes(new, old, {"sab_key": "typed"}, {"sab_key": "saved"})["sab_key"], "typed")

    def test_sab_feature_and_secret_scrubbing(self):
        up = Upstream({"/api": (200, {}, {"status": False, "error": "bad"})})
        self.addCleanup(up.close)
        app = load_app(SAB_URL=up.url, SAB_KEY=KEY)
        self.assertTrue(app.SAB["on"])
        self.assertIn(KEY, app.SECRETS)
        self.assertNotIn(KEY, app.scrub(f"failed at {up.url}/api?apikey={KEY}"))
        self.assertFalse(load_app().page_config()["features"]["sab"])
        r = app.test_integration("sabnzbd")
        self.assertFalse(r["ok"])
        self.assertNotIn(KEY, r["detail"])


if __name__ == "__main__":
    unittest.main()
