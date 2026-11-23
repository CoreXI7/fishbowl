"""Caddyfile sites as Fishbowl services (for people who run Caddy instead of Nginx Proxy Manager).

CADDYFILE is the Caddyfile's path inside the container, mounted read-only with its folder (the root). Every site block with
a usable address and a reverse_proxy to an http(s) upstream becomes one service, in the same shape as an NPM proxy host.

These files are data from another program. A hostile or odd file must not make the dashboard read anything outside the
root, read files the Caddyfile doesn't import, hang, or use much CPU or memory:
- files(root) lists the regular files under root (depth <= 2, no hidden names, no linked folders; links only when their
  target stays inside root and is a regular file). That list is the change signature and the only set an import may reach;
- only the Caddyfile and the files it imports are opened (nginxconf.read_text: O_NOFOLLOW | O_NONBLOCK, 256 KB, re-check
  of the opened path), each at most once, import depth <= MAX_IMPORT_DEPTH;
- the lexer and tree walk are linear, tokens per file and nesting are capped; snippet expansion is depth- and size-capped.
"""
import collections
import fnmatch
import os
import re
import stat

import nginxconf

MAX_FILES = 200
MAX_ENTRIES = 5000                   # directory entries looked at per scan (folders count too)
MAX_SITES = 200
MAX_DEPTH = 20
MAX_TOKENS = 100_000
MAX_IMPORT_DEPTH = 5
MAX_SNIPPET_DEPTH = 3
MAX_EXPANDED = 50_000                # snippet lines expanded per scan, all sites together
MAX_SUB_CHARS = 2_000_000            # characters produced by {args[N]} substitution per scan
MAX_SITE_BLOCKS = 2000               # site blocks looked at per scan (with or without a service)
MAX_TOKEN = 4096
CADDY_PREFIX = "/etc/caddy"
UPSTREAM = re.compile(r"(?:(https?|h2c)://)?(" + nginxconf.HOST + r")?(?::(\d{1,5}))?/?\Z")
SCOPES = ("handle", "route", "handle_path")


def _no_comment(line):
    """Caddy's rule: # starts a comment only at the start of a token (not inside quotes or a word like a#b)."""
    q, esc, prev = None, False, " "
    for i, ch in enumerate(line):
        if q:
            if esc:
                esc = False
            elif ch == "\\" and q == '"':
                esc = True
            elif ch == q:
                q = None
        elif ch in "\"`":
            q = ch
        elif ch == "#" and prev in " \t":
            return line[:i]
        prev = ch
    return line


def files(root):
    """[(relative name, real path, mtime_ns)] under root, in name order. Empty when root is unset or missing."""
    if not root or not os.path.isdir(root):
        return []
    picks, todo, budget = [], [(root, 0)], MAX_ENTRIES
    while todo:
        folder, depth = todo.pop(0)
        names = []
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if budget <= 0:
                        break
                    budget -= 1
                    names.append(e.name)
        except OSError:
            continue
        names.sort()
        for n in names:
            if len(picks) >= MAX_FILES:
                return picks
            if n.startswith("."):
                continue
            entry = os.path.join(folder, n)
            try:
                st = os.lstat(entry)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if depth < 2:
                    todo.append((entry, depth + 1))
                continue
            real = nginxconf._resolve(entry, folder, root, CADDY_PREFIX)
            if real:
                try:
                    picks.append((os.path.relpath(entry, root), real, os.lstat(real).st_mtime_ns))
                except OSError:
                    pass
    return picks


def signature(root):
    return tuple((n, m) for n, _, m in files(root))


# ---- lexer and tree ------------------------------------------------------------------------------------------------

def lex(txt):
    """[[token, ...] per line]. Quotes are kept as one token (without the quotes); a heredoc (`<<END` ... `END`) is one
    opaque token; at most MAX_TOKENS tokens."""
    lines, cur, n, heredoc = [], [], 0, None
    for raw in txt.split("\n"):
        if heredoc is not None:
            first = raw.split(None, 1)
            if not first or first[0] != heredoc:
                continue
            heredoc = None                   # the END line: the rest of it (e.g. a status code) belongs to the directive
            cur.append("<<heredoc>>")
            raw = first[1] if len(first) > 1 else ""
        line, i, L = raw, 0, len(raw)
        while i < L:
            ch = line[i]
            if ch in " \t\r":
                i += 1
                continue
            if ch in "\"`":
                j, buf = i + 1, []
                while j < L and line[j] != ch:
                    if ch == '"' and line[j] == "\\" and j + 1 < L:
                        j += 1
                    buf.append(line[j])
                    j += 1
                cur.append("".join(buf))
                i = j + 1
            else:
                j = i
                while j < L and line[j] not in " \t\r":
                    j += 1
                cur.append(line[i:j])
                i = j
            n += 1
            if n >= MAX_TOKENS:
                break
        if cur and re.fullmatch(r"<<[A-Za-z_][A-Za-z0-9_]{0,31}", cur[-1]):
            heredoc = cur.pop()[2:]          # the body up to the END line is text, not directives
            continue
        if cur:
            lines.append(cur)
            cur = []
        if n >= MAX_TOKENS:
            break
    return lines


