"""A slow upstream must not hold the dash's threads for ever. urllib's timeout limits each read, so a service that
sends one byte every few seconds (in its headers or its body) used to keep a request thread, and the browser's
connection slot, until it stopped; enough of them and the dashboard answered nobody (audit 2026-10-30).
Every outbound request now has an overall deadline."""
import threading
import time
import unittest

from helpers import Upstream, load_app


def trickle(head):
    """A route that sends head, then one more byte every 0.2 s for up to 60 s."""
    def route(h):
        try:
            h.wfile.write(head)
            h.wfile.flush()
            end = time.monotonic() + 60
            while time.monotonic() < end:
                time.sleep(0.2)
                h.wfile.write(b"a")
                h.wfile.flush()
        except OSError:
            pass
        h.close_connection = True
    return route


class SlowUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = Upstream({
            "/": trickle(b"HTTP/1.1 200 OK\r\nX-Pad: "),
            "/favicon.ico": trickle(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n"),
            "/api/v2": trickle(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"),
        })
        cls.app = load_app(TAUTULLI_URL=cls.up.url, TAUTULLI_KEY="k")
        import upstream
        cls.upstream = upstream
        cls.factor = getattr(upstream, "DEADLINE_FACTOR", None)
        upstream.DEADLINE_FACTOR = 0.3      # (deadline = 0.3 x the timeout, so the test doesn't wait long)

    @classmethod
    def tearDownClass(cls):
        cls.up.close()
        cls.upstream.DEADLINE_FACTOR = cls.factor

    def finishes(self, fn, within):
        out = []
        t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(within)
        self.assertFalse(t.is_alive(), f"still waiting on the upstream after {within} s")
        return out[0] if out else None, time.monotonic() - t0

    def test_a_health_check_ends_on_a_trickling_header(self):
        host = {"name": "slow.example.com", "upstream": self.up.url, "auth": False}
        r, took = self.finishes(lambda: self.app.check_once(host, timeout=5), 10)
        self.assertLess(took, 5)

    def test_an_icon_ends_on_a_trickling_body(self):
        r, took = self.finishes(lambda: self.upstream.fetch_same_host(self.up.url + "/favicon.ico", self.up.url,
                                                                           "slow.example.com", 512_000), 10)
        self.assertIsNone(r)

    def test_an_integration_ends_on_a_trickling_body(self):
        def plex():
            try:
                return self.app.plex()
            except Exception as e:
                return e
        r, took = self.finishes(plex, 15)
        self.assertIsInstance(r, Exception)


if __name__ == "__main__":
    unittest.main()
