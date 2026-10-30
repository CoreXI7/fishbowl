"""Size caps alone don't bound memory: 8 MB of "[],[],..." parses into ~180 MB of Python objects. Upstream JSON with
more values than any real answer is refused before parsing, and the qBittorrent sync state can't grow without end."""
import unittest

from helpers import Upstream, load_app


class UpstreamMemory(unittest.TestCase):
    def setUp(self):
        self.answers = []
        self.up = Upstream({"/api/v2/auth/login": (200, {}, "Ok."),
                            "/api/v2/transfer/info": (200, {}, {}),
                            "/api/v2/sync/maindata": lambda h: self.answers.pop(0),
                            "/api/v1/request": (200, {}, b'{"results": [' + b"[]," * 1_500_000 + b"[]]}")})
        self.app = load_app(QBIT_URL=self.up.url, QBIT_USER="u", QBIT_PASS="p", SEERR_URL=self.up.url, SEERR_KEY="k")

    def tearDown(self):
        self.up.close()

    def test_too_many_values_refused_before_parsing(self):
        with self.assertRaisesRegex(ValueError, "too many values"):
            self.app.requests_pending()

    def test_sync_state_is_bounded(self):
        n = self.app.MAX_TORRENTS
        self.answers = [(200, {}, {"rid": i + 1, "torrents": {f"{i}-{j}": {"name": "x"} for j in range(n // 2 + 1)}})
                        for i in range(3)]
        self.app.qbit()
        with self.assertRaisesRegex(ValueError, "more than"):
            self.app.qbit()
        self.assertEqual((self.app.qb.rid, len(self.app.qb.state)), (0, 0), "starts over with a full list next time")

    def test_stored_values_are_typed(self):
        self.answers = [(200, {}, {"rid": 1, "full_update": True, "torrents": {"a": {"name": "n" * 5000, "size": "big"}}})]
        self.app.qbit()
        stored = self.app.qb.state["a"]
        self.assertLessEqual(len(stored["name"]), 300)
        self.assertEqual(stored["size"], 0)


    def test_torrent_ids_stay_short(self):
        # (the 20,000 cap counts torrents, not bytes: 20,000 ids of 1 MB each would be 20 GB; audit 2026-10-30)
        self.answers = [(200, {}, {"rid": 1, "full_update": True, "torrents": {"h" * 5000 + str(j): {"name": "x"} for j in range(20)}})]
        self.app.qbit()
        self.assertLessEqual(max((len(h) for h in self.app.qb.state), default=0), 64)

    def test_cookie_jar_stays_small(self):
        def many_cookies(h):
            h.send_response(200)
            for i in range(80):
                h.send_header("Set-Cookie", f"c{len(self.app.qb.jar)}x{i}={'v' * 100}; Path=/")
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", "2")
            h.end_headers()
            h.wfile.write(b"{}")
        self.up.routes["/api/v2/transfer/info"] = many_cookies
        for _ in range(5):
            self.app.qb.get("/api/v2/transfer/info")
        self.assertLessEqual(len(self.app.qb.jar), 100, "a qBittorrent setting new cookies every time can't fill memory")


class UpstreamLists(unittest.TestCase):
    """One small answer with a huge list used to be turned into a huge answer for every page (audit 2026-10-30)."""
    def setUp(self):
        self.up = Upstream({"/api/v2": (200, {}, {"response": {"data": {"sessions": [{}] * 5000}}}),
                            "/api/v1/request": (200, {}, {"results": [{"id": i, "type": "movie"} for i in range(5000)]})})
        self.app = load_app(TAUTULLI_URL=self.up.url, TAUTULLI_KEY="k", SEERR_URL=self.up.url, SEERR_KEY="k")

    def tearDown(self):
        self.up.close()

    def test_tautulli_sessions_capped(self):
        self.assertLessEqual(len(self.app.plex()["sessions"]), 100)

    def test_seerr_requests_capped(self):
        self.assertLessEqual(len(self.app.requests_pending()["pending"]), 50, "it asks for 50: more is a lie")


if __name__ == "__main__":
    unittest.main()
