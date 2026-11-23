#!/usr/bin/env python3
r"""Set dash_uid in a collector.toml: python3 set_dash_uid.py PATH UID.

UID is ASCII digits, no sign, no leading zero unless exactly "0", and
1..2147483646 (0 refused: the dashboard must not run as root). A bad UID
prints one stderr line and exits 2 touching nothing. PATH is read as text
(missing = empty); the first line matching ^\s*#?\s*dash_uid\s*= becomes
exactly "dash_uid = UID" with all other lines byte-for-byte unchanged, else
it is appended. The original is backed up to PATH + ".bak" (same mode) then
written atomically: same-folder temp (O_CREAT|O_EXCL|O_NOFOLLOW), fsync,
os.replace. A symlinked PATH is refused (exit 1). set_dash_uid(path,
uid_text) raises ValueError on a bad uid and returns the new text.
"""
import os, re, secrets, shutil, stat, sys

_RE = re.compile(r"^\s*#?\s*dash_uid\s*=")
_MAX = 2147483646

def _check_uid(uid_text):
    text = uid_text if isinstance(uid_text, str) else str(uid_text)
    if not (text.isascii() and text.isdigit()) or (len(text) > 1 and text.startswith("0")):
        raise ValueError(f"bad uid {text!r}: want ASCII digits, no sign or leading zero")
    if not 1 <= int(text) <= _MAX:
        raise ValueError(f"bad uid {text!r}: must be 1..{_MAX} (0 refused)")
    return int(text)

def set_dash_uid(path, uid_text):
    """Validate uid_text, rewrite PATH's dash_uid line and return the new text."""
    uid = _check_uid(uid_text)
    if os.path.islink(path):
        raise OSError(f"refusing to edit symlink: {path}")
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            text = f.read()
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        text, mode = "", 0o644
    line, lines = f"dash_uid = {uid}", text.splitlines(keepends=True)
    hit = next((i for i, old in enumerate(lines) if _RE.match(old)), None)
    if hit is None:
        sep = "\n" if text and not text.endswith(("\n", "\r")) else ""
        new_text = f"{text}{sep}{line}\n"
    else:
        old = lines[hit]
        lines[hit] = line + old[len(old.rstrip("\r\n")):]
        new_text = "".join(lines)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    folder = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return new_text

def main(argv):
    if len(argv) != 2:
        print("usage: set_dash_uid.py PATH UID", file=sys.stderr); return 2
    try:
        set_dash_uid(argv[0], argv[1])
    except ValueError as exc:
        print(exc, file=sys.stderr); return 2
    except OSError as exc:
        print(exc, file=sys.stderr); return 1
    print(f"dash_uid set to {_check_uid(argv[1])} in {argv[0]}")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
