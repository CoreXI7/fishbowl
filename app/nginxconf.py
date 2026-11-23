"""Plain Nginx site configs as Fishbowl services (for people who run Nginx without Nginx Proxy Manager).

Reads NGINX_DIR (the host's /etc/nginx mounted read-only): the files in sites-enabled/ and conf.d/*.conf, or the folder's
own *.conf when it has neither. Every `server { ... }` block with a usable server_name and a proxy_pass to an http(s)
address becomes one service, in the same shape as an NPM proxy host.

These files are data from another program. A hostile or odd file must not make the dashboard read anything outside
NGINX_DIR, hang, or use much CPU or memory:
- only regular files, opened with O_NOFOLLOW | O_NONBLOCK, at most 256 KB each, at most MAX_FILES files and MAX_BLOCKS
  server blocks per scan;
- sites-enabled links are followed only when the target, mapped into NGINX_DIR (an absolute /etc/nginx/... target becomes
  NGINX_DIR/...; a relative one is resolved from the link's folder), stays inside NGINX_DIR after realpath; the target
  itself is then opened with O_NOFOLLOW (no link chains);
- comments and lines over 4 KB are dropped before any pattern runs; the brace walk is linear and stops at MAX_DEPTH.
"""
import os
import re
import stat

MAX_BYTES = 256 * 1024
MAX_FILES = 200
MAX_BLOCKS = 200
MAX_DEPTH = 20
NGINX_PREFIX = "/etc/nginx"
HOST = r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]{2,45}\])"   # starts and ends with a letter/digit
NAME_OK = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
TARGET = re.compile(r"(https?)://(" + HOST + r")(?::(\d{1,5}))?(?:/[^\s;]*)?\Z")


def _inside(path, root):
    root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _resolve(entry, folder, root):
    """The real file behind a directory entry, or None. A link may point anywhere under NGINX_PREFIX on the host; its
    target is mapped into the mounted root and must stay inside it."""
    try:
        st = os.lstat(entry)
    except OSError:
        return None
    if stat.S_ISREG(st.st_mode):
        return entry if _inside(entry, root) else None
    if not stat.S_ISLNK(st.st_mode):
        return None
    try:
        target = os.readlink(entry)
    except OSError:
        return None
    if os.path.isabs(target):
        norm = os.path.normpath(target)
        if norm != NGINX_PREFIX and not norm.startswith(NGINX_PREFIX + "/"):
            return None
        mapped = os.path.join(root, os.path.relpath(norm, NGINX_PREFIX))
    else:
        mapped = os.path.normpath(os.path.join(folder, target))
    if not _inside(mapped, root):
        return None
    try:
        if not stat.S_ISREG(os.lstat(mapped).st_mode):   # the target must be a plain file itself: no link chains
            return None
    except OSError:
        return None
    return mapped


def files(root):
    """[(entry name, real path, mtime_ns)] to read, in name order. Empty when root is unset or missing."""
    if not root or not os.path.isdir(root):
        return []
    picks = []
    sub = [(d, None) for d in ("sites-enabled",)] + [("conf.d", ".conf")]
    folders = [(os.path.join(root, d), ext) for d, ext in sub
               if os.path.isdir(os.path.join(root, d)) and not os.path.islink(os.path.join(root, d))]   # no linked folders
    if not folders:
        folders = [(root, ".conf")]
    for folder, ext in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            if len(picks) >= MAX_FILES:
                return picks
            if n.startswith(".") or (ext and not n.endswith(ext)):
                continue
            real = _resolve(os.path.join(folder, n), folder, root)
            if real:
                try:
                    picks.append((os.path.relpath(os.path.join(folder, n), root), real, os.lstat(real).st_mtime_ns))
                except OSError:
                    pass
    return picks


def read_text(path, root=None):
    """A regular file's text with comments and over-long lines dropped, or None (same rules as the NPM reader). With
    root: the file actually opened must lie inside root (a folder swapped for a link after files() checked it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        if root is not None:
            try:
                opened = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return None                              # can't tell where it really is: don't read it
            if not _inside(opened, root):
                return None
        raw = os.read(fd, MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    out = []
    for line in raw.splitlines():
        if len(line) > 4096:
            continue
        out.append(_strip_comment(line))
    return "\n".join(out)


def _strip_comment(line):
    """Drop a # comment that isn't inside quotes."""
    q, esc = None, False
    for i, ch in enumerate(line):
        if q:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return line[:i]
    return line


