"""Shared test helpers (stdlib only): load a fresh copy of the app with its own environment, serve it on
127.0.0.1, and fake upstream services (qBittorrent, Seerr, Tautulli, any web app) that answer whatever a test wants.

Run all tests from the repo root:  python3 -m unittest discover -s tests
Temporary files go to $TMPDIR (set it to somewhere that isn't /tmp if your host watches /tmp)."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
_n = [0]


def load_app(**env):
    """Import app/app.py as a new module with a fresh data folder and these environment variables.
    settings.py reads the environment at import, so it is re-imported too."""
    tmp = tempfile.mkdtemp(prefix="hd-test-")
    for d in ("data", "npm", "hw", "req"):
        os.makedirs(os.path.join(tmp, d))
    base = {"DATA_DIR": os.path.join(tmp, "data"), "NPM_DIR": os.path.join(tmp, "npm"), "TRUSTED_PROXY": "",
            "HW_FILE": os.path.join(tmp, "hw", "hw.json"), "REQ_DIR": os.path.join(tmp, "req"), "DEMO": "",
            "ORIGIN": "", "SETTINGS_UI": "1"}
    for k in ("QBIT_URL", "QBIT_USER", "QBIT_PASS", "SEERR_URL", "SEERR_KEY", "TAUTULLI_URL", "TAUTULLI_KEY",
              "HOME_IP", "CHECK_VIA_PROXY", "PROXY_SECRET", "USER_HEADER"):
        os.environ.pop(k, None)
    os.environ.update({**base, **env})
    for m in ("settings", "demo"):
        sys.modules.pop(m, None)
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    _n[0] += 1
    spec = importlib.util.spec_from_file_location(f"app_under_test_{_n[0]}", os.path.join(APP_DIR, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TEST_TMP = tmp
    return mod


def serve(handler, host="127.0.0.1"):
    """Run an HTTP handler class in a background thread. Returns (server, "http://127.0.0.1:port")."""
    srv = ThreadingHTTPServer((host, 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}"


class Upstream:
    """A fake web service. routes: path (without query) -> (status, headers dict, body bytes|str|dict|list).
    Every request is recorded in .hits as (method, full path, headers)."""
    def __init__(self, routes=None):
        self.routes, self.hits = dict(routes or {}), []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self):
                up.hits.append((self.command, self.path, dict(self.headers)))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                route = up.routes.get(self.path.split("?")[0])
                if callable(route):
                    route = route(self)
                    if route is None:
                        return
                status, headers, body = route or (404, {}, b"not found")
                if isinstance(body, (dict, list)):
                    body, headers = json.dumps(body).encode(), {"Content-Type": "application/json", **headers}
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

        self.server, self.url = serve(H)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
