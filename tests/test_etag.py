"""Pages, CSS and JS carry an ETag: a reload revalidates and gets 304 Not Modified instead of every file again."""
import unittest
import urllib.error
import urllib.request

from helpers import load_app, serve


class ETag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app(DEMO="1")
        cls.srv, cls.url = serve(cls.app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_revalidation(self):
        for path in ("/", "/settings", "/style.css", "/themes.css", "/fonts.css", "/js/core.js", "/js/theme-boot.js"):
            r = urllib.request.urlopen(self.url + path)
            tag = r.headers["ETag"]
            self.assertTrue(tag, path)
            self.assertIn("no-cache", r.headers["Cache-Control"], path)
            with self.assertRaises(urllib.error.HTTPError, msg=path) as e:
                urllib.request.urlopen(urllib.request.Request(self.url + path, headers={"If-None-Match": tag}))
            self.assertEqual(e.exception.code, 304, path)
            self.assertEqual(e.exception.read(), b"", path)

    def test_other_tag_gets_the_file(self):
        r = urllib.request.urlopen(urllib.request.Request(self.url + "/style.css", headers={"If-None-Match": 'W/"nope"'}))
        self.assertEqual(r.status, 200)
        self.assertTrue(r.read())


if __name__ == "__main__":
    unittest.main()
