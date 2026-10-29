"""The torrent list comes from qBittorrent's incremental sync feed: one full list, then only what changed.
The merged result must equal what a full list would show."""
import unittest

from helpers import Upstream, load_app


def t(name, **kw):
    return {"name": name, "state": "uploading", "progress": 1, "dlspeed": 0, "upspeed": 0, "eta": 0, "size": 100,
            "num_seeds": 1, "num_leechs": 0, "ratio": 1.5, "category": "tv", "added_on": 1, "uploaded": 10,
            "magnet_uri": "magnet:?xt=urn:btih:" + "0" * 40, "trackers_count": 3, **kw}


class QbitSync(unittest.TestCase):
    def setUp(self):
        self.answers = []
        self.up = Upstream({"/api/v2/auth/login": (200, {}, "Ok."),
                            "/api/v2/transfer/info": (200, {}, {"connection_status": "connected"}),
                            "/api/v2/sync/maindata": lambda h: self.answers.pop(0)})
        self.app = load_app(QBIT_URL=self.up.url, QBIT_USER="u", QBIT_PASS="p")

    def tearDown(self):
        self.up.close()

    def sync_rids(self):
        return [h[1].split("rid=")[1] for h in self.up.hits if h[1].startswith("/api/v2/sync/maindata")]

    def test_incremental_updates_merge(self):
        self.answers = [
            (200, {}, {"rid": 7, "full_update": True, "torrents": {"a": t("A"), "b": t("B"), "c": t("C", state="downloading", progress=0.5)}}),
            (200, {}, {"rid": 8, "torrents": {"a": {"upspeed": 5000}, "c": {"progress": 0.75, "dlspeed": 900}}, "torrents_removed": ["b"]}),
        ]
        first = self.app.qbit()
        self.assertEqual((first["total"], first["seeding_active"]), (3, 0))
        second = self.app.qbit()
        self.assertEqual(self.sync_rids(), ["0", "7"])
        self.assertEqual(second["total"], 2)
        self.assertEqual([x["name"] for x in second["seeding"]], ["A"])
        self.assertEqual(second["seeding"][0]["upspeed"], 5000)
        self.assertEqual((second["downloading"][0]["progress"], second["downloading"][0]["dlspeed"]), (0.75, 900))

    def test_full_update_replaces_everything(self):
        self.answers = [
            (200, {}, {"rid": 1, "full_update": True, "torrents": {"a": t("A"), "b": t("B")}}),
            (200, {}, {"rid": 1, "full_update": True, "torrents": {"z": t("Z")}}),   # e.g. qBittorrent restarted
        ]
        self.app.qbit()
        self.assertEqual(self.app.qbit()["total"], 1)

    def test_keeps_only_the_fields_it_shows(self):
        self.answers = [(200, {}, {"rid": 1, "full_update": True, "torrents": {"a": t("A")}})]
        self.app.qbit()
        stored = next(iter(self.app.qb.state.values()))
        self.assertNotIn("magnet_uri", stored)


if __name__ == "__main__":
    unittest.main()
