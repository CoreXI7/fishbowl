"""A slow upstream must not hold the dash's threads for ever. urllib's timeout limits each read, so a service that
sends one byte every few seconds (in its headers or its body) used to keep a request thread, and the browser's
connection slot, until it stopped; enough of them and the dashboard answered nobody (audit 2026-10-30).
Every outbound request now has an overall deadline."""
import os
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
            # an answer that "will close" (HTTP/1.0, Connection: close, no length): http.client closes the connection
            # object once the headers are in, and the body is read after that (audit round 2)
            "/closing.png": trickle(b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n\r\n"),
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

    def test_a_closing_answer_ends_too(self):
        r, took = self.finishes(lambda: self.upstream.fetch_same_host(self.up.url + "/closing.png", self.up.url,
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

class SlowManyLookups(unittest.TestCase):
    """The per-request deadline isn't enough when one answer asks for many requests: a service page listing 50 icons,
    or Seerr naming 50 titles to look up (audit round 2)."""

    @classmethod
    def setUpClass(cls):
        slow = trickle(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n")
        links = "".join(f'<link rel="icon" href="/i{i}.png">' for i in range(50))
        cls.up = Upstream({"/": (200, {"Content-Type": "text/html"}, links), **{f"/i{i}.png": slow for i in range(50)},
                           "/favicon.ico": slow,
                           "/api/v1/request": (200, {}, {"results": [{"id": i, "type": "movie", "media": {"tmdbId": i}}
                                                                     for i in range(50)]}),
                           **{f"/api/v1/movie/{i}": slow for i in range(50)}})
        cls.app = load_app(SEERR_URL=cls.up.url, SEERR_KEY="k")
        import upstream
        cls.upstream, cls.factor = upstream, upstream.DEADLINE_FACTOR
        upstream.DEADLINE_FACTOR = 0.3
        with open(os.path.join(cls.app.NPM_DIR, "slow.conf"), "w") as f:
            host, port = cls.up.url.split("//")[1].split(":")
            f.write(f'server {{\n  server_name slow.example;\n  set $forward_scheme http;\n  set $server "{host}";\n  set $port {port};\n}}\n')

    @classmethod
    def tearDownClass(cls):
        cls.up.close()
        cls.upstream.DEADLINE_FACTOR = cls.factor

    def finishes(self, fn, within):
        t = threading.Thread(target=fn, daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(within)
        self.assertFalse(t.is_alive(), f"still going after {within} s")
        return time.monotonic() - t0

    def test_an_icon_lookup_has_an_overall_budget(self):
        self.finishes(lambda: self.app.favicon("slow.example"), 30)
        self.assertLessEqual(sum(1 for h in self.up.hits if h[1].startswith("/i")), 4, "a few candidates, not 50")

    def test_seerr_titles_have_an_overall_budget(self):
        self.finishes(self.app.requests_pending, 40)

    def test_a_url_urllib_refuses_shows_as_down(self):
        r = self.app.check_once({"name": "odd.example", "upstream": "http://[127.0.0.1]:8080", "auth": False})
        self.assertFalse(r["up"])


if __name__ == "__main__":
    unittest.main()