def blocks(txt):
    """[(name, args, body, depth)] for every { } block, found in one linear pass. Unbalanced text just ends the walk."""
    out, stack, q, start = [], [], None, 0
    i, n = 0, len(txt)
    while i < n:
        ch = txt[i]
        if q:
            if ch == "\\":
                i += 2                                   # an escaped character inside quotes
                continue
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "{":
            head = txt[start:i].strip().split()
            if len(stack) >= MAX_DEPTH:
                return out
            stack.append((head[0] if head else "", " ".join(head[1:]), i + 1))
            start = i + 1
        elif ch == "}":
            if not stack:
                return out
            name, args, b = stack.pop()
            out.append((name, args, txt[b:i], len(stack)))
            start = i + 1
        elif ch == ";":
            start = i + 1
        i += 1
    return out


def _top(body):
    """The body with nested { } blocks removed (directives that belong to this block only)."""
    out, depth, q, esc = [], 0, None, False
    for ch in body:
        if q:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "{":
            depth += 1
            continue
        elif ch == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _directive(top, name, limit=2048):
    """The value of the first `name value;` statement in a block's own text (linear: split on ';'), or None."""
    for stmt in top.split(";")[:-1]:
        words = stmt.split(None, 1)
        if len(words) == 2 and words[0] == name:
            return words[1][:limit]
    return None


def _pick_name(top):
    value = _directive(top, "server_name")
    if value is None:
        return None
    for name in value.split()[:50]:
        name = name.strip("\"'")
        if name in ("_", "localhost", "") or name.startswith(("~", "*", ".")) or "*" in name:
            continue
        if NAME_OK.match(name):
            return name
    return None


def _target(value, upstreams):
    """proxy_pass value -> scheme://host:port, or None (variables, unix sockets, other schemes)."""
    value = value.strip().strip("\"'")
    if "$" in value:
        return None
    m = re.match(r"(https?)://([A-Za-z0-9._-]{1,64})(/[^\s;]*)?\Z", value)
    if m and m.group(2) in upstreams:
        host, port = upstreams[m.group(2)]
        return f"{m.group(1)}://{host}:{port or ('443' if m.group(1) == 'https' else '80')}"
    m = TARGET.match(value)
    if not m:
        return None
    port = m.group(3) or ("443" if m.group(1) == "https" else "80")
    if not 1 <= int(port) <= 65535:
        return None
    return f"{m.group(1)}://{m.group(2)}:{int(port)}"


def parse_text(txt, has_login=None, auth_re=None):
    """Services from one file's text: [{name, upstream, auth, login}]."""
    found = blocks(txt)
    upstreams = {}
    for name, args, body, _ in found:
        if name == "upstream" and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args or ""):
            v = (_directive(_top(body), "server") or "").split()
            m = re.fullmatch(r"(" + HOST + r")(?::(\d{1,5}))?", v[0]) if v else None
            if m and (m.group(2) is None or 1 <= int(m.group(2)) <= 65535):
                upstreams[args] = (m.group(1), str(int(m.group(2))) if m.group(2) else None)
    out, seen = [], set()
    force_ssl = bool(re.search(r"return\s+30[18]\s+https://", txt))      # once per file
    servers = [(body, depth) for name, _, body, depth in found if name == "server" and depth <= 1]   # top level or in http {}
    for body, _ in servers[:MAX_BLOCKS]:
        top = _top(body)
        name = _pick_name(top)
        if not name or name in seen:
            continue
        locs = [(args, b) for n, args, b, d in blocks(body) if n == "location" and d == 0][:50]   # this server's own
        root_loc = [b for args, b in locs if args.split()[-1:] == ["/"]]
        cands = root_loc or [b for _, b in locs] or [top]
        upstream = None
        for b in cands:
            v = _directive(_top(b), "proxy_pass")
            if v:
                upstream = _target(v.split()[0], upstreams)
                if upstream:
                    break
        if not upstream:
            continue                                     # a redirect-only or static block: not a service
        seen.add(name)
        out.append({"name": name, "upstream": upstream,
                    "auth": bool(auth_re.search(body)) if auth_re else False,
                    "login": bool(has_login(body)) if has_login else False,
                    "caches": False, "force_ssl": force_ssl, "plain": True})
    return out


def signature(root):
    return tuple((n, m) for n, _, m in files(root))


def read_hosts(root, has_login=None, auth_re=None, picked=None):
    """Every service in the plain Nginx configs under root (first file wins a name). picked = a files(root) result to
    reuse (the same scan the caller's signature came from)."""
    out, seen, count = [], set(), 0
    for _, real, _ in (files(root) if picked is None else picked):
        txt = read_text(real, root)
        if txt is None:
            continue
        for h in parse_text(txt, has_login, auth_re):
            if h["name"] in seen:
                continue
            seen.add(h["name"])
            out.append(h)
            count += 1
            if count >= MAX_BLOCKS:
                return out
    return out
