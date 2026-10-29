"""The page escapes everything it puts into HTML, numbers included, so even data that slipped past the
server's type checks can't add markup or run script. The API functions are replaced with hostile raw data
here on purpose: this tests the page on its own."""
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve

EVIL = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'


@unittest.skipUnless(CHROME, "needs Chrome or Chromium")
class PageEscaping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = cls.app = load_app()
        for i in (app.QBIT, app.SEERR, app.TAUTULLI):
            i["on"] = True
        t = {"name": EVIL, "state": "downloading", "progress": EVIL, "dlspeed": EVIL, "upspeed": EVIL, "eta": EVIL,
             "size": EVIL, "num_seeds": EVIL, "num_leechs": EVIL, "ratio": 1, "category": EVIL}
        app.API["/api/qbit"] = (0, lambda: {
            "connection": EVIL, "external_ip": EVIL, "vpn_check": True, "vpn_leak": False, "dl_speed": EVIL, "up_speed": EVIL,
            "dl_total": EVIL, "up_total": EVIL, "total": EVIL, "downloading": [t], "seeding": [{**t, "state": "uploading"}],
            "seeding_active": EVIL})
        app.API["/api/requests"] = (0, lambda: {"pending": [
            {"id": EVIL, "type": "movie", "is4k": False, "created": "2026-01-01T00:00:00Z", "user": EVIL,
             "seasons": [EVIL], "title": EVIL, "year": EVIL, "poster": None, "overview": EVIL}]})
        app.API["/api/plex"] = (0, lambda: {"stream_count": 1, "transcodes": EVIL, "bandwidth": EVIL, "sessions": [
            {"media_type": "episode", "title": EVIL, "grandparent_title": EVIL, "parent_media_index": EVIL,
             "media_index": EVIL, "progress_percent": EVIL, "bandwidth": EVIL, "duration": 10, "view_offset": EVIL,
             "state": EVIL, "full_title": EVIL, "friendly_name": EVIL, "product": EVIL, "player": EVIL, "art": None}]})
        cls.srv, cls.url = serve(app.Handler)
        cls.b = Browser()
        cls.b.send("Page.navigate", {"url": cls.url + "/"})
        cls.b.wait_for("document.querySelector('#dl .tor') && document.querySelector('#req .req') && document.querySelector('#plex .stream')")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        cls.srv.shutdown()

    def test_panels_rendered(self):
        self.assertTrue(self.b.js("!!document.querySelector('#dl .tor')"))
        self.assertTrue(self.b.js("!!document.querySelector('#req .req')"))

    def test_no_injected_markup(self):
        # an <img> created by the payload would be inside a torrent row, request card or stream card
        n = self.b.js("[...document.querySelectorAll('#dl img, #seed img, #req .b img, #plex .b img, .kpi img')].length")
        self.assertEqual(n, 0)
        self.assertFalse(self.b.js("window.__xss || 0"))


if __name__ == "__main__":
    unittest.main()
