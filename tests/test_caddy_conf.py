import os, sys, tempfile, unittest, shutil, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import caddyconf

OUTSIDE = "outside.example.com {\n\treverse_proxy outside:1\n}\n"


class CaddyTest(unittest.TestCase):
    def setUp(self):
        self.dirs = []
        self.root = self.mkdtemp()

    def tearDown(self):
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def mkdtemp(self):
        d = tempfile.mkdtemp()
        self.dirs.append(d)
        return d

    def write(self, name, text, root=None):
        path = os.path.join(root or self.root, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def hosts(self, text):
        return caddyconf.read_hosts(self.write("Caddyfile", text))


class TestSites(CaddyTest):
    def test_one_site(self):
        """A single site becomes one Caddy service."""
        hosts = self.hosts("app.example.com {\n\treverse_proxy app:8080\n}\n")
        self.assertEqual(len(hosts), 1)
        h = hosts[0]
        self.assertEqual(h["name"], "app.example.com")
        self.assertEqual(h["upstream"], "http://app:8080")
        self.assertTrue(h["force_ssl"])
        self.assertFalse(h["auth"])
        self.assertFalse(h["login"])
        self.assertTrue(h["caddy"])

    def test_several_sites_and_duplicate_name(self):
        """All sites are found; a repeated name is listed once, first wins."""
        hosts = self.hosts(
            "a.example.com {\n\treverse_proxy a:1\n}\n"
            "b.example.com {\n\treverse_proxy b:2\n}\n"
            "a.example.com {\n\treverse_proxy again:3\n}\n")
        self.assertEqual([h["name"] for h in hosts], ["a.example.com", "b.example.com"])
        self.assertEqual(hosts[0]["upstream"], "http://a:1")

    def test_global_options_block_is_not_a_site(self):
        """A top-level block with no head is global options, not a site."""
        hosts = self.hosts("{\n\temail a@b.c\n}\napp.example.com {\n\treverse_proxy app:8080\n}\n")
        self.assertEqual([h["name"] for h in hosts], ["app.example.com"])


class TestImports(CaddyTest):
    def test_import_glob_reads_both_files(self):
        """import sites/* reaches both site files."""
        self.write("sites/a.caddy", "a.example.com {\n\treverse_proxy a:1\n}\n")
        self.write("sites/b.caddy", "b.example.com {\n\treverse_proxy b:2\n}\n")
        hosts = self.hosts("import sites/*\n")
        self.assertEqual([h["name"] for h in hosts], ["a.example.com", "b.example.com"])

    def test_file_that_is_not_imported_is_not_read(self):
        """A site in an unimported file never appears."""
        self.write("sites/x.caddy", "x.example.com {\n\treverse_proxy x:1\n}\n")
        hosts = self.hosts("app.example.com {\n\treverse_proxy app:8080\n}\n")
        self.assertEqual([h["name"] for h in hosts], ["app.example.com"])

    def test_absolute_caddy_path_maps_into_root(self):
        """import /etc/caddy/... is mapped onto the mounted root and read."""
        self.write("sites/a.caddy", "a.example.com {\n\treverse_proxy a:1\n}\n")
        hosts = self.hosts("import /etc/caddy/sites/a.caddy\n")
        self.assertEqual([h["name"] for h in hosts], ["a.example.com"])


class TestAuth(CaddyTest):
    def test_authelia_snippet(self):
        """An Authelia snippet sets auth and login."""
        hosts = self.hosts(
            "(authelia) {\n\tforward_auth authelia:9091 {\n\t\turi /api/authz/forward-auth\n\t}\n}\n"
            "app.example.com {\n\timport authelia\n\treverse_proxy app:8080\n}\n")
        self.assertEqual(len(hosts), 1)
        self.assertTrue(hosts[0]["auth"])
        self.assertTrue(hosts[0]["login"])

    def test_snippet_from_imported_file(self):
        """A snippet defined in an imported file can be used."""
        self.write("snippets.caddy",
                   "(authelia) {\n\tforward_auth authelia:9091 {\n\t\turi /api/authz/forward-auth\n\t}\n}\n")
        hosts = self.hosts(
            "import snippets.caddy\n"
            "app.example.com {\n\timport authelia\n\treverse_proxy app:8080\n}\n")
        self.assertEqual(len(hosts), 1)
        self.assertTrue(hosts[0]["auth"])
        self.assertTrue(hosts[0]["login"])

    def test_basic_auth_at_site_level(self):
        """Site-level basic_auth means login without forward_auth."""
        hosts = self.hosts(
            "app.example.com {\n\treverse_proxy app:8080\n\tbasic_auth {\n\t\tadmin $2a$hash\n\t}\n}\n")
        self.assertTrue(hosts[0]["login"])
        self.assertFalse(hosts[0]["auth"])

    def test_basic_auth_with_matcher(self):
        """A matcher on basic_auth does not gate the whole site."""
        hosts = self.hosts(
            "app.example.com {\n\treverse_proxy app:8080\n\tbasic_auth /admin/* {\n\t\tadmin $2a$hash\n\t}\n}\n")
        self.assertFalse(hosts[0]["login"])
        self.assertFalse(hosts[0]["auth"])


class TestReverseProxy(CaddyTest):
    def test_matched_proxy_skipped_for_unmatched(self):
        """A site-level reverse_proxy without a matcher wins over a matched one."""
        hosts = self.hosts(
            "app.example.com {\n\treverse_proxy /api/* other:1\n\treverse_proxy main:2\n}\n")
        self.assertEqual(hosts[0]["upstream"], "http://main:2")

    def test_handle_block_and_matched_handle_fallback(self):
        """A plain handle is used; a matched handle still falls back to its proxy."""
        hosts = self.hosts(
            "a.example.com {\n\thandle {\n\t\treverse_proxy z:3\n\t}\n}\n"
            "b.example.com {\n\thandle /api/* {\n\t\treverse_proxy q:4\n\t}\n}\n")
        by = {h["name"]: h["upstream"] for h in hosts}
        self.assertEqual(by["a.example.com"], "http://z:3")
        self.assertEqual(by["b.example.com"], "http://q:4")

    def test_reverse_proxy_to_line(self):
        """A `to` line inside reverse_proxy supplies the upstream."""
        hosts = self.hosts("app.example.com {\n\treverse_proxy {\n\t\tto a:1 b:2\n\t}\n}\n")
        self.assertEqual(hosts[0]["upstream"], "http://a:1")

    def test_upstream_forms(self):
        """Supported upstream forms resolve; placeholders, unix and bad ports do not."""
        cases = {
            "https://s:8989": "https://s:8989",
            "https://s": "https://s:443",
            ":8080": "http://localhost:8080",
            "h2c://g:50051": "http://g:50051",
            "unix//run/x.sock": None,
            "{upstream}": None,
            "localhost:99999": None,
            "localhost:0": None,
        }
        for up, want in cases.items():
            with self.subTest(up=up):
                hosts = self.hosts("app.example.com {\n\treverse_proxy %s\n}\n" % up)
                self.assertEqual(hosts[0]["upstream"] if hosts else None, want)


class TestAddresses(CaddyTest):
    def test_address_forms(self):
        """Address parsing picks a valid name and the right force_ssl."""
        cases = {
            "http://old.example.com": ("old.example.com", False),
            "old.example.com:8443": ("old.example.com", True),
            "a.example.com, b.example.com": ("a.example.com", True),
            "*.example.com": None,
            ":8080": None,
            "localhost": None,
            "{$DOMAIN}": None,
        }
        for addr, want in cases.items():
            with self.subTest(addr=addr):
                hosts = self.hosts("%s {\n\treverse_proxy app:1\n}\n" % addr)
                if want is None:
                    self.assertEqual(hosts, [])
                else:
                    self.assertEqual((hosts[0]["name"], hosts[0]["force_ssl"]), want)

    def test_sites_without_reverse_proxy(self):
        """redir, respond and file_server sites are not services."""
        for directive in ("redir https://x.example.com", 'respond "hi"', "file_server"):
            with self.subTest(directive=directive):
                self.assertEqual(self.hosts("app.example.com {\n\t%s\n}\n" % directive), [])


class TestLexing(CaddyTest):
    def test_hash_comment_rules(self):
        """# starts a comment at a token start, but not inside a word or quotes."""
        hosts = self.hosts("app.example.com {\n\treverse_proxy app:1 # note {\n}\n")
        self.assertEqual(hosts[0]["upstream"], "http://app:1")
        for proxy in ("a#b:1", '"a#b:1"'):
            with self.subTest(proxy=proxy):
                hosts = self.hosts(
                    "app.example.com {\n\treverse_proxy %s\n\treverse_proxy good:2\n}\n" % proxy)
                self.assertEqual(hosts[0]["upstream"], "http://good:2")


class TestHostileImports(CaddyTest):
    def test_relative_import_out_of_root(self):
        """import ../outside.caddy cannot escape the root."""
        container = self.mkdtemp()
        root = os.path.join(container, "root")
        os.mkdir(root)
        self.write("outside.caddy", OUTSIDE, root=container)
        caddyfile = self.write("Caddyfile", "import ../outside.caddy\n", root=root)
        self.assertEqual(caddyconf.read_hosts(caddyfile), [])

    def test_import_etc_passwd(self):
        """import /etc/passwd is ignored."""
        self.assertEqual(self.hosts("import /etc/passwd\n"), [])

    def test_import_absolute_traversal(self):
        """import /etc/caddy/../../outside.caddy is ignored."""
        self.assertEqual(self.hosts("import /etc/caddy/../../outside.caddy\n"), [])

    def test_symlink_outside_root(self):
        """A link inside root pointing outside is neither listed nor read."""
        outside_dir = self.mkdtemp()
        outside = self.write("outside.caddy", OUTSIDE, root=outside_dir)
        os.symlink(outside, os.path.join(self.root, "linked.caddy"))
        self.assertNotIn("linked.caddy", [n for n, _, _ in caddyconf.files(self.root)])
        self.assertEqual(self.hosts("import linked.caddy\n"), [])

    def test_symlink_chain_not_followed(self):
        """A link to another link is not followed."""
        self.write("sites/real.caddy", OUTSIDE)
        os.symlink("real.caddy", os.path.join(self.root, "sites", "l1"))
        os.symlink("l1", os.path.join(self.root, "sites", "l2"))
        names = [n for n, _, _ in caddyconf.files(self.root)]
        self.assertIn(os.path.join("sites", "l1"), names)
        self.assertNotIn(os.path.join("sites", "l2"), names)
        self.assertEqual(self.hosts("import sites/l2\n"), [])

    def test_fifo_import_does_not_hang(self):
        """A FIFO is not a regular file and is never opened."""
        try:
            os.mkfifo(os.path.join(self.root, "fifo.caddy"))
        except (AttributeError, OSError):
            self.skipTest("os.mkfifo unavailable")
        start = time.time()
        self.assertEqual(self.hosts("import fifo.caddy\n"), [])
        self.assertLess(time.time() - start, 2.0)


class TestLoops(CaddyTest):
    def test_file_imports_itself(self):
        """A self-importing Caddyfile finishes and lists the site once."""
        hosts = self.hosts("import Caddyfile\napp.example.com {\n\treverse_proxy app:8080\n}\n")
        self.assertEqual([h["name"] for h in hosts], ["app.example.com"])

    def test_two_files_import_each_other(self):
        """a imports b imports a finishes, each site listed once."""
        self.write("a.caddy", "import b.caddy\na.example.com {\n\treverse_proxy a:1\n}\n")
        self.write("b.caddy", "import a.caddy\nb.example.com {\n\treverse_proxy b:2\n}\n")
        hosts = self.hosts("import a.caddy\n")
        self.assertEqual(sorted(h["name"] for h in hosts), ["a.example.com", "b.example.com"])

    def test_snippet_loops_finish(self):
        """A self-importing snippet and two mutually importing snippets finish."""
        hosts = self.hosts(
            "(self) {\n\timport self\n}\n"
            "(a) {\n\timport b\n}\n"
            "(b) {\n\timport a\n}\n"
            "app.example.com {\n\timport self\n\timport a\n\treverse_proxy app:8080\n}\n")
        self.assertEqual([h["name"] for h in hosts], ["app.example.com"])
        self.assertEqual(hosts[0]["upstream"], "http://app:8080")


class TestLimits(CaddyTest):
    def test_deep_nesting_is_fast(self):
        """100000 nested blocks finish quickly without crashing."""
        start = time.time()
        hosts = self.hosts("x {\n" * 100000 + "app.example.com {\n\treverse_proxy app:8080\n}\n")
        self.assertIsInstance(hosts, list)
        self.assertLess(time.time() - start, 2.0)

    def test_many_blank_lines_are_fast(self):
        """A 250000-line file of blank lines is handled quickly."""
        start = time.time()
        self.assertEqual(self.hosts("\n" * 250000), [])
        self.assertLess(time.time() - start, 2.0)

    def test_file_over_256kb_is_skipped(self):
        """A file over 256 KB is skipped entirely."""
        text = "app.example.com {\n\treverse_proxy app:8080\n}\n#" + "x" * (256 * 1024) + "\n"
        start = time.time()
        self.assertEqual(self.hosts(text), [])
        self.assertLess(time.time() - start, 2.0)

    def test_glob_over_many_files_is_capped(self):
        """300 site files under import sites/* read at most 200 without crashing."""
        for i in range(300):
            self.write("sites/s%03d.caddy" % i,
                       "s%03d.example.com {\n\treverse_proxy s%d:1\n}\n" % (i, i))
        start = time.time()
        hosts = self.hosts("import sites/*\n")
        self.assertLessEqual(len(hosts), 200)
        self.assertGreaterEqual(len(hosts), 1)
        self.assertLess(time.time() - start, 2.0)


class TestMisc(CaddyTest):
    def test_empty_and_missing_paths(self):
        """No path and a missing path both give no services."""
        self.assertEqual(caddyconf.read_hosts(""), [])
        self.assertEqual(caddyconf.read_hosts("/nonexistent/Caddyfile"), [])

    def test_signature_tracks_mtime(self):
        """The signature changes when a file's mtime changes."""
        path = self.write("Caddyfile", "app.example.com {\n\treverse_proxy app:8080\n}\n")
        before = caddyconf.signature(self.root)
        os.utime(path, (time.time() + 100, time.time() + 100))
        self.assertNotEqual(before, caddyconf.signature(self.root))


class TestReviewRound1(CaddyTest):
    def test_snippet_arguments(self):
        """`import proxy app:8080` fills {args[0]} (and the older {args.0}) in the snippet."""
        hosts = self.hosts("(p) {\n\treverse_proxy {args[0]}\n}\n(q) {\n\treverse_proxy {args.0}\n}\n"
                           "a.example.com {\n\timport p app:8080\n}\nb.example.com {\n\timport q b:9\n}\n")
        self.assertEqual([(h["name"], h["upstream"]) for h in hosts],
                         [("a.example.com", "http://app:8080"), ("b.example.com", "http://b:9")])

    def test_heredoc_body_is_text(self):
        """A reverse_proxy line inside a heredoc is response text; the END line may carry more and later sites still count."""
        hosts = self.hosts("b.example.com {\n\trespond <<EOF\n\treverse_proxy x:1\n\tEOF 200\n}\n"
                           "c.example.com {\n\treverse_proxy c:2\n}\n")
        self.assertEqual([(h["name"], h["upstream"]) for h in hosts], [("c.example.com", "http://c:2")])

    def test_many_folders_bounded(self):
        """A root with thousands of empty folders: the scan stops at its entry budget and stays quick."""
        for i in range(caddyconf.MAX_ENTRIES + 500):
            os.mkdir(os.path.join(self.root, "d%05d" % i))
        start = time.monotonic()
        caddyconf.files(self.root)
        self.assertLess(time.monotonic() - start, 2)
        seen = []
        real = os.scandir

        def counting(path):
            it = real(path)

            class Wrap:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    it.close()

                def __iter__(self):
                    for e in it:
                        seen.append(e.name)
                        yield e
            return Wrap()
        caddyconf.os.scandir = counting
        try:
            caddyconf.files(self.root)
        finally:
            caddyconf.os.scandir = real
        self.assertLessEqual(len(seen), caddyconf.MAX_ENTRIES + 1)

    def test_file_cap(self):
        """300 files: at most MAX_FILES are listed."""
        for i in range(300):
            self.write("sites/s%03d.caddy" % i, "")
        self.assertLessEqual(len(caddyconf.files(self.root)), caddyconf.MAX_FILES)

    def test_import_fan_out_reads_each_file_once(self):
        """20 files that each import all 20 (5 levels deep would be 20^5 reads): each is read once, quickly."""
        for i in range(20):
            self.write("sites/s%02d.caddy" % i, "import *\ns%02d.example.com {\n\treverse_proxy s:%d\n}\n" % (i, i + 1))
        start = time.monotonic()
        hosts = self.hosts("import sites/*\n")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(hosts), 20)


