"""Other containers on the proxy's Docker network can reach the dash. LISTEN lets it answer only on the
addresses you give (e.g. a network shared with the proxy alone), and the proxy check accepts every address
the proxy's name resolves to (a container on two networks has one address per network)."""
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

from helpers import APP_DIR, load_app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Listen(unittest.TestCase):
    def test_listen_only_on_the_given_address(self):
        app = load_app()                     # just for its fresh environment + data folder
        port = free_port()
        env = {**os.environ, "LISTEN": "127.0.0.2", "PORT": str(port), "TRUSTED_PROXY": ""}
        p = subprocess.Popen([sys.executable, "-I", os.path.join(APP_DIR, "app.py")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.2:{port}/healthz", timeout=1).read()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("not answering on the LISTEN address")
            with self.assertRaises(OSError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
        finally:
            p.terminate()
            p.wait(10)
        self.assertTrue(app)

    def test_proxy_with_several_addresses(self):
        app = load_app(TRUSTED_PROXY="proxy.test")
        real = app.socket.getaddrinfo
        app.socket.getaddrinfo = lambda host, *a, **k: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.0.3", 0))]
            if host == "proxy.test" else real(host, *a, **k))
        try:
            self.assertEqual(app.proxy_ips(), {"10.0.0.5", "10.9.0.3"})
        finally:
            app.socket.getaddrinfo = real


if __name__ == "__main__":
    unittest.main()
