"""Uploads are capped in size (2 MB each) and now also in number and total size, so a stolen session or a
script can't fill the host's disk with images."""
import os
import unittest

from helpers import load_app

PNG = b"\x89PNG\r\n\x1a\n"


class Uploads(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        os.makedirs(self.app.cfgstore.UPLOAD_DIR, exist_ok=True)

    def test_count_limit(self):
        for i in range(self.app.MAX_UPLOADS):
            with open(os.path.join(self.app.cfgstore.UPLOAD_DIR, f"{i}.png"), "wb") as f:
                f.write(PNG)
        with self.assertRaisesRegex(self.app.cfgstore.Invalid, "too many"):
            self.app.save_upload(PNG + b"new")

    def test_total_size_limit(self):
        with open(os.path.join(self.app.cfgstore.UPLOAD_DIR, "big.png"), "wb") as f:
            f.truncate(self.app.MAX_UPLOAD_TOTAL)
        with self.assertRaisesRegex(self.app.cfgstore.Invalid, "full"):
            self.app.save_upload(PNG + b"new")

    def test_same_image_again_is_fine_when_full(self):
        url = self.app.save_upload(PNG + b"same")["url"]
        with open(os.path.join(self.app.cfgstore.UPLOAD_DIR, "big.png"), "wb") as f:
            f.truncate(self.app.MAX_UPLOAD_TOTAL)
        self.assertEqual(self.app.save_upload(PNG + b"same")["url"], url)

    def test_normal_upload(self):
        self.assertTrue(self.app.save_upload(PNG + b"x")["url"].startswith("/uploads/"))


if __name__ == "__main__":
    unittest.main()