class TestFileList(CaddyTest):
    def test_link_out_of_root_not_listed(self):
        """files() itself leaves out a link whose target is outside the root."""
        out = self.write("o.caddy", OUTSIDE, root=self.mkdtemp())
        os.symlink(out, os.path.join(self.root, "o.caddy"))                       # absolute, not under /etc/caddy
        os.symlink(os.path.relpath(out, self.root), os.path.join(self.root, "r.caddy"))   # relative, ../ out of the root
        self.assertEqual(caddyconf.files(self.root), [])

    def test_linked_folder_not_entered(self):
        """files() doesn't descend into a folder that is a link, even one pointing inside the root."""
        self.write("real/a.caddy", OUTSIDE)
        os.symlink(os.path.join(self.root, "real"), os.path.join(self.root, "linked"))
        self.assertEqual([n for n, _, _ in caddyconf.files(self.root)], ["real/a.caddy"])


class TestReviewRound2(CaddyTest):
    def test_repeated_args_do_not_multiply(self):
        """{args[0]} repeated in nested snippets can't blow up memory or time."""
        r = "{args[0]}" * 450
        txt = ("(s1) {\n\timport s2 %s\n}\n(s2) {\n\timport s3 %s\n}\n(s3) {\n\theader X %s\n}\n" % (r, r, r)
               + "a.example.com {\n\treverse_proxy a:1\n\timport s1 %s\n}\n" % ("x" * 3000))
        start = time.monotonic()
        self.assertEqual(len(self.hosts(txt)), 1)
        self.assertLess(time.monotonic() - start, 2)

    def test_many_identical_imports_fast(self):
        """A Caddyfile of 20000 `import *` lines over 150 files finishes quickly."""
        for i in range(150):
            self.write("f%03d" % i, "b%d.example.com {\n\treverse_proxy b:1\n}\n" % i)
        start = time.monotonic()
        self.assertEqual(len(self.hosts("import *\n" * 20000)), 150)
        self.assertLess(time.monotonic() - start, 2)

    def test_snippet_budget_shared_by_sites(self):
        """A big snippet imported by thousands of sites: the expansion budget is per scan, not per site."""
        txt = "(s) {\n" + "h\n" * 4900 + "}\n" + "a {\nimport s\n}\n" * 16000
        start = time.monotonic()
        self.assertEqual(self.hosts(txt), [])
        self.assertLess(time.monotonic() - start, 2)

    def test_forward_auth_on_a_path_only(self):
        """forward_auth only on /admin* doesn't mark the whole site as signed-in."""
        h = self.hosts("app.example.com {\n\thandle /admin* {\n\t\tforward_auth authelia:9091 {\n\t\t\turi /x\n\t\t}\n"
                       "\t\treverse_proxy app:80\n\t}\n\thandle {\n\t\treverse_proxy app:80\n\t}\n}\n")[0]
        self.assertEqual((h["auth"], h["login"]), (False, False))
        h = self.hosts("app.example.com {\n\tforward_auth /admin* authelia:9091\n\treverse_proxy app:80\n}\n")[0]
        self.assertFalse(h["auth"])

    def test_link_through_linked_folder_and_hidden_target(self):
        """x -> lnk/y with lnk -> realdir (two links) and z -> .secret are not listed."""
        self.write("realdir/y", OUTSIDE)
        self.write(".secret", OUTSIDE)
        os.symlink("realdir", os.path.join(self.root, "lnk"))
        os.symlink("lnk/y", os.path.join(self.root, "x"))
        os.symlink(".secret", os.path.join(self.root, "z"))
        names = [n for n, _, _ in caddyconf.files(self.root)]
        self.assertNotIn("x", names)
        self.assertNotIn("z", names)
        self.assertEqual(self.hosts("import x\nimport z\n"), [])


if __name__ == "__main__":
    unittest.main()
