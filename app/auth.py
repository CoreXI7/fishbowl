"""Fishbowl's auth.py: the shared signin-core account layer (app/signin_core/auth.py), configured for Fishbowl.
Kept at this path so the documented host commands work:
  docker exec -it <container> python /app/auth.py status | unlock | setup-code | signout | reset [--yes]
Imported as `auth`, it IS signin_core.auth."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from signin_core import verify as _verify   # noqa: E402

_ok, _problems = _verify.check(HERE)
if not _ok:
    raise SystemExit("signin_core does not match signin_core.pin: " + "; ".join(_problems))
from signin_core import auth as _core       # noqa: E402

if __name__ == "__main__":
    import signin_conf
    sys.exit(_core.cli(signin_conf.config("login"), sys.argv[1:]))
sys.modules[__name__] = _core
