"""Fishbowl's Caddyfile reader against real Caddy: tests/caddy_corpus holds common layouts (from Caddy's docs and
typical homelab setups) and expected.json what `caddy adapt` made of each one (re-record: caddy_corpus/record.py).
Every service Fishbowl finds must be a site Caddy serves, with an upstream Caddy proxies to; every site Caddy proxies
must be found, unless it is one Fishbowl skips on purpose (see SPEC-caddy.md)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
import caddyconf  # noqa: E402

CORPUS = os.path.join(HERE, "caddy_corpus")
with open(os.path.join(CORPUS, "expected.json")) as f:
    EXPECTED = json.load(f)


def skipped_on_purpose(host, upstreams, case):
    """Sites Caddy serves that Fishbowl leaves out by design."""
    names = [h for h in EXPECTED["cases"][case]["caddy"] if EXPECTED["cases"][case]["caddy"][h] == upstreams]
    return (host == "(any host)"                                   # port-only site (:8080 { ... })
            or host.startswith("*.")                               # wildcard site
            or all(u.startswith("unix/") for u in upstreams)       # unix socket upstream
            or names.index(host) > 0)                              # 2nd+ address of a multi-address site


class CaddyCorpus(unittest.TestCase):
    def test_matches_real_caddy(self):
        self.assertGreaterEqual(len(EXPECTED["cases"]), 25)
        for case, exp in EXPECTED["cases"].items():
            with self.subTest(case=case):
                ours = {s["name"]: s for s in caddyconf.read_hosts(os.path.join(CORPUS, case, "Caddyfile"))}
                for name, s in ours.items():
                    self.assertIn(name, exp["caddy"], "a site Caddy really serves with a reverse_proxy")
                    up = s["upstream"].split("://", 1)[1]
                    theirs = ["localhost" + d if d.startswith(":") else d for d in exp["caddy"][name]]   # ":9999" = this host
                    self.assertIn(up, theirs, "an upstream Caddy really proxies to")
                for host, ups in exp["caddy"].items():
                    if host not in ours:
                        self.assertTrue(skipped_on_purpose(host, ups, case), f"Caddy proxies {host}, Fishbowl missed it")

    def test_same_answers_as_recorded(self):
        """The main upstream and the sign-in flags (checked by hand when recorded) don't drift."""
        for case, exp in EXPECTED["cases"].items():
            with self.subTest(case=case):
                ours = {s["name"]: {k: s[k] for k in ("upstream", "auth", "login", "force_ssl")}
                        for s in caddyconf.read_hosts(os.path.join(CORPUS, case, "Caddyfile"))}
                self.assertEqual(ours, exp["fishbowl"])

    def test_the_main_upstream_wins(self):
        """A path-only proxy (/api/*, a websocket matcher) is a side door, also inside handle { }."""
        f = lambda case: EXPECTED["cases"][case]["fishbowl"]
        self.assertEqual(f("05-matchers")["api.example.com"]["upstream"], "http://frontend:3000")
        self.assertEqual(f("17-named-matcher")["nm.example.com"]["upstream"], "http://web:6401")
        self.assertEqual(f("27-scoped-matched")["sc.example.com"]["upstream"], "http://web:8081")
        self.assertEqual(sorted(f("26-glob-dirs")), ["app.example.com", "db.example.com"], "import sites/*/Caddyfile")


if __name__ == "__main__":
    unittest.main()
