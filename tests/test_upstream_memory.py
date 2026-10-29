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


if __name__ == "__main__":
    unittest.main()
