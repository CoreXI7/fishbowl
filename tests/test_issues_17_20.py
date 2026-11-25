"""GitHub issues #17-#20: icon names in any case (#17), Jellyfin now playing wired into the app (#18; the session mapping
itself is in test_jellyfin.py), service names with spaces (#19) and the Downloads card's All tab (#20)."""
import json
import time
import unittest
import urllib.error
import urllib.request

from browser import CHROME, Browser
from helpers import Upstream, load_app, serve

KEY = "jf-test-key-6c1f0e"
ITEM = "0123456789abcdef0123456789abcdef"
SVG_ITEM = "fedcba9876543210fedcba9876543210"
SESSION = {"Id": "s1", "UserName": "alice", "Client": "Jellyfin Web", "DeviceName": "Firefox", "RemoteEndPoint": "10.0.0.7",
           "PlayState": {"PositionTicks": 6_000_000_000, "IsPaused": False, "PlayMethod": "Transcode"},
           "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": True, "Bitrate": 8_000_000, "Height": 1080,
                               "HardwareAccelerationType": "vaapi"},
           "NowPlayingItem": {"Id": "aaaa", "Type": "Episode", "Name": "Pilot", "SeriesName": "Show", "SeriesId": ITEM,
                              "ParentIndexNumber": 1, "IndexNumber": 1, "RunTimeTicks": 24_000_000_000}}


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


