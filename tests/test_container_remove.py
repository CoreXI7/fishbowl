"""Remove button for leftover containers. Two gates: the dash only hands over an id the collector
listed as removable; the root collector looks that id up again and runs `docker rm <id>` (never -f / -v) only if it is
still a leftover. These tests try to make either gate remove something else."""
import json
import os
import threading
import unittest

from helpers import load_app, serve
from test_auth import call
from test_collector_requests import load_collector

ID = "e" * 64


class CollectorGate(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector()
        self.col.have = lambda cmd: True
        self.calls = []
        self.inspect = "created\tTrue\tapp\t/app_xhdl_run_a31773e56a19"
        def out(cmd, timeout=20, ok=(0,)):
            self.calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                if self.inspect is None:
                    raise self.col.CheckFailed("no such container")
                return self.inspect + "\n"
            return ""
        self.col.out = out

    def ask(self, text):
        with open(self.col.REMOVE_REQ, "w") as f:
            f.write(text)
        return self.col.handle_container_remove()

    def rm_calls(self):
        return [c for c in self.calls if c[:2] == ["docker", "rm"]]

    def test_a_leftover_is_removed_by_id_only(self):
        self.assertTrue(self.ask(ID))
        self.assertEqual(self.rm_calls(), [["docker", "rm", ID]])
        self.assertTrue(self.col.LAST_REMOVE[0]["ok"])
        self.assertFalse(os.path.lexists(self.col.REMOVE_REQ))

    def test_a_real_stopped_service_is_refused(self):
        self.inspect = "exited\tFalse\tcontainers\t/sonarr"
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])
        self.assertFalse(self.col.LAST_REMOVE[0]["ok"])

    def test_a_running_container_is_refused(self):
        self.inspect = "running\tTrue\tapp\t/app_x_run_1"
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])

    def test_plex_is_refused_whatever_its_labels(self):
        self.inspect = "created\tTrue\tcontainers\t/plex"
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])

    def test_not_compose_made_is_refused(self):
        self.inspect = "created\t<no value>\t<no value>\t/by-hand"
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])

    def test_names_options_and_junk_never_reach_docker(self):
        for bad in ("plex", "-f " + ID, ID + " -v", ID.upper(), ID[:12], "../" + ID, ID + "\n" + ID, ""):
            self.calls.clear()
            self.ask(bad)
            self.assertEqual(self.calls, [], repr(bad))

    def test_a_real_service_that_failed_to_start_is_refused(self):
        self.inspect = "created\tFalse\tcontainers\t/sonarr"
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])

    def test_a_directory_at_the_request_path_is_not_handled_every_round(self):
        os.mkdir(self.col.REMOVE_REQ)
        self.assertFalse(self.col.handle_container_remove())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.col.LAST_REMOVE[0])

    def test_an_empty_request_reaches_nothing(self):
        self.ask("")
        self.assertEqual(self.calls, [])

    def test_gone_container(self):
        self.inspect = None
        self.ask(ID)
        self.assertEqual(self.rm_calls(), [])
        self.assertFalse(self.col.LAST_REMOVE[0]["ok"])

    def test_a_symlinked_request_is_not_followed(self):
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write(ID)
        os.symlink(target, self.col.REMOVE_REQ)
        self.col.handle_container_remove()
        self.assertEqual(self.calls, [])
        self.assertTrue(os.path.exists(target), "root must not delete through the link")

    def test_a_fifo_does_not_block(self):
        os.mkfifo(self.col.REMOVE_REQ)
        done = threading.Event()
        threading.Thread(target=lambda: (self.col.handle_container_remove(), done.set()), daemon=True).start()
        if not done.wait(3):
            fd = os.open(self.col.REMOVE_REQ, os.O_WRONLY | os.O_NONBLOCK); os.close(fd)
            self.fail("the collector waited on a FIFO")
        self.assertEqual(self.calls, [])

    def test_an_oversized_request_is_ignored(self):
        self.ask(ID + "0" * 200)
        self.assertEqual(self.calls, [])

    def test_no_request_does_nothing(self):
        self.assertFalse(self.col.handle_container_remove())
        self.assertEqual(self.calls, [])


class CollectorRows(unittest.TestCase):
    def test_control_characters_in_labels_are_never_junk(self):
        col, _ = load_collector()
        self.assertTrue(col.leftover("x_run_1", "exited", "True", "app"))
        self.assertFalse(col.leftover("x_run_1", "exited", "True", "app\nfake"))
        self.assertFalse(col.leftover("x\trun", "exited", "True", "app"))