def tree(lines):
    """[{"head": [...], "kids": [...] or None}] for the top level. Unbalanced text ends the walk."""
    top = []
    stack = [top]
    for toks in lines:
        if toks == ["}"]:
            if len(stack) == 1:
                break
            stack.pop()
            continue
        if toks[-1] == "{":
            if len(stack) > MAX_DEPTH:
                break
            node = {"head": toks[:-1], "kids": []}
            stack[-1].append(node)
            stack.append(node["kids"])
            continue
        if toks[0] == "}":                  # "} else ..." style: close, the rest is ignored
            if len(stack) > 1:
                stack.pop()
            continue
        stack[-1].append({"head": toks, "kids": None})
    return top


# ---- files and imports ----------------------------------------------------------------------------------------------

def _import_targets(arg, here, root, listed):
    """The listed files an `import <arg>` reaches (relative names, sorted), or []."""
    if "{" in arg:
        return []
    if os.path.isabs(arg):
        norm = os.path.normpath(arg)
        if norm != CADDY_PREFIX and not norm.startswith(CADDY_PREFIX + "/"):
            return []
        rel = os.path.relpath(norm, CADDY_PREFIX)
    else:
        rel = os.path.normpath(os.path.join(here, arg))
    if rel.startswith("..") or os.path.isabs(rel):
        return []
    if any(c in rel for c in "*?["):
        return sorted(n for n in listed if os.path.dirname(n) == os.path.dirname(rel) and fnmatch.fnmatchcase(n, rel))
    return [rel] if rel in listed else []


def load(root, caddyfile, picked=None):
    """[(relative name, top-level nodes)] for the Caddyfile and every file it imports (each once, depth-capped)."""
    listing = files(root) if picked is None else picked
    listed = {n: real for n, real, _ in listing}
    start = os.path.relpath(caddyfile, root)
    out, seen, todo = [], {start}, collections.deque([(start, 0)])   # seen = read or queued: each file once
    done = set()                                                     # (import argument, folder) pairs handled
    while todo:
        name, depth = todo.popleft()
        if name not in listed:
            continue
        txt = nginxconf.read_text(listed[name], root, strip=_no_comment)
        if txt is None:
            continue
        nodes = tree(lex(txt))
        out.append((name, nodes))
        if depth >= MAX_IMPORT_DEPTH:
            continue
        for node in nodes:
            h = node["head"]
            if node["kids"] is None and len(h) >= 2 and h[0] == "import":
                key = (h[1], os.path.dirname(name))
                if key in done:
                    continue                 # the same import line again: already queued
                done.add(key)
                for t in _import_targets(h[1], os.path.dirname(name), root, listed):
                    if t not in seen and len(seen) < MAX_FILES:
                        seen.add(t)
                        todo.append((t, depth + 1))
    return out


# ---- sites ----------------------------------------------------------------------------------------------------------

def _expand(kids, snippets, depth=0, budget=None):
    """kids with `import name` lines replaced by that snippet's body (depth- and size-capped)."""
    budget = budget if budget is not None else [MAX_EXPANDED, MAX_SUB_CHARS]
    out = []
    for node in kids:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        h = node["head"]
        if node["kids"] is None and len(h) >= 2 and h[0] == "import" and h[1] in snippets:
            if depth < MAX_SNIPPET_DEPTH:
                out += _expand(_with_args(snippets[h[1]], h[2:12], budget), snippets, depth + 1, budget)
            continue
        if node["kids"] is not None:
            node = {"head": h, "kids": _expand(node["kids"], snippets, depth, budget)}
        out.append(node)
    return out


def _with_args(kids, args, budget, depth=0):
    """A snippet body with {args[N]} / {args.N} replaced by the import's arguments (Caddy's snippet arguments)."""
    def sub(tok):
        if "{args" not in tok:
            return tok
        new = re.sub(r"\{args(?:\[(\d)\]|\.(\d))\}",
                     lambda m: args[int(m.group(1) or m.group(2))] if int(m.group(1) or m.group(2)) < len(args) else m.group(0), tok)
        if len(new) > MAX_TOKEN or len(new) > budget[1]:
            return tok                       # too big (repeated {args} can multiply per level): keep it unfilled
        budget[1] -= len(new)
        return new
    out = []
    for node in kids:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            break
        budget[0] -= 1
        out.append({"head": [sub(t) for t in node["head"]],
                    "kids": None if node["kids"] is None else _with_args(node["kids"], args, budget, depth + 1)})
    return out


