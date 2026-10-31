"""Wall mode keeps the header's buttons where they are: the wall button can be clicked on and off again without moving
the mouse, and settings stays one click away."""
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve


class WallHeader(unittest.TestCase):
    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_the_wall_button_stays_put(self):
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        b = Browser(1920, 1080)
        try:
            b.send("Page.navigate", {"url": url + "/"})
            self.assertTrue(b.wait_for("document.readyState === 'complete' && !document.getElementById('gear').hidden", 20))
            x = "Math.round(document.getElementById('wall-btn').getBoundingClientRect().x)"
            before = b.js(x)
            b.js("document.getElementById('wall-btn').click()")
            self.assertTrue(b.wait_for("document.body.classList.contains('wall')", 5))
            self.assertEqual(b.js(x), before, "the wall button doesn't move")
            self.assertNotEqual(b.js("getComputedStyle(document.getElementById('gear')).display"), "none", "settings stays")
            b.js("document.getElementById('wall-btn').click()")
            self.assertTrue(b.wait_for("!document.body.classList.contains('wall')", 5))
            self.assertEqual(b.js(x), before)
            b.send("Page.navigate", {"url": url + "/?wall"})          # a wall screen opened at ?wall: the whole width
            self.assertTrue(b.wait_for("document.readyState === 'complete' && document.body.classList.contains('wall')", 20))
            self.assertEqual(b.js("document.documentElement.clientWidth"), 1920)
        finally:
            b.close()
            srv.shutdown()


class PlexRow(unittest.TestCase):
    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_requests_can_always_be_moved_aside(self):
        # (the button only showed when streams didn't fit, so with a few streams there was no way to hide Requests)
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        b = Browser(1920, 1600)
        try:
            b.send("Page.navigate", {"url": url + "/"})
            self.assertTrue(b.wait_for("!!document.querySelector('#plex .stream') && !!document.querySelector('#req .req, #req > *')", 20))
            self.assertTrue(b.wait_for("!document.getElementById('plex-more').hidden", 5), "the button shows")
            b.js("document.getElementById('plex-more').click()")
            self.assertTrue(b.wait_for("document.querySelector('.duo').classList.contains('plex-wide')", 5))
            self.assertIn("show requests", b.js("document.getElementById('plex-more').textContent"))
            b.js("document.getElementById('plex-more').click()")
            self.assertTrue(b.wait_for("!document.querySelector('.duo').classList.contains('plex-wide')", 5))
        finally:
            b.js("localStorage.removeItem('plexWide')")
            b.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