class DashGate(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.srv, self.url = serve(self.app.Handler)
        self.req = os.path.join(self.app.REQ_DIR, "container-remove")

    def tearDown(self):
        self.srv.shutdown()

    def hw(self, stopped):
        with open(self.app.HW_FILE, "w") as f:
            json.dump({"containers": {"running": 1, "total": 2, "stopped": stopped}}, f)

    def post(self, body, **kw):
        return call(self.url + "/api/containers/remove", "POST", body, **kw)[0]

    def test_a_listed_leftover_is_handed_to_root(self):
        self.hw([{"name": "x_run_1", "state": "exited", "id": ID, "removable": True}])
        self.assertEqual(self.post({"id": ID}), 200)
        with open(self.req) as f:
            self.assertEqual(f.read(), ID)

    def test_not_listed_or_not_removable_is_refused(self):
        self.hw([{"name": "sonarr", "state": "exited", "id": ID, "removable": False}])
        self.assertEqual(self.post({"id": ID}), 409)
        self.hw([{"name": "x", "state": "exited", "id": "f" * 64, "removable": True}])
        self.assertEqual(self.post({"id": ID}), 409)
        self.assertFalse(os.path.exists(self.req))

    def test_the_request_is_written_whole_and_nothing_else_is_left(self):
        self.hw([{"name": "x_run_1", "state": "exited", "id": ID, "removable": True}])
        self.assertEqual(self.post({"id": ID}), 200)
        self.assertEqual(os.listdir(self.app.REQ_DIR), ["container-remove"])

    def test_a_second_request_waits_for_the_first(self):
        other = "f" * 64
        self.hw([{"name": "x_run_1", "state": "exited", "id": ID, "removable": True},
                 {"name": "y_run_2", "state": "exited", "id": other, "removable": True}])
        self.assertEqual(self.post({"id": ID}), 200)
        self.assertEqual(self.post({"id": other}), 409)
        with open(self.req) as f:
            self.assertEqual(f.read(), ID, "the first request must not be replaced")

    def test_a_non_object_body_is_refused(self):
        self.hw([{"name": "x_run_1", "state": "exited", "id": ID, "removable": True}])
        for body in ([ID], ID, 5):
            self.assertIn(self.post(body), (400, 409), body)
        self.assertFalse(os.path.exists(self.req))

    def test_bad_ids_are_refused(self):
        self.hw([{"name": "x", "state": "exited", "id": ID, "removable": "yes"}])
        for body in ({"id": "plex"}, {"id": ID + " -f"}, {"id": ["x"]}, {"id": ID}, {}, {"id": None}):
            self.assertIn(self.post(body), (400, 409), body)
        self.assertFalse(os.path.exists(self.req))

    def test_a_malformed_id_is_refused_even_if_listed(self):
        # hw.json is the collector's, but the dash checks the shape itself: nothing but 64 hex ever reaches the request file
        self.hw([{"name": "plex", "state": "exited", "id": "plex", "removable": True},
                 {"name": "x", "state": "exited", "id": ID + " -f", "removable": True}])
        for bad in ("plex", ID + " -f"):
            self.assertEqual(self.post({"id": bad}), 400, bad)
        self.assertFalse(os.path.exists(self.req))

    def test_cross_site_and_form_posts_are_refused(self):
        self.hw([{"name": "x_run_1", "state": "exited", "id": ID, "removable": True}])
        self.assertEqual(self.post({"id": ID}, origin="https://evil.example"), 403)
        self.assertEqual(self.post({"id": ID}, headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.post({"id": ID}, headers={"Content-Type": "application/x-www-form-urlencoded"}), 403)
        self.assertFalse(os.path.exists(self.req))

    def test_the_demo_never_writes(self):
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        try:
            st, _, body = call(url + "/api/containers/remove", "POST", {"id": ID})
            self.assertEqual(st, 200)
            self.assertIn(b'"demo": true', body if isinstance(body, bytes) else json.dumps(body).encode())
            self.assertFalse(os.path.exists(os.path.join(app.REQ_DIR, "container-remove")))
        finally:
            srv.shutdown()


class SignIn(unittest.TestCase):
    def test_login_mode_refuses_without_a_session(self):
        app = load_app(AUTH="login")
        srv, url = serve(app.Handler)
        try:
            self.assertIn(call(url + "/api/containers/remove", "POST", {"id": ID})[0], (401, 403))
            self.assertFalse(os.path.exists(os.path.join(app.REQ_DIR, "container-remove")))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
