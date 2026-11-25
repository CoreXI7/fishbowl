import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
import jellyfin


def _ep():
    return {
        "Id": "sess-1", "UserName": "Alice",
        "NowPlayingItem": {
            "Type": "Episode", "Name": "Pilot", "SeriesName": "My Show",
            "SeriesId": "0123456789abcdef0123456789abcdef",
            "ProductionYear": 2019, "ParentIndexNumber": 1, "IndexNumber": 2,
            "RunTimeTicks": 36_000_000_000,
            "MediaStreams": [{"Type": "Video", "Height": 1080}],
        },
        "PlayState": {"IsPaused": False, "PlayMethod": "Transcode", "PositionTicks": 18_000_000_000},
        "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": False,
                            "Height": 2160, "Bitrate": 5_000_000,
                            "HardwareAccelerationType": "vaapi"},
        "Client": "Jellyfin Web", "DeviceName": "Living Room", "RemoteEndPoint": "172.20.0.5",
    }


def _movie(endpoint="8.8.8.8", session_id="movie-1"):
    return {
        "Id": session_id, "UserName": "Bob",
        "NowPlayingItem": {"Type": "Movie", "Name": "Inception",
                           "Id": "deadbeefdeadbeefdeadbeefdeadbeef",
                           "ProductionYear": 2010, "RunTimeTicks": 88_800_000_000,
                           "MediaStreams": [{"Type": "Video", "Height": 720}]},
        "PlayState": {"IsPaused": False, "PlayMethod": "DirectPlay", "PositionTicks": 0},
        "Client": "Android", "DeviceName": "Phone", "RemoteEndPoint": endpoint,
    }


def _track():
    return {
        "Id": "track-1", "UserName": "Carol",
        "NowPlayingItem": {
            "Type": "Audio", "Name": "Song", "Artists": ["Artist A", "Artist B"],
            "AlbumArtist": "Band", "AlbumId": "abcdefabcdefabcdefabcdefabcdef12",
            "RunTimeTicks": 2_000_000_000, "ParentIndexNumber": 1, "IndexNumber": 3,
            "MediaStreams": [{"Type": "Audio"}],
        },
        "PlayState": {"IsPaused": True, "PlayMethod": "DirectStream", "PositionTicks": 500_000_000},
        "RemoteEndPoint": "10.0.0.5",
    }


