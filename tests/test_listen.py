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

    def test_several_entries_and_ipv6_listen(self):
        """TRUSTED_PROXY may list several names/IPs (Docker's DNS gives only one address of a container on two
        networks); LISTEN may hold IPv6 addresses and repeats without crashing."""
        app = load_app(TRUSTED_PROXY="proxy.test 10.250.81.3")
        real = app.socket.getaddrinfo
        app.socket.getaddrinfo = lambda host, *a, **k: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.36", 0))] if host == "proxy.test" else real(host, *a, **k))
        try:
            self.assertEqual(app.proxy_ips(), {"172.18.0.36", "10.250.81.3"})
        finally:
            app.socket.getaddrinfo = real
        servers = app.make_servers("127.0.0.2 127.0.0.2 ::1", 0)
        try:
            self.assertEqual(len(servers), 2)
            self.assertEqual(servers[1].address_family, socket.AF_INET6)
        finally:
            for srv in servers:
                srv.server_close()

    def test_refused_before_a_thread_starts(self):
        """A client that isn't the proxy is dropped in verify_request(): no handler thread, so other containers
        can't use up the dash's threads (pids_limit) with idle connections."""
        app = load_app(TRUSTED_PROXY="10.250.81.3")
        srv = app.make_servers("127.0.0.2", 0)[0]
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = socket.socket()
            c.bind(("127.0.0.3", 0))
            c.settimeout(3)
            c.connect(("127.0.0.2", srv.server_address[1]))
            c.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertEqual(c.recv(100), b"", "closed without an answer")
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()

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
