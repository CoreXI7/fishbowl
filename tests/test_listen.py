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
        app = load_app(TRUSTED_PROXY="proxy.test 10.89.250.3")
        real = app.socket.getaddrinfo
        app.socket.getaddrinfo = lambda host, *a, **k: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.31.0.36", 0))] if host == "proxy.test" else real(host, *a, **k))
        try:
            self.assertEqual(app.proxy_ips(), {"172.31.0.36", "10.89.250.3"})
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
        app = load_app(TRUSTED_PROXY="10.89.250.3")
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

    def test_own_address_is_allowed(self):
        """The dash health-checks its own proxy host (dash.example -> dash:8000), i.e. connects to itself from its
        own address: that must still get an answer."""
        app = load_app(TRUSTED_PROXY="10.89.250.3")
        srv = app.make_servers("127.0.0.2", 0)[0]
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = socket.socket()
            c.bind(("127.0.0.2", 0))
            c.settimeout(3)
            c.connect(("127.0.0.2", srv.server_address[1]))
            c.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            self.assertIn(b"200", c.recv(100))
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def resolving(self, app, answers):
        """Make app's lookups of "proxy.test" answer from answers[0] (a list of IPs or an exception), then the next..."""
        real = app.socket.getaddrinfo

        def fake(host, *a, **k):
            if host != "proxy.test":
                return real(host, *a, **k)
            got = answers[0] if len(answers) == 1 else answers.pop(0)
            if isinstance(got, Exception):
                raise got
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in got]
        app.socket.getaddrinfo = fake
        self.addCleanup(lambda: setattr(app.socket, "getaddrinfo", real))

    def again(self, app):
        app._proxy_ip["at"] = float("-inf")     # (looked up again: normally once a minute)
        return app.proxy_ips()

    def test_proxy_with_several_addresses(self):
        # Docker answers a name with one address per lookup, even for a container on two shared networks (the second
        # one is listed by hand); one IPv4 and one IPv6 address are one dual-stack container.
        app = load_app(TRUSTED_PROXY="proxy.test 10.9.0.3")
        self.resolving(app, [["10.0.0.5", "fd00::5"]])
        self.assertEqual(app.proxy_ips(), {"10.0.0.5", "fd00::5", "10.9.0.3"})

    def test_a_name_two_containers_answer_to_is_trusted_for_neither(self):
        # Two answers of one kind: another container shares the name (a compose service called nginx on the proxy's
        # network gets that alias); either could be the proxy, so neither is (audit 2026-10-30).
        app = load_app(TRUSTED_PROXY="proxy.test")
        self.resolving(app, [["10.0.0.5", "10.0.0.66"]])
        self.assertEqual(app.proxy_ips(), set())
        checks = {c["id"]: c for c in app.setup_checks()}
        self.assertEqual(checks["proxy"]["state"], "problem")
        self.assertIn("10.0.0.66", checks["proxy"]["detail"])

    def test_a_removed_proxy_is_trusted_no_more(self):
        app = load_app(TRUSTED_PROXY="proxy.test")
        self.resolving(app, [["10.0.0.5"], socket.gaierror(socket.EAI_AGAIN, "busy"), socket.gaierror(socket.EAI_NONAME, "gone")])
        self.assertEqual(app.proxy_ips(), {"10.0.0.5"})
        self.assertEqual(self.again(app), {"10.0.0.5"}, "a lookup that failed for now keeps the last answer")
        self.assertEqual(self.again(app), set(), "a name that no longer exists: its old address (soon someone else's) isn't trusted")

    def test_sso_mode_without_listen_is_a_warning(self):
        # With AUTH: proxy, a container that fakes the proxy's address (ARP, NET_RAW is on by default) is the admin:
        # LISTEN stops being optional (audit round 2)
        app = load_app(TRUSTED_PROXY="127.0.0.1", AUTH="proxy")
        item = {i["id"]: i for i in app.setup_checks()}["listen"]
        self.assertEqual((item["state"], item["optional"]), ("warn", False))
        app = load_app(TRUSTED_PROXY="127.0.0.1", AUTH="login")
        self.assertTrue({i["id"]: i for i in app.setup_checks()}["listen"]["optional"])


    def test_a_name_isnt_trusted_on_more_than_one_network(self):
        # Docker answers a name from one of the container's networks only: a container on another network of the dash
        # holding the proxy's name as an alias became the only "proxy" (audit round 2). On more than one network,
        # without LISTEN, a name isn't trusted; the proxy's IP still is.
        app = load_app(TRUSTED_PROXY="proxy.test 10.9.0.3")
        self.resolving(app, [["10.0.0.5"]])
        app.network_count = lambda: 2
        self.assertEqual(app.proxy_ips(), {"10.9.0.3"})
        item = {i["id"]: i for i in app.setup_checks()}["proxy"]
        self.assertEqual(item["state"], "problem")
        self.assertIn("more than one", item["title"])
        app.network_count = lambda: 1
        self.assertEqual(self.again(app), {"10.0.0.5", "10.9.0.3"})
        app = load_app(TRUSTED_PROXY="proxy.test", LISTEN="127.0.0.1")
        self.resolving(app, [["10.0.0.5"]])
        app.network_count = lambda: 2
        self.assertEqual(app.proxy_ips(), {"10.0.0.5"}, "with LISTEN, only the proxy's own network reaches the dash")

    def test_an_ipv6_only_network_counts(self):
        # (only IPv4 routes were counted: on an IPv6-only network a name was still trusted, and Docker answered it from
        # that network alone; audit round 4)
        app = load_app()
        v4 = "Iface\tDestination\tGateway\neth0\t0000FE0A\t00000000\n"
        v6 = ("fd000000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 "
              "00000100 00000001 00000000 00000001 eth1\n"
              "00000000000000000000000000000001 80 00000000000000000000000000000000 00 00000000000000000000000000000000 "
              "00000000 00000002 00000000 80200001 lo\n")
        self.assertEqual(app.count_networks(v4, v6), 2)
        self.assertEqual(app.count_networks(v4, ""), 1)
        self.assertEqual(app.count_networks("", ""), 1)

    def test_an_odd_name_doesnt_crash(self):
        app = load_app(TRUSTED_PROXY="a..b " + "x" * 70)
        self.assertEqual(app.proxy_ips(), set())


    def test_an_ipv4_proxy_on_an_ipv6_socket_counts(self):
        # (LISTEN "::" takes IPv4 connections as ::ffff:a.b.c.d, which never matched TRUSTED_PROXY; audit round 3)
        app = load_app(TRUSTED_PROXY="10.9.0.3")
        self.assertTrue(app.allowed_ip("::ffff:10.9.0.3"))
        self.assertTrue(app.is_proxy_address("::ffff:10.9.0.3"))

    def test_its_own_address_gets_only_public_files_behind_a_proxy(self):
        # rootless Podman 4's port forwarder makes every visitor the container's own address, which was let in as "the
        # dashboard itself" with a proxy set: with AUTH=proxy, past the SSO (audit round 4)
        import http.client
        from helpers import serve
        app = load_app(TRUSTED_PROXY="10.9.0.3", AUTH="proxy")
        srv, _ = serve(app.Handler, "127.0.0.2")
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.2", srv.server_address[1], timeout=10, source_address=("127.0.0.2", 0))
                c.request("GET", path)
                return c.getresponse().status
            self.assertEqual(get("/api/settings"), 403, "not the dashboard's data")
            self.assertEqual(get("/"), 403, "its own health check still counts as up (any answer under 500)")
            self.assertEqual(get("/favicon.ico"), 200, "its own icon still loads")
        finally:
            srv.shutdown()

    def test_the_refusal_log_says_why_names_were_skipped(self):
        app = load_app(TRUSTED_PROXY="proxy.test")
        self.resolving(app, [["10.0.0.5"]])
        app.network_count = lambda: 2
        self.assertIn("more than one", app.proxy_refusal_note())


if __name__ == "__main__":
    unittest.main()
