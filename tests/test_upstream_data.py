"""Data from qBittorrent, Seerr and Tautulli is attacker-controlled (a taken-over container can answer anything).
The server must pass on only the types the page expects: numbers stay numbers, ids are ints, text is clipped."""
import unittest

from helpers import Upstream, load_app

EVIL = '<img src=x onerror="window.__xss=1">'


def evil_torrent(state):
    return {"name": "ok name", "state": state, "progress": EVIL, "dlspeed": EVIL, "upspeed": 5 if state == "uploading" else EVIL,
            "eta": EVIL, "size": EVIL, "num_seeds": EVIL, "num_leechs": EVIL, "ratio": EVIL, "category": ["x"],
            "added_on": EVIL, "uploaded": EVIL, "extra_field": "x" * 10_000}


class UpstreamTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = Upstream({
            "/api/v2/auth/login": (200, {"Set-Cookie": "SID=1; path=/"}, "Ok."),
            "/api/v2/transfer/info": (200, {}, {"connection_status": EVIL, "last_external_address_v4": EVIL,
                                                "dl_info_speed": EVIL, "up_info_speed": "1", "dl_info_data": None, "up_info_data": [1]}),
            "/api/v2/sync/maindata": (200, {}, {"rid": 1, "full_update": True,
                                                 "torrents": {"a": evil_torrent("downloading"), "b": evil_torrent("uploading"),
                                                              "c": EVIL}}),
            "/api/v1/request": (200, {}, {"results": [
                {"id": "1);window.__xss=1;//", "type": "movie", "media": {"tmdbId": 5}, "seasons": [], "requestedBy": {}},
                {"id": 7, "type": "tv", "media": {"tmdbId": "../../status"}, "seasons": [{"seasonNumber": EVIL}, {"seasonNumber": 2}],
                 "requestedBy": {"displayName": "x" * 5000}, "createdAt": EVIL, "is4k": EVIL},
                {"id": 8, "type": "../../x", "media": {"tmdbId": 6}, "seasons": [], "requestedBy": {}}]}),
            "/api/v1/tv/../../status": (200, {}, {}),
            "/api/v1/movie/5": (200, {}, {"title": "t", "releaseDate": EVIL, "posterPath": "@evil.example/x.png", "overview": 5}),
            "/api/v2": (200, {}, {"response": {"data": {"stream_count": "1", "sessions": [
                {"media_type": "episode", "title": EVIL, "parent_media_index": EVIL, "media_index": EVIL, "year": EVIL,
                 "progress_percent": EVIL, "bandwidth": EVIL, "duration": "1000", "view_offset": EVIL, "state": ["x"],
                 "grandparent_thumb": "/library/metadata/1/thumb/2"}]}}}),
        })
        cls.app = load_app(QBIT_URL=cls.up.url, QBIT_USER="u", QBIT_PASS="p", SEERR_URL=cls.up.url, SEERR_KEY="k",
                           TAUTULLI_URL=cls.up.url, TAUTULLI_KEY="k")

    @classmethod
    def tearDownClass(cls):
        cls.up.close()

    def assertNumbers(self, d, keys):
        for k in keys:
            self.assertIsInstance(d[k], (int, float), k)
            self.assertNotIsInstance(d[k], bool, k)

    def test_qbit_numbers_are_numbers(self):
        q = self.app.qbit()
        self.assertNumbers(q, ("dl_speed", "up_speed", "dl_total", "up_total", "total", "seeding_active"))
        self.assertTrue(q["downloading"] and q["seeding"])
        for t in q["downloading"] + q["seeding"]:
            self.assertNumbers(t, ("progress", "dlspeed", "upspeed", "eta", "size", "num_seeds", "num_leechs",
                                   "ratio", "added_on", "uploaded"))
            for k in ("name", "state", "category"):
                self.assertIsInstance(t[k], str, k)
            self.assertNotIn("extra_field", t)
        for k in ("connection", "external_ip"):
            self.assertIsInstance(q[k], str)
            self.assertLessEqual(len(q[k]), 100)

    def test_seerr_ids_and_seasons(self):
        out = self.app.requests_pending()["pending"]
        self.assertEqual([r["id"] for r in out], [7])          # a non-int id or an unknown type can't be shown (or approved)
        r = out[0]
        self.assertEqual(r["seasons"], [2])
        self.assertIsInstance(r["is4k"], bool)
        self.assertLessEqual(len(r["user"]), 200)
        self.assertIsInstance(r["created"], str)
        self.assertFalse(any("status" in h[1] for h in self.up.hits))   # the tmdb id is never pasted into a URL unchecked

    def test_seerr_media_info(self):
        info = self.app.media_info("movie", 5)
        self.assertEqual(info["year"], "")
        self.assertIsNone(info["poster"])
        self.assertIsInstance(info["overview"], str)

    def test_plex_numbers(self):
        s = self.app.plex()["sessions"][0]
        self.assertNumbers(s, ("parent_media_index", "media_index", "progress_percent", "bandwidth", "duration", "view_offset"))
        self.assertEqual(s["duration"], 1000)
        self.assertIsInstance(s["state"], str)
        self.assertIsInstance(s["title"], str)

class UpstreamTextCharacters(unittest.TestCase):
    def test_control_and_bidi_characters_are_dropped(self):
        # (torrent names and request titles kept RLO, ESC and NUL: a name could pass for another on the page; audit round 3)
        load_app()
        import upstream
        self.assertEqual(upstream.text("good\u202etxt.exe\x1b[31m\x00\u061cend"), "goodtxt.exe[31mend")


if __name__ == "__main__":
    unittest.main()
