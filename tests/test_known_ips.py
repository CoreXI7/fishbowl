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
        self.assertTrue(self.app.is_known("2001:db8::1:0:0:9"))

    def test_junk(self):
        for v in ("", None, 5, "not an ip"):
            self.assertFalse(self.app.is_known(v))


if __name__ == "__main__":
    unittest.main()
