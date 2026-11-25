import ipaddress
import re

MAX_SESSIONS = 100
ART_ID = re.compile(r"[0-9a-f]{32}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def _text(value):
    return _CONTROL.sub("", value)[:200] if isinstance(value, str) else ""

def _isint(value):
    return isinstance(value, int) and not isinstance(value, bool)

def _bounded(value, low, high):
    return value if _isint(value) and low <= value <= high else 0

def _dict(value):
    return value if isinstance(value, dict) else {}

def _media_type(item):
    kind = item.get("Type")
    known = {"Episode": "episode", "Movie": "movie", "Audio": "track"}
    if isinstance(kind, str) and kind in known:
        return known[kind]
    if isinstance(kind, str):
        return re.sub(r"[^a-z]", "", kind.lower())[:20] or "other"
    return "other"

def _resolution(height):
    if not _isint(height):
        return ""
    for limit, name in ((2000, "4k"), (1000, "1080"), (700, "720"), (576, "576"), (470, "480")):
        if height >= limit:
            return name
    return "sd"

def _video(item):
    streams = item.get("MediaStreams")
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("Type") == "Video":
                return stream
    return {}

def _location(value):
    if not isinstance(value, str):
        return "wan"
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return "wan"
        value = value[1:end]
    elif value.count(":") == 1:
        value = value.rsplit(":", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "wan"
    return "lan" if address.is_private or address.is_loopback or address.is_link_local else "wan"

def art_id(value):
    if not isinstance(value, str):
        return None
    cleaned = value.lower().replace("-", "")
    return cleaned if ART_ID.fullmatch(cleaned) else None

def _session(session):
    item = _dict(session.get("NowPlayingItem"))
    play = _dict(session.get("PlayState"))
    info = _dict(session.get("TranscodingInfo"))
    kind = _media_type(item)
    title = _text(item.get("Name"))
    if kind == "episode":
        gp = _text(item.get("SeriesName"))
    elif kind == "track":
        gp = ""
        artists = item.get("Artists")
        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, str):
                    gp = _text(artist)
                    break
        if not gp:
            gp = _text(item.get("AlbumArtist"))
    else:
        gp = ""
    full = "%s - %s" % (gp, title) if kind == "episode" else title
    year = item.get("ProductionYear")
    year = str(year) if _isint(year) and 1800 <= year <= 2200 else ""
    method = play.get("PlayMethod")
    if method == "Transcode":
        td = "transcode"
    elif method == "DirectStream":
        td = "copy"
    else:
        td = "direct play"
    trans = td == "transcode"
    if trans:
        vd = "copy" if info.get("IsVideoDirect") is True else "transcode"
        ad = "copy" if info.get("IsAudioDirect") is True else "transcode"
    else:
        vd = ad = "direct play"
    video = _video(item)
    svfr = _resolution(info.get("Height") if trans else video.get("Height"))
    vfr = _resolution(video.get("Height"))
    rt = item.get("RunTimeTicks")
    rt = rt if _isint(rt) and rt >= 0 else 0
    pt = play.get("PositionTicks")
    pt = pt if _isint(pt) and pt >= 0 else 0
    progress = min(100, (pt * 100) // rt) if rt else 0
    duration = min(rt // 10000, 10 ** 10)
    view_offset = min(pt // 10000, 10 ** 10)
    if trans:
        br = info.get("Bitrate")
        bandwidth = min(br // 1000, 10 ** 7) if _isint(br) and br >= 0 else 0
    else:
        bandwidth = 0
    hw = 0
    if trans:
        accel = info.get("HardwareAccelerationType")
        if isinstance(accel, str) and accel.strip() and accel.strip().lower() != "none":
            hw = 1
    if kind == "episode":
        raw = item.get("SeriesId")
    elif kind == "track" and item.get("AlbumId") is not None:
        raw = item.get("AlbumId")
    else:
        raw = item.get("Id")
    aid = art_id(raw)
    art = "/api/jfart?id=%s" % aid if aid else None
    return {
        "session_key": _text(session.get("Id")), "friendly_name": _text(session.get("UserName")), "media_type": kind, "title": title,
        "grandparent_title": gp, "year": year, "parent_media_index": _bounded(item.get("ParentIndexNumber"), 0, 100000), "media_index": _bounded(item.get("IndexNumber"), 0, 100000),
        "full_title": full, "state": "paused" if play.get("IsPaused") is True else "playing", "transcode_decision": td, "video_decision": vd,
        "audio_decision": ad, "stream_video_full_resolution": svfr, "video_full_resolution": vfr, "product": _text(session.get("Client")),
        "player": _text(session.get("DeviceName")), "platform": _text(session.get("Client")), "location": _location(session.get("RemoteEndPoint")), "progress_percent": progress,
        "duration": duration, "view_offset": view_offset, "bandwidth": bandwidth, "transcode_hw_encoding": hw,
        "transcode_hw_decoding": hw, "art": art, "server": "jellyfin",
    }

def sessions_view(data):
    sessions = []
    if isinstance(data, list):
        for session in data[:500]:
            if not isinstance(session, dict) or not isinstance(session.get("NowPlayingItem"), dict):
                continue
            if len(sessions) >= MAX_SESSIONS:
                break
            sessions.append(_session(session))
    return {
        "stream_count": len(sessions),
        "transcodes": sum(1 for s in sessions if s["transcode_decision"] == "transcode"),
        "bandwidth": sum(s["bandwidth"] for s in sessions),
        "sessions": sessions,
    }