class TestJellyfin(unittest.TestCase):
    def check(self, session, **expected):
        for key, value in expected.items():
            self.assertEqual(session[key], value, key)

    def test_episode_transcoding_hardware_accel(self):
        view = jellyfin.sessions_view([_ep()])
        self.assertEqual((view["stream_count"], view["transcodes"], view["bandwidth"]), (1, 1, 5_000))
        self.check(
            view["sessions"][0],
            session_key="sess-1", friendly_name="Alice", media_type="episode",
            title="Pilot", grandparent_title="My Show", year="2019",
            parent_media_index=1, media_index=2, full_title="My Show - Pilot",
            state="playing", transcode_decision="transcode",
            video_decision="transcode", audio_decision="transcode",
            stream_video_full_resolution="4k", video_full_resolution="1080",
            product="Jellyfin Web", player="Living Room", platform="Jellyfin Web",
            location="lan", progress_percent=50, duration=3_600_000,
            view_offset=1_800_000, bandwidth=5_000,
            transcode_hw_encoding=1, transcode_hw_decoding=1,
            art="/api/jfart?id=0123456789abcdef0123456789abcdef", server="jellyfin",
        )

    def test_movie_direct_play(self):
        view = jellyfin.sessions_view([_movie()])
        self.assertEqual((view["transcodes"], view["bandwidth"]), (0, 0))
        self.check(
            view["sessions"][0],
            media_type="movie", grandparent_title="", full_title="Inception",
            year="2010", state="playing", transcode_decision="direct play",
            video_decision="direct play", audio_decision="direct play",
            stream_video_full_resolution="720", video_full_resolution="720",
            location="wan", progress_percent=0, duration=8_880_000,
            view_offset=0, art="/api/jfart?id=deadbeefdeadbeefdeadbeefdeadbeef",
        )

    def test_paused_track(self):
        view = jellyfin.sessions_view([_track()])
        self.assertEqual(view["transcodes"], 0)
        self.check(
            view["sessions"][0],
            media_type="track", grandparent_title="Artist A", full_title="Song",
            state="paused", transcode_decision="copy", video_decision="direct play",
            audio_decision="direct play", stream_video_full_resolution="",
            video_full_resolution="", progress_percent=25, duration=200_000,
            view_offset=50_000, bandwidth=0,
            art="/api/jfart?id=abcdefabcdefabcdefabcdefabcdef12",
        )

    def test_skips_sessions_without_now_playing_item(self):
        data = [{"Id": "no-item"}, {"Id": "none", "NowPlayingItem": None},
                {"Id": "string", "NowPlayingItem": "bad"}, None, "not a dict", _movie()]
        view = jellyfin.sessions_view(data)
        self.assertEqual(view["stream_count"], 1)
        self.assertEqual(view["sessions"][0]["session_key"], "movie-1")

    def test_non_list_input(self):
        empty = {"stream_count": 0, "transcodes": 0, "bandwidth": 0, "sessions": []}
        for data in (None, {}, "abc", 42, True):
            with self.subTest(data=data):
                self.assertEqual(jellyfin.sessions_view(data), empty)

    def test_hostile_values(self):
        hostile = {
            "Id": "h" * 500, "UserName": {"x": "y"},
            "NowPlayingItem": {"Type": "Widget-X", "Name": "N" * 500,
                               "SeriesName": ["x"], "ProductionYear": True,
                               "ParentIndexNumber": True, "IndexNumber": -5,
                               "RunTimeTicks": 10_000_000,
                               "MediaStreams": [{"Type": "Video", "Height": True}]},
            "PlayState": {"IsPaused": "yes", "PlayMethod": "Transcode", "PositionTicks": -100},
            "TranscodingInfo": {"IsVideoDirect": "yes", "IsAudioDirect": True,
                                "Height": 500, "Bitrate": 10 ** 30,
                                "HardwareAccelerationType": "none"},
            "Client": 123, "DeviceName": "Dev\x00ice\nX", "RemoteEndPoint": "garbage",
        }
        bool_bitrate = {
            "Id": "bool-bitrate",
            "NowPlayingItem": {"Type": "Movie", "Name": "M",
                               "Id": "deadbeefdeadbeefdeadbeefdeadbeef"},
            "PlayState": {"PlayMethod": "Transcode"},
            "TranscodingInfo": {"Bitrate": True, "HardwareAccelerationType": ""},
        }
        view = jellyfin.sessions_view([hostile, bool_bitrate])
        self.assertEqual((view["stream_count"], view["transcodes"], view["bandwidth"]), (2, 2, 10_000_000))
        self.check(
            view["sessions"][0],
            session_key="h" * 200, friendly_name="", media_type="widgetx",
            title="N" * 200, grandparent_title="", year="",
            parent_media_index=0, media_index=0, full_title="N" * 200,
            state="playing", transcode_decision="transcode",
            video_decision="transcode", audio_decision="copy",
            stream_video_full_resolution="480", video_full_resolution="",
            product="", player="DeviceX", platform="", location="wan",
            progress_percent=0, duration=1_000, view_offset=0,
            bandwidth=10_000_000, transcode_hw_encoding=0,
            transcode_hw_decoding=0, art=None,
        )
        self.assertEqual(view["sessions"][1]["bandwidth"], 0)

    def test_remote_endpoint_locations(self):
        cases = [("172.20.0.5", "lan"), ("10.0.0.5:8096", "lan"),
                 ("[fe80::1]:8096", "lan"), ("8.8.8.8", "wan"), ("garbage", "wan")]
        for endpoint, expected in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    jellyfin.sessions_view([_movie(endpoint)])["sessions"][0]["location"],
                    expected,
                )
        missing = _movie()
        del missing["RemoteEndPoint"]
        self.assertEqual(
            jellyfin.sessions_view([missing])["sessions"][0]["location"], "wan"
        )

    def test_art_ids(self):
        episode = _ep()
        episode["NowPlayingItem"]["SeriesId"] = "ABCDEF01-2345-6789-ABCD-EF0123456789"
        bad = _movie()
        bad["NowPlayingItem"]["Id"] = "../x"
        view = jellyfin.sessions_view([episode, bad])
        self.assertEqual(view["sessions"][0]["art"],
                         "/api/jfart?id=abcdef0123456789abcdef0123456789")
        self.assertIsNone(view["sessions"][1]["art"])
        self.assertEqual(jellyfin.art_id("ABCDEF01-2345-6789-ABCD-EF0123456789"),
                         "abcdef0123456789abcdef0123456789")
        self.assertIsNone(jellyfin.art_id("../x"))

    def test_max_sessions_cap(self):
        view = jellyfin.sessions_view([_movie(session_id="s%d" % i) for i in range(150)])
        self.assertEqual((view["stream_count"], len(view["sessions"])), (100, 100))
        self.assertEqual(view["sessions"][0]["session_key"], "s0")
        self.assertEqual(view["sessions"][-1]["session_key"], "s99")

    def test_exact_key_set(self):
        view = jellyfin.sessions_view([_movie()])
        self.assertEqual(set(view), {"stream_count", "transcodes", "bandwidth", "sessions"})
        self.assertEqual(set(view["sessions"][0]), {
            "session_key", "friendly_name", "media_type", "title",
            "grandparent_title", "year", "parent_media_index", "media_index",
            "full_title", "state", "transcode_decision", "video_decision",
            "audio_decision", "stream_video_full_resolution",
            "video_full_resolution", "product", "player", "platform",
            "location", "progress_percent", "duration", "view_offset",
            "bandwidth", "transcode_hw_encoding", "transcode_hw_decoding",
            "art", "server",
        })


if __name__ == "__main__":
    unittest.main()
