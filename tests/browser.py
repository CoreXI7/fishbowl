"""Headless Chrome over a pipe (Chrome DevTools Protocol, stdlib only) for the page tests.
Tests that need it skip themselves when no Chrome/Chromium is installed."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

CHROME = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)


class Browser:
    def __init__(self, width=1920, height=1080):
        self.profile = tempfile.mkdtemp(prefix="hd-chrome-")
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()

        def fds():
            os.dup2(r1, 3)
            os.dup2(w2, 4)
        self.p = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={self.profile}",
                                   "--no-first-run", "--no-default-browser-check", f"--window-size={width},{height}",
                                   "--mute-audio", "about:blank"], preexec_fn=fds, pass_fds=(3, 4),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r1)
        os.close(w2)
        self.w, self.r = os.fdopen(w1, "wb", buffering=0), os.fdopen(r2, "rb", buffering=0)
        self.n, self.lock, self.waiting, self.events = 0, threading.Lock(), {}, []
        threading.Thread(target=self._reader, daemon=True).start()
        tid = self.send("Target.createTarget", {"url": "about:blank"}, None)["targetId"]
        self.sid = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True}, None)["sessionId"]
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            self.send(m)

    def _reader(self):
        buf = b""
        while chunk := self.r.read(1 << 16):
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                m = json.loads(raw)
                if m.get("id") in self.waiting:
                    ev, box = self.waiting.pop(m["id"])
                    box.append(m)
                    ev.set()
                else:
                    self.events.append(m)

    def send(self, method, params=None, sid="self", timeout=30):
        with self.lock:
            self.n += 1
            i = self.n
        ev, box = threading.Event(), []
        self.waiting[i] = (ev, box)
        msg = {"id": i, "method": method, "params": params or {}}
        sid = self.sid if sid == "self" else sid
        if sid:
            msg["sessionId"] = sid
        self.w.write(json.dumps(msg).encode() + b"\0")
        if not ev.wait(timeout):
            raise TimeoutError(method)
        if "error" in box[0]:
            raise RuntimeError(box[0]["error"])
        return box[0].get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")

    def wait_for(self, expr, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.js(expr):
                return True
            time.sleep(0.1)
        return False

    def csp_violations(self):
        return [e for e in self.events if e.get("method") == "Log.entryAdded"
                and "Content Security Policy" in json.dumps(e)]

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
