"""Fishbowl's adapter for signin-core's attack runner (signin-core tests/attack_core.py): a real Fishbowl in login mode
in the sandbox, behind 127.0.0.1 as its trusted proxy, which sends X-Forwarded-Proto and X-Real-IP as a real proxy does.
The port is one the host firewall doesn't open (the app binds every address)."""
import os, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST, PORT = "dash.example", 18497


class Adapter:
    name, cookie = "fishbowl", "fishbowl"
    setup_code_in_log = True   # (printed at start while there is no account: the docs read it with docker logs)
    pages = ("/", "/settings")
    public_get = ("/login", "/healthz", "/style.css", "/api/auth/state")
    private_get = ("/api/services", "/api/system", "/api/requests", "/api/settings")

    def start(self, base):
        self.lines, self.code = [], None
        for d in ("data", "npm", "hw", "req"):
            os.makedirs(os.path.join(base, d))
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "DATA_DIR": os.path.join(base, "data"),
               "NPM_DIR": os.path.join(base, "npm"), "HW_FILE": os.path.join(base, "hw", "hw.json"),
               "REQ_DIR": os.path.join(base, "req"), "AUTH": "login", "TRUSTED_PROXY": "127.0.0.1",
               "ORIGIN": f"https://{HOST}", "LISTEN": "127.0.0.1", "PORT": str(PORT), "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen([sys.executable, "app/app.py"], env=env, cwd=ROOT,   # (as the container runs it)
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._read, daemon=True).start()
        for _ in range(150):
            if self.code and self._up():
                return
            time.sleep(0.1)
        raise RuntimeError("Fishbowl did not start: " + "".join(self.lines)[-400:])

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)
            if line.startswith("SETUP CODE:") and not self.code:
                self.code = line.split(":", 1)[1].split()[0]

    def _up(self):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.5).close()
            return True
        except OSError:
            return False

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def log(self):
        return "".join(self.lines)

    def setup_code(self):
        return self.code

    def send(self, raw):
        s = socket.create_connection(("127.0.0.1", PORT), 30)
        try:
            s.sendall(raw)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk
        except socket.timeout:
            return b"TIMEOUT"
        except OSError as exc:
            return repr(exc).encode()
        finally:
            s.close()

    def headers(self, ip):
        return [("Host", HOST), ("X-Forwarded-Proto", "https"), ("X-Real-IP", ip)]

    def csrf_ok(self):
        return [("Origin", f"https://{HOST}")]

    def csrf_bad(self):
        return [("no-Origin", []), ("foreign-Origin", [("Origin", "https://evil.example")]),
                ("cross-site", [("Origin", f"https://{HOST}"), ("Sec-Fetch-Site", "cross-site")])]