def _walk(kids, depth=0):
    for node in kids:
        yield node
        if node["kids"] is not None and depth < MAX_DEPTH:
            yield from _walk(node["kids"], depth + 1)


def _pick_name(head):
    for a in " ".join(head).replace(",", " ").split()[:50]:
        if "{" in a or "*" in a:
            continue
        host = re.sub(r"^https?://", "", a).split("/", 1)[0]
        host = re.sub(r":\d{1,5}\Z", "", host)
        if host and host != "localhost" and nginxconf.NAME_OK.match(host):
            return host, not a.startswith("http://")
    return None, False


def _unmatched(args):
    """No matcher in front: nothing, `*` or `/*`, or a first argument that isn't a path or @name."""
    return not args or args[0] in ("*", "/*") or not args[0].startswith(("/", "@"))


def _proxy_args(node):
    args = node["head"][1:]
    if args and (args[0].startswith(("/", "@")) or args[0] == "*"):
        args = args[1:]
    if not args and node["kids"]:
        for k in node["kids"]:
            if k["kids"] is None and k["head"][:1] == ["to"] and len(k["head"]) > 1:
                args = k["head"][1:]
                break
    return args


def _target(up):
    if "{" in up or up.startswith("unix/"):
        return None
    m = UPSTREAM.match(up)
    if not m or (not m.group(2) and not m.group(3)):
        return None
    scheme = "https" if m.group(1) == "https" else "http"
    port = m.group(3) or ("443" if scheme == "https" else "80")
    if not 1 <= int(port) <= 65535:
        return None
    return f"{scheme}://{m.group(2) or 'localhost'}:{int(port)}"


def _site(head, kids, snippets, budget=None):
    name, force_ssl = _pick_name(head)
    if not name:
        return None
    kids = _expand(kids, snippets, budget=budget)
    first = [n for n in kids if n["head"][:1] == ["reverse_proxy"] and _unmatched(n["head"][1:2])]
    scoped = [k for n in kids if n["kids"] is not None and n["head"][:1] and n["head"][0] in SCOPES
              and _unmatched(n["head"][1:2]) for k in n["kids"] if k["head"][:1] == ["reverse_proxy"]]
    anywhere = [n for n in _walk(kids) if n["head"][:1] == ["reverse_proxy"]]
    upstream = None
    for node in (first + scoped + anywhere)[:100]:
        args = _proxy_args(node)
        upstream = _target(args[0]) if args else None
        if upstream:
            break
    if not upstream:
        return None                                  # a redirect, file_server or respond site: not a service
    gates = [n for n in kids] + [k for n in kids if n["kids"] is not None and n["head"][:1] and n["head"][0] in SCOPES
                                 and _unmatched(n["head"][1:2]) for k in n["kids"]]
    # only a gate in front of the whole site counts (forward_auth on /admin* alone leaves the rest public)
    auth = any(n["head"][:1] == ["forward_auth"] and _unmatched(n["head"][1:2]) for n in gates)
    basic = any(n["head"][:1] in (["basic_auth"], ["basicauth"]) and _unmatched(n["head"][1:2]) for n in gates)
    return {"name": name, "upstream": upstream, "auth": auth, "login": auth or basic,
            "caches": False, "force_ssl": force_ssl, "plain": True, "caddy": True}


def parse_files(loaded):
    """Services from load()'s result."""
    snippets = {}
    for _, nodes in loaded:
        for n in nodes:
            h = n["head"]
            if n["kids"] is not None and len(h) == 1 and re.fullmatch(r"\([A-Za-z0-9._-]{1,64}\)", h[0]):
                snippets.setdefault(h[0][1:-1], n["kids"])
    out, seen, budget, blocks = [], set(), [MAX_EXPANDED, MAX_SUB_CHARS], 0
    for _, nodes in loaded:
        for n in nodes:
            h = n["head"]
            if n["kids"] is None or not h or h[0].startswith(("(", "&(")):
                continue
            blocks += 1
            if blocks > MAX_SITE_BLOCKS:
                return out
            s = _site(h, n["kids"], snippets, budget)
            if s and s["name"] not in seen:
                seen.add(s["name"])
                out.append(s)
                if len(out) >= MAX_SITES:
                    return out
    return out


def read_hosts(caddyfile, picked=None):
    """Every service in the Caddyfile and the files it imports. picked = a files(root) result to reuse."""
    if not caddyfile:
        return []
    root = os.path.dirname(caddyfile)
    return parse_files(load(root, caddyfile, picked))
