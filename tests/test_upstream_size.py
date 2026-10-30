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

class ChunkSize(unittest.TestCase):
    """http.client reads a chunk size of "-1" as "everything up to the end" (CVE-2026-103995, not fixed in 3.12.16):
    every byte cap was bypassed, and one hostile upstream got the dash OOM-killed (audit round 3)."""

    def test_a_negative_chunk_size_is_refused(self):
        import http.client
        import io
        load_app()          # (puts app/ on the import path)
        import upstream

        class Sock:
            def __init__(self, data):
                self.data = data

            def makefile(self, *a, **k):
                return io.BufferedReader(io.BytesIO(self.data))
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n" + b"x" * 5_000_000
        r = upstream.StrictResponse(Sock(raw))
        r.begin()
        with self.assertRaises(http.client.HTTPException) as e:
            r.read(1000)
        self.assertLess(len(getattr(e.exception, "partial", b"")), 2000, "it stopped instead of reading to the end")
        for cls in (upstream.DeadlineHTTPConnection, upstream.DeadlineHTTPSConnection):
            self.assertIs(cls.response_class, upstream.StrictResponse)

class HeaderSize(unittest.TestCase):
    """http.client takes ~6 MB of headers per answer, and the deadline watcher kept each finished connection (with its
    last answer and those headers) until its deadline: one Seerr poll's 50 title lookups OOM-killed the dash (audit
    round 3). Upstream answers' headers are capped at 64 KB, and the watcher holds sockets only."""

    def test_huge_headers_are_refused_and_nothing_big_is_kept(self):
        def big(h):
            h.send_response(200)
            for i in range(30):
                h.send_header(f"X-Pad-{i}", "a" * 60_000)
            h.send_header("Content-Type", "image/png")
            h.send_header("Content-Length", "4")
            h.end_headers()
            h.wfile.write(b"\x89PNG")
        up = Upstream({"/favicon.ico": big})
        self.addCleanup(up.close)
        load_app()
        import upstream
        self.assertIsNone(upstream.fetch_same_host(up.url + "/favicon.ico", up.url, "big.example", 512_000))
        self.assertTrue(all(not hasattr(k, "headers") and not hasattr(k, "_HTTPConnection__response")
                            for k in list(upstream._watched)), "only sockets are kept until the deadline")


if __name__ == "__main__":
    unittest.main()
