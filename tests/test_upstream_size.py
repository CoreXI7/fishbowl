"""A hostile upstream can answer with gigabytes. The dash (256 MB memory cap) must stop reading at a limit
instead of loading the whole answer, for every integration."""
import unittest

from helpers import Upstream, load_app

MB = 1 << 20


def big_json_list(size):
    return b"[" + b"0," * (size // 2) + b"0]"


class UpstreamSize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        big = big_json_list(40 * MB)
        cls.up = Upstream({
            "/api/v2/auth/login": (200, {}, "Ok."),
            "/api/v2/transfer/info": (200, {}, {}),
            "/api/v2/sync/maindata": (200, {"Content-Type": "application/json"}, big),
            "/api/v1/request": (200, {"Content-Type": "application/json"}, b'{"results": ' + big + b"}"),
            "/api/v2": (200, {"Content-Type": "application/json"}, b'{"response": {"data": ' + big + b"}}"),
        })
        cls.app = load_app(QBIT_URL=cls.up.url, QBIT_USER="u", QBIT_PASS="p", SEERR_URL=cls.up.url, SEERR_KEY="k",
                           TAUTULLI_URL=cls.up.url, TAUTULLI_KEY="k")

    @classmethod
    def tearDownClass(cls):
        cls.up.close()

    def test_qbit_capped(self):
        with self.assertRaisesRegex(ValueError, "bigger than"):
            self.app.qbit()

    def test_seerr_capped(self):
        with self.assertRaisesRegex(ValueError, "bigger than"):
            self.app.requests_pending()

    def test_tautulli_capped(self):
        with self.assertRaisesRegex(ValueError, "bigger than"):
            self.app.plex()

    def test_normal_sizes_pass(self):
        self.up.routes["/api/v2/sync/maindata"] = (200, {}, {"rid": 1, "full_update": True, "torrents": {"h": {"name": "a", "state": "downloading"}}})
        try:
            self.assertEqual(self.app.qbit()["total"], 1)
        finally:
            self.up.routes["/api/v2/sync/maindata"] = (200, {}, big_json_list(40 * MB))


if __name__ == "__main__":
    unittest.main()
