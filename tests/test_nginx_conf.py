"""Tests for app/nginxconf.py: plain Nginx site configs as services (SPEC-setup-friction.md section 3).

Every test builds its own temporary /etc/nginx-like tree; no network or root is needed.
"""
import os, sys, tempfile, unittest, shutil, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))
import nginxconf
import re

BLOCK = """server {{
  server_name {name};
  location / {{
    proxy_pass {target};
  }}
}}
"""


class NginxConfTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def mkdirs(self, *names):
        for name in names:
            os.makedirs(os.path.join(self.root, name), exist_ok=True)

    def write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def link(self, name, target):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)

    @staticmethod
    def service(name, target):
        return BLOCK.format(name=name, target=target)

    def by_name(self, **kw):
        return {h["name"]: h for h in nginxconf.read_hosts(self.root, **kw)}

    def test_debian_layout_relative_and_absolute_links(self):
        """sites-enabled links (relative ../sites-available and absolute /etc/nginx/...) are followed."""
        self.mkdirs("sites-available", "sites-enabled")
        self.write("sites-available/a", self.service("a.example", "http://10.0.0.5:8080"))
        self.write("sites-available/b", self.service("b.example", "http://10.0.0.6:8080"))
        self.link("sites-enabled/a", "../sites-available/a")
        self.link("sites-enabled/b", "/etc/nginx/sites-available/b")
        hosts = self.by_name()
        self.assertEqual(sorted(hosts), ["a.example", "b.example"])
        self.assertEqual(hosts["a.example"]["upstream"], "http://10.0.0.5:8080")
        self.assertEqual(hosts["b.example"]["upstream"], "http://10.0.0.6:8080")

    def test_redirect_only_block_is_not_a_service(self):
        """An 80->443 return block is skipped; the 443 block of the same name is the service."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/site.conf", """server {
  listen 80;
  server_name example.com;
  return 301 https://example.com$request_uri;
}
server {
  listen 443 ssl;
  server_name example.com;
  location / { proxy_pass http://10.0.0.5:8080; }
}
""")
        hosts = self.by_name()
        self.assertEqual(sorted(hosts), ["example.com"])
        self.assertEqual(hosts["example.com"]["upstream"], "http://10.0.0.5:8080")

    def test_upstream_block_resolves_proxy_pass(self):
        """An upstream block in the same file resolves a named proxy_pass target."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/up.conf", "upstream jf {\n  server 10.0.0.9:8096;\n}\n"
                   + self.service("jf.example", "http://jf"))
        hosts = self.by_name()
        self.assertEqual(hosts["jf.example"]["upstream"], "http://10.0.0.9:8096")

    def test_two_blocks_per_file_and_duplicate_names(self):
        """Two server blocks in one file are two services; a repeated name is listed once."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/one.conf",
                   self.service("alpha.example", "http://10.0.0.1:80")
                   + self.service("beta.example", "http://10.0.0.2:80"))
        self.write("sites-enabled/two.conf", self.service("alpha.example", "http://10.0.0.3:80"))
        hosts = self.by_name()
        self.assertEqual(sorted(hosts), ["alpha.example", "beta.example"])
        self.assertEqual(hosts["alpha.example"]["upstream"], "http://10.0.0.1:80")

    def test_unusable_server_names_are_skipped(self):
        """_, localhost, wildcard and regex names are skipped; the first usable name is used."""
        self.mkdirs("sites-enabled")
        text = "".join([
            self.service("_", "http://10.0.0.1:80"),
            self.service("localhost", "http://10.0.0.1:80"),
            self.service("*.example.com", "http://10.0.0.1:80"),
            self.service(r"~^www\.", "http://10.0.0.1:80"),
            self.service("_ real.example.com", "http://10.0.0.1:80"),
        ])
        self.write("sites-enabled/names.conf", text)
        self.assertEqual(sorted(self.by_name()), ["real.example.com"])

    def test_odd_proxy_targets_are_not_services(self):
        """Variables, file://, unix: and gopher:// targets produce no service."""
        self.mkdirs("sites-enabled")
        text = "".join([
            self.service("var.example", "http://$upstream"),
            self.service("file.example", "file:///etc/passwd"),
            self.service("unix.example", "unix:/run/x.sock"),
            self.service("gopher.example", "gopher://example.com"),
        ])
        self.write("sites-enabled/odd.conf", text)
        self.assertEqual(self.by_name(), {})

    def test_missing_ports_default_to_443_and_80(self):
        """https without a port gets 443, http without a port gets 80."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/ports.conf",
                   self.service("tls.example", "https://10.0.0.8")
                   + self.service("plain.example", "http://10.0.0.8"))
        hosts = self.by_name()
        self.assertEqual(hosts["tls.example"]["upstream"], "https://10.0.0.8:443")
        self.assertEqual(hosts["plain.example"]["upstream"], "http://10.0.0.8:80")

    def test_root_location_wins(self):
        """When both /api and / have proxy_pass, location / is used."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/loc.conf", """server {
  server_name loc.example;
  location /api { proxy_pass http://a:1; }
  location / { proxy_pass http://b:2; }
}
""")
        self.assertEqual(self.by_name()["loc.example"]["upstream"], "http://b:2")

    def test_auth_and_login_detection(self):
        """auth_re and has_login mark only the block that has auth_request."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/auth.conf",
                   """server {
  server_name guarded.example;
  auth_request /authelia;
  location / { proxy_pass http://10.0.0.4:80; }
}
""" + self.service("open.example", "http://10.0.0.4:80"))
        auth_re = re.compile(r"auth_request\s+/authelia")
        hosts = self.by_name(auth_re=auth_re, has_login=lambda body: "auth_request" in body)
        self.assertTrue(hosts["guarded.example"]["auth"])
        self.assertTrue(hosts["guarded.example"]["login"])
        self.assertFalse(hosts["open.example"]["auth"])
        self.assertFalse(hosts["open.example"]["login"])

    def test_hostile_links_are_ignored(self):
        """Links outside the root, link chains and FIFOs are ignored and do not hang."""
        self.mkdirs("sites-available", "sites-enabled")
        self.write("sites-enabled/good.conf", self.service("good.example", "http://10.0.0.5:8080"))
        self.write("sites-available/a", self.service("chained.example", "http://10.0.0.6:80"))
        self.link("sites-available/chain", "a")
        self.link("sites-enabled/shadow", "/etc/shadow")
        self.link("sites-enabled/dotdot", "/etc/nginx/../shadow")
        self.link("sites-enabled/passwd", "../../../../etc/passwd")
        self.link("sites-enabled/c", "../sites-available/chain")
        if hasattr(os, "mkfifo"):
            os.mkfifo(os.path.join(self.root, "sites-enabled", "fifo"))
        started = time.monotonic()
        hosts = self.by_name()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(sorted(hosts), ["good.example"])

    def test_size_and_depth_limits_are_fast(self):
        """Oversized files are skipped; huge brace runs and blank lines stay under 2 seconds."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/big.conf",
                   self.service("big.example", "http://10.0.0.5:80") + "#" + "x" * (300 * 1024))
        self.write("sites-enabled/braces.conf",
                   "{\n" * 100_000 + self.service("braces.example", "http://10.0.0.5:80"))
        self.write("sites-enabled/blanks.conf",
                   "\n" * 250_000 + self.service("blanks.example", "http://10.0.0.5:80"))
        started = time.monotonic()
        hosts = self.by_name()
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotIn("big.example", hosts)
        self.assertIn("blanks.example", hosts)

    def test_comments_do_not_break_parsing(self):
        """A # comment is dropped; a # inside quotes is not a comment."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/comments.conf", """server {
  server_name comment.example;
  # a { fake block and server_name bad.example;
  location / { proxy_pass http://10.0.0.5:8080; }
}
server {
  server_name quoted.example;
  add_header X "a # b";
  location / { proxy_pass http://10.0.0.5:8080; }
}
""")
        hosts = self.by_name()
        self.assertEqual(sorted(hosts), ["comment.example", "quoted.example"])

    def test_empty_and_missing_roots(self):
        """read_hosts('') and read_hosts('/nonexistent') return []."""
        self.assertEqual(nginxconf.read_hosts(""), [])
        self.assertEqual(nginxconf.read_hosts("/nonexistent"), [])

    def test_root_conf_files_when_no_subfolders(self):
        """Without sites-enabled and conf.d, the root's own *.conf files are read."""
        self.write("only.conf", self.service("root.example", "http://10.0.0.7:8080"))
        self.write("skip.txt", self.service("text.example", "http://10.0.0.7:8080"))
        self.write(".hidden.conf", self.service("hidden.example", "http://10.0.0.7:8080"))
        self.assertEqual(sorted(self.by_name()), ["root.example"])

    def test_invalid_target_ports_are_skipped(self):
        """Ports 0 and 99999 are out of range and produce no service."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/badport.conf",
                   self.service("zero.example", "http://10.0.0.9:0")
                   + self.service("huge.example", "http://10.0.0.9:99999"))
        self.assertEqual(self.by_name(), {})

    def test_upstream_with_invalid_port_gives_no_service(self):
        """An upstream server with port 0 or 99999 is not resolved."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/upbad.conf",
                   "upstream bad_ {\n  server 10.0.0.9:99999;\n}\n"
                   "upstream worse- {\n  server 10.0.0.9:0;\n}\n"
                   + self.service("over.example", "http://bad_")
                   + self.service("zero.example", "https://worse-"))
        self.assertEqual(self.by_name(), {})

    def test_upstream_https_defaults_to_443(self):
        """upstream b { server h; } with proxy_pass https://b gives https://h:443."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/up.conf", "upstream b {\n  server h;\n}\n"
                   + self.service("up.example", "https://b"))
        self.assertEqual(self.by_name()["up.example"]["upstream"], "https://h:443")

    def test_quoted_brace_before_server_name(self):
        """A { inside quotes (add_header X "{";) does not break the server block."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/quote.conf", """server {
  add_header X "{";
  server_name quote.example;
  location / { proxy_pass http://10.0.0.5:8080; }
}
""")
        self.assertEqual(self.by_name()["quote.example"]["upstream"], "http://10.0.0.5:8080")

    def test_host_names_must_start_and_end_alphanumeric(self):
        """Targets like -foo and a- are not accepted."""
        self.mkdirs("sites-enabled")
        self.write("sites-enabled/hosts.conf",
                   self.service("lead.example", "http://-foo:8080")
                   + self.service("trail.example", "http://a-:8080"))
        self.assertEqual(self.by_name(), {})


if __name__ == "__main__":
    unittest.main()


class FileListLayer(unittest.TestCase):
    """files() itself refuses bad links (the read-time check is a second layer: each must hold on its own)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        for d in ("sites-available", "sites-enabled"):
            os.makedirs(os.path.join(self.root, d))
        with open(os.path.join(self.root, "sites-available", "a"), "w") as f:
            f.write("server { server_name a.example.com; location / { proxy_pass http://10.0.0.1:80; } }\n")
        en = os.path.join(self.root, "sites-enabled")
        os.symlink("../sites-available/a", os.path.join(en, "good"))
        os.symlink("/etc/shadow", os.path.join(en, "shadow"))
        os.symlink("/etc/nginx/../shadow", os.path.join(en, "esc"))
        os.symlink("../../../../etc/passwd", os.path.join(en, "up"))
        os.symlink("a", os.path.join(self.root, "sites-available", "chain"))
        os.symlink("../sites-available/chain", os.path.join(en, "chain"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_only_the_good_link_is_listed(self):
        names = [n for n, _, _ in nginxconf.files(self.root)]
        self.assertEqual(names, [os.path.join("sites-enabled", "good")], names)

    def test_read_refuses_a_file_reached_through_a_swapped_folder(self):
        """A folder replaced by a link after files() checked it: the opened file's real location is checked again."""
        outside = tempfile.mkdtemp()
        try:
            with open(os.path.join(outside, "secret"), "w") as f:
                f.write("server { server_name leak.example.com; location / { proxy_pass http://10.0.0.2:80; } }\n")
            os.symlink(outside, os.path.join(self.root, "swapped"))
            self.assertIsNone(nginxconf.read_text(os.path.join(self.root, "swapped", "secret"), self.root))
            self.assertIsNotNone(nginxconf.read_text(os.path.join(self.root, "sites-available", "a"), self.root))
        finally:
            shutil.rmtree(outside)


class ReviewRound2(unittest.TestCase):
    """Findings of the final review (each failed before its fix)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "conf.d"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, name, text):
        with open(os.path.join(self.root, "conf.d", name), "w") as f:
            f.write(text)

    def test_no_semicolon_flood_stays_fast(self):
        """19 nested server blocks around 240 KB of 'proxy_pass ' with no ';' (quadratic with the old patterns)."""
        line = ("proxy_pass " * 360).strip() + "\n"
        body = line * (240000 // len(line))
        self.write("flood.conf", "server { server_name s.example.com; " * 19 + "location / {\n" + body + "}" + "}" * 19)
        t = time.time()
        nginxconf.read_hosts(self.root)
        self.assertLess(time.time() - t, 2.0)

    def test_escaped_quote_with_hash_keeps_the_block(self):
        self.write("q.conf", 'server { add_header X-Note "a \\" # b"; server_name q.example.com; '
                             'location / { proxy_pass http://10.0.0.3:80; } }\n')
        self.assertEqual([h["name"] for h in nginxconf.read_hosts(self.root)], ["q.example.com"])

    def test_a_linked_conf_d_outside_is_not_walked(self):
        outside = tempfile.mkdtemp()
        try:
            with open(os.path.join(outside, "x.conf"), "w") as f:
                f.write("server { server_name out.example.com; location / { proxy_pass http://10.0.0.4:80; } }\n")
            shutil.rmtree(os.path.join(self.root, "conf.d"))
            os.symlink(outside, os.path.join(self.root, "conf.d"))
            self.assertEqual(nginxconf.files(self.root), [])
            self.assertEqual(nginxconf.read_hosts(self.root), [])
        finally:
            shutil.rmtree(outside)


class AuthSnippetSpeed(unittest.TestCase):
    def test_include_flood_is_fast(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        from helpers import load_app
        app = load_app()
        txt = "include " + "authelia" * 510 + "\n" + (" " * 4000 + "\n") * 60
        t = time.time()
        app.AUTH_SNIPPET.search(txt)
        self.assertLess(time.time() - t, 1.0)
