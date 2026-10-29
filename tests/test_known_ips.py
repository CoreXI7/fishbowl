"""Known-IP entries: full addresses and prefixes, IPv4 and IPv6. The settings page accepts IPv6 prefixes like
"2001:db8:", so they must actually match (they never did), and IPv6 case/compression must not matter."""
import unittest

from helpers import load_app


class KnownIPs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()
        new = cls.app.cfgstore.validate({**cls.app.ED, "known_ips": ["203.0.113.", "198.51.100.7", "2001:db8:", "2001:DB8:0:0:1::9"]},
                                        cls.app.ED)
        cls.app.cfgstore.save(new, cls.app.STORE)
        cls.app.apply_settings()

    def test_ipv4(self):
        k = self.app.is_known
        self.assertTrue(k("203.0.113.40"))
        self.assertTrue(k("198.51.100.7"))
        self.assertFalse(k("198.51.100.70"))
        self.assertFalse(k("203.0.11.1"))

    def test_ipv6_prefix(self):
        k = self.app.is_known
        self.assertTrue(k("2001:db8::5"))
        self.assertTrue(k("2001:DB8:abcd::1"))
        self.assertFalse(k("2001:db80::1"))

    def test_ipv6_full_address_any_spelling(self):
        app = load_app()             # without the 2001:db8: prefix, so only the full-address match can make it true
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "known_ips": ["2001:DB8:0:0:1::9"]}, app.ED), app.STORE)
        app.apply_settings()
        self.assertTrue(app.is_known("2001:db8::1:0:0:9"))
        self.assertFalse(app.is_known("2001:db8::1:0:0:8"))

    def test_ipv6_prefix_any_spelling(self):
        app = load_app()
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "known_ips": ["2001:db8:0:", "2600:0abc:"]}, app.ED), app.STORE)
        app.apply_settings()
        self.assertTrue(app.is_known("2001:db8::5"))         # 2001:db8:0:0:...: zero groups compressed by sshd
        self.assertTrue(app.is_known("2600:abc::1"))         # leading zeros in the prefix
        self.assertFalse(app.is_known("2001:db8:1::5"))

    def test_junk(self):
        for v in ("", None, 5, "not an ip"):
            self.assertFalse(self.app.is_known(v))


if __name__ == "__main__":
    unittest.main()


class AddKnown(unittest.TestCase):
    """Adding known IPs from the SSH view: only full addresses that the detail view shows as real logins, never
    something the card's data held (a crafted log line once made "6." look like a login IP: a whole /8)."""
    def setUp(self):
        import json
        self.app = load_app()
        hw = {"updated": 9e9, "security": {"ssh": {"logins": [{"time": "t1", "user": "root", "ip": "6."},
                                                              {"time": "t2", "user": "a", "ip": "198.51.100.7"}]}}}
        det = {"updated": 9e9, "ssh": {"logins": [{"raw": "t2", "user": "a", "ip": "198.51.100.7"}],
                                       "sources": [{"user": "a", "ip": "198.51.100.7"}, {"user": "b", "ip": "203.0.113."}]}}
        json.dump(hw, open(self.app.HW_FILE, "w"))
        json.dump(det, open(self.app.SEC_DETAIL_FILE, "w"))

    def test_add_all_only_full_addresses_from_the_details(self):
        self.assertEqual(self.app.add_known_ips(everything=True)["added"], ["198.51.100.7"])
        self.assertFalse(self.app.is_known("6.6.6.6"))

    def test_single_prefix_refused(self):
        for ip in ("6.", "203.0.113."):
            with self.assertRaises(self.app.cfgstore.Invalid):
                self.app.add_known_ips(ip)
