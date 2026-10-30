"""NPM's proxy_host/*.conf files are data from another program (NPM, or anyone who can write its folder). One odd file
must not break the Services panel or the settings page, nor make the dash read local files (audit 2026-10-30)."""
import os
import unittest

from helpers import load_app

CONF = 'server {{\n  set $forward_scheme {scheme};\n  set $server "{server}";\n  set $port 80;\n  server_name {name};\n}}\n'


class NpmConf(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.write("good.conf", CONF.format(scheme="http", server="10.0.0.5", name="good.example"))

    def write(self, name, text):
        with open(os.path.join(self.app.NPM_DIR, name), "wb") as f:
            f.write(text.encode("latin-1") if isinstance(text, str) else text)
        self.app._npm["sig"] = None

    def names(self):
        return sorted(h["name"] for h in self.app.read_npm_hosts())

    def test_a_latin1_byte_doesnt_break_the_list(self):
        self.write("odd.conf", CONF.format(scheme="http", server="10.0.0.6", name="caf\xe9.example"))
        self.assertIn("good.example", self.names())

    def test_a_fifo_or_a_link_isnt_read(self):
        os.mkfifo(os.path.join(self.app.NPM_DIR, "fifo.conf"))
        os.symlink("/dev/zero", os.path.join(self.app.NPM_DIR, "zero.conf"))
        self.app._npm["sig"] = None
        self.assertEqual(self.names(), ["good.example"])    # (a FIFO used to hang it; /dev/zero, read until OOM)

    def test_only_http_upstreams(self):
        self.write("file.conf", CONF.format(scheme="file", server="/app/static/favicon.svg#", name="file.example"))
        self.assertEqual(self.names(), ["good.example"], "a file:// upstream would make /api/icon read local files")

    def test_a_file_of_blank_lines_parses_quickly(self):
        # ("^\\s*" with re.M ran across every following blank line: 256 KB of them took ~100 s, holding the GIL;
        # audit round 2)
        import threading
        import time
        self.write("blank.conf", 'server {\n  server_name blank.example;\n  set $server "10.0.0.7";\n  set $port 80;\n'
                   + "\n" * 250_000 + "}\n")
        t = threading.Thread(target=self.names, daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 2)


if __name__ == "__main__":
    unittest.main()