class Jellyfin(unittest.TestCase):
    def setUp(self):
        self.up = Upstream({"/Sessions": (200, {}, [SESSION, {"Id": "idle", "UserName": "bob"}]),
                            f"/Items/{ITEM}/Images/Primary": (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff fake jpeg"),
                            f"/Items/{SVG_ITEM}/Images/Primary": (200, {"Content-Type": "image/svg+xml"}, "<svg onload=alert(1)>")})
        self.addCleanup(self.up.close)

    def test_now_playing_from_jellyfin(self):
        app = load_app(JELLYFIN_URL=self.up.url, JELLYFIN_KEY=KEY)
        d = app.plex()
        self.assertEqual((d["stream_count"], d["transcodes"], d["bandwidth"]), (1, 1, 8000))
        s = d["sessions"][0]
        self.assertEqual((s["server"], s["friendly_name"], s["grandparent_title"], s["art"]), ("jellyfin", "alice", "Show", f"/api/jfart?id={ITEM}"))
        method, path, headers = self.up.hits[-1]
        self.assertEqual(path, "/Sessions?activeWithinSeconds=960")
        self.assertIn(f'Token="{KEY}"', headers.get("Authorization", ""), "the key goes in a header")
        self.assertNotIn(KEY, path, "never in the URL")
        self.assertNotIn(KEY, json.dumps(d), "never to the page")
        cfg = app.page_config()["features"]
        self.assertEqual((cfg["plex"], cfg["jellyfin"], cfg["tautulli"]), (True, True, False))

    def test_key_is_scrubbed_from_errors(self):
        """(review) An error text that echoes the key must not show it."""
        app = load_app(JELLYFIN_URL=self.up.url, JELLYFIN_KEY=KEY)
        self.assertNotIn(KEY, app.scrub(f"upstream said: bad token {KEY}"))

    def test_test_button(self):
        app = load_app(JELLYFIN_URL=self.up.url, JELLYFIN_KEY=KEY)
        r = app.test_integration("jellyfin")
        self.assertEqual((r["ok"], r["detail"]), (True, "connected · 1 streams now"))

    def test_posters_through_the_dashboard(self):
        app = load_app(JELLYFIN_URL=self.up.url, JELLYFIN_KEY=KEY)
        srv, url = serve(app.Handler)
        self.addCleanup(srv.shutdown)
        code, ctype, body = get(f"{url}/api/jfart?id={ITEM}")
        self.assertEqual((code, ctype, body), (200, "image/jpeg", b"\xff\xd8\xff fake jpeg"))
        self.assertIn(f'Token="{KEY}"', self.up.hits[-1][2].get("Authorization", ""))
        for bad in (SVG_ITEM, "../../System/Info", ITEM[:-1], ITEM + "0", ""):
            hits = len(self.up.hits)
            self.assertEqual(get(f"{url}/api/jfart?id={bad}")[0], 404, bad)
            if bad != SVG_ITEM:
                self.assertEqual(len(self.up.hits), hits, f"{bad!r}: refused before asking Jellyfin")

    def test_one_source_down_keeps_the_other(self):
        dead = Upstream({})
        dead.close()                                        # nothing listens there any more
        app = load_app(JELLYFIN_URL=self.up.url, JELLYFIN_KEY=KEY, TAUTULLI_URL=dead.url, TAUTULLI_KEY="t")
        d = app.plex()
        self.assertEqual(d["stream_count"], 1, "Tautulli failing leaves Jellyfin's streams")
        app2 = load_app(JELLYFIN_URL=dead.url, JELLYFIN_KEY=KEY)
        with self.assertRaises(Exception):
            app2.plex()                                      # every configured source down: an error, as before


class NamesAndIcons(unittest.TestCase):
    def validate(self, app, **change):
        return app.cfgstore.validate({**app.ED, **change}, {})

    def test_icon_names_in_any_case(self):
        app = load_app()
        apps = self.validate(app, apps={"a.example.com": {"icon": "Sabnzbd"}, "b.example.com": {"icon": "  Home Assistant "},
                                        "c.example.com": {"icon": "/uploads/x.png"}})["apps"]
        self.assertEqual([apps[h]["icon"] for h in ("a.example.com", "b.example.com", "c.example.com")],
                         ["sabnzbd", "home-assistant", "/uploads/x.png"])
        with self.assertRaises(app.cfgstore.Invalid):
            self.validate(app, apps={"a.example.com": {"icon": "Sab<nzbd>"}})

    def test_service_names_with_spaces(self):
        app = load_app()
        st = self.validate(app, services=[{"name": "Home Assistant", "url": "http://10.0.0.9:8123"}],
                           groups=[{"name": "Home", "hosts": ["Home Assistant"]}], apps={"Home Assistant": {"icon": "home-assistant"}},
                           skip_hosts=["Old Thing"])
        self.assertEqual((st["services"][0]["name"], st["groups"][0]["hosts"], list(st["apps"]), st["skip_hosts"]),
                         ("Home Assistant", ["Home Assistant"], ["Home Assistant"], ["Old Thing"]))
        for bad in ("__proto__", "a\tb", "a<b", "x" * 300):
            with self.assertRaises(app.cfgstore.Invalid, msg=bad):
                self.validate(app, services=[{"name": bad, "url": "http://10.0.0.9"}])
        self.assertEqual(self.validate(app, services=[{"name": "  NAS  ", "url": "http://10.0.0.9"}])["services"][0]["name"], "NAS")


class AllTab(unittest.TestCase):
    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_all_tab_with_torrents_and_usenet(self):
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        b = Browser(1440, 900)
        try:
            b.send("Page.navigate", {"url": url + "/"})
            self.assertTrue(b.wait_for("!!document.querySelector('#all .tor')", 20), "the All list fills")
            r = b.js("""(() => ({shown: !$('t-all').hidden, on: $('t-all').classList.contains('on'), dlHidden: $('dl').hidden,
                kinds: [...new Set([...document.querySelectorAll('#all .src')].map(x => x.textContent))],
                label: $('t-all').textContent}))()""")
            self.assertEqual((r["shown"], r["on"], r["dlHidden"], sorted(r["kinds"])), (True, True, True, ["Torrent", "Usenet"]))
            self.assertRegex(r["label"], r"^All \(\d+\)$")
            b.js("$('t-dl').click()")
            time.sleep(4)                                     # a refresh must not take the user's tab back
            self.assertEqual(b.js("[$('dl').hidden, $('all').hidden]"), [False, True])
        finally:
            b.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
