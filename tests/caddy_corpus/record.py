#!/usr/bin/env python3
"""Record what real Caddy makes of each tests/caddy_corpus/*/Caddyfile, for tests/test_caddy_corpus.py.

Each case goes through `caddy adapt` in the official image (no network, folder mounted read-only at /etc/caddy, so
absolute imports work as on a real install). Saved per case: every site host -> the upstreams of its reverse_proxy
handlers (forward_auth's own requests and php_fastcgi left out), and what Fishbowl's reader finds.
Usage: python3 tests/caddy_corpus/record.py [image]   (default caddy:2.11.7-alpine; needs Docker). Review the diff.
"""
import json, os, subprocess, sys

CORPUS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(CORPUS, "..", "..", "app"))
import caddyconf  # noqa: E402

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "caddy:2.11.7-alpine"


def dials(node):
    out = []
    if isinstance(node, dict):
        if node.get("handler") == "reverse_proxy" and (node.get("transport") or {}).get("protocol") != "fastcgi" \
                and not node.get("handle_response"):
            out += [u.get("dial", "") for u in node.get("upstreams") or []]
        for v in node.values():
            out += dials(v)
    elif isinstance(node, list):
        for v in node:
            out += dials(v)
    return out


def main():
    result = {"caddy_image": IMAGE, "cases": {}}
    for case in sorted(os.listdir(CORPUS)):
        folder = os.path.join(CORPUS, case)
        if not os.path.isdir(folder) or case.startswith(("_", ".")):
            continue
        js = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{folder}:/etc/caddy:ro", IMAGE,
                             "caddy", "adapt", "--config", "/etc/caddy/Caddyfile"],
                            capture_output=True, text=True, check=True).stdout
        sites = {}
        for srv in ((json.loads(js).get("apps") or {}).get("http") or {}).get("servers", {}).values():
            for r in srv.get("routes", []):
                hosts = [h for m in r.get("match", [{}]) for h in m.get("host", [])] or ["(any host)"]
                for h in hosts:
                    sites.setdefault(h, []).extend(dials(r.get("handle", [])))
        ours = caddyconf.read_hosts(os.path.join(folder, "Caddyfile"))
        result["cases"][case] = {
            "caddy": {h: d for h, d in sorted(sites.items()) if d},
            "fishbowl": {s["name"]: {k: s[k] for k in ("upstream", "auth", "login", "force_ssl")} for s in ours}}
    with open(os.path.join(CORPUS, "expected.json"), "w") as f:
        json.dump(result, f, indent=1, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    main()
