"""Fishbowl's settings for the shared sign-in (signin-core, app/signin_core/): its names, public paths and data folder.
app.py and the auth.py command line both build their Config here, so they always agree."""
import settings as cfgstore
from signin_core import web

# Reached without a session: the sign-in page and what it needs, the health check (and /static/ and every /js/ app
# script: see Handler.public_extra in app.py)
PUBLIC_GET = frozenset({"/login", "/healthz", "/favicon.ico", "/style.css", "/themes.css", "/fonts.css", "/api/auth/state"})
PUBLIC_POST = frozenset({"/api/login", "/api/login/code", "/api/setup-account", "/api/logout"})
PAGE_PATHS = frozenset({"/", "/settings"})       # app.py's PAGES (checked there)
CLI = "docker exec -it <container> python /app/auth.py"


def config(auth_mode):
    return web.Config(
        auth=auth_mode, app="Fishbowl", cookie="fishbowl", error_key="error", settings_label="Settings",
        service_noun="the dashboard", open_noun="the dashboard", signin_first="Sign in first.",
        banned_words=("fishbowl", "homelab", "dashboard"), cli_hint=CLI, reset_keeps="settings and keys stay",
        setup_code_hint="Use the newest one in the container's log: docker logs <container> 2>&1 | grep '^SETUP CODE' | "
                        "tail -n 1 (or the one auth.py reset or setup-code printed).",
        setup_code_where="the newest in the container's log, or the one reset printed",
        run_as_hint="Run it as the container's user (docker exec without -u root): files made by root would lock the "
                    "dashboard out.",
        data_dir=cfgstore.DATA_DIR, public_get=PUBLIC_GET, public_post=PUBLIC_POST, pages=PAGE_PATHS)
