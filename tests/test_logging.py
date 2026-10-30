"""The container log must say who really did something and must not carry raw control characters
(escape sequences can rewrite what a terminal shows). A malformed request must not crash the logger."""
import contextlib
import io
import socket
import unittest

from helpers import load_app, serve


class Logging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()
        cls.srv, cls.url = serve(cls.app.Handler)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def raw(self, data):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                s.sendall(data)
                reply = b""
                while chunk := s.recv(65536):
                    reply += chunk
        return reply, out.getvalue(), err.getvalue()

    def test_control_characters_are_escaped(self):
        reply, out, err = self.raw(b"GET /\x1b[2J\x1b]0;owned\x07 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn(b"400", reply.split(b"\r\n")[0], "control characters in a path: refused")
        self.assertTrue(out.strip(), "the request was logged")
        self.assertNotIn("\x1b", out + err)
        self.assertNotIn("\x07", out + err)

    def test_malformed_request_does_not_crash_the_logger(self):
        reply, out, err = self.raw(b"GET /a b c d\r\n\r\n")
        self.assertIn(b"400", reply.split(b"\r\n")[0])
        self.assertNotIn("Traceback", err)

    def test_proxy_headers_trusted_from_the_proxy(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1", USER_HEADER="Remote-User")
        srv, _ = serve(app.Handler)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5) as s:
                port = srv.server_address[1]
                s.sendall(b"POST /api/security/ack HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                          b"Remote-User: alice\r\nX-Real-IP: 198.51.100.9\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                          b"Connection: close\r\n\r\n{}" % (port, port))
                while s.recv(65536):
                    pass
            line = [l for l in out.getvalue().splitlines() if "marked as reviewed" in l][0]
            self.assertIn("alice (198.51.100.9)", line)
        finally:
            srv.shutdown()

    def test_remote_user_header_is_not_trusted_by_default(self):
        body = b"{}"
        reply, out, err = self.raw(b"POST /api/security/ack HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                                   b"Remote-User: admin\r\nX-Real-IP: 6.6.6.6\r\nContent-Type: application/json\r\n"
                                   b"Content-Length: 2\r\nConnection: close\r\n\r\n%s" % (self.port, self.port, body))
        self.assertIn(b"200", reply.split(b"\r\n")[0])
        line = [l for l in out.splitlines() if "marked as reviewed" in l][0]
        self.assertNotIn("admin", line)
        self.assertNotIn("6.6.6.6", line, "X-Real-IP from a client that isn't the proxy")
        self.assertIn("127.0.0.1", line)


if __name__ == "__main__":
    unittest.main()
