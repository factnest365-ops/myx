"""Comprehensive tests for Pillars 1, 2, and 3:
- Pillar 1: Audiophile DSP, Parametric EQ, Normalization, Crossfade, SponsorBlock, Synced Mel Visualizer
- Pillar 2: SQLite Persistent Library, Liked Songs, Playlists & M3U/YT Import, History, Offline Cache LRU
- Pillar 3: Chameleon Album-Art Dynamic Theme, SGR 1006 Mouse Scrub/Clicks/Wheel, YouTube Chapters
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from myx import cli, dsp, mcp, picker, player, sources, tui
from myx.library import Library


# ---------------------------------------------------------------------------
# Pillar 1: Audiophile DSP, Crossfade, SponsorBlock, Synced Mel Visualizer
# ---------------------------------------------------------------------------


def test_dsp_presets_and_5band_parametric_eq():
    cfg = dsp.DSPConfig()
    assert cfg.preset == "flat"
    assert dsp.build_mpv_af(cfg) == ""

    assert cfg.set_preset("bass_boost") is True
    af_bass = dsp.build_mpv_af(cfg)
    assert af_bass.startswith("lavfi=[")
    assert "bass=g=6.0:f=95" in af_bass

    # Slowed + Reverb alters speed and disables pitch correction (tape pitch drop)
    assert cfg.set_preset("slowed_reverb") is True
    cmds_slow = dsp.mpv_dsp_commands(cfg)
    assert ["set_property", "audio-pitch-correction", "no"] in cmds_slow
    assert ["set_property", "speed", 0.86] in cmds_slow
    assert "aecho=" in dsp.build_mpv_af(cfg)

    # Nightcore speeds up tempo + pitch
    assert cfg.set_preset("nightcore") is True
    cmds_nc = dsp.mpv_dsp_commands(cfg)
    assert ["set_property", "speed", 1.2] in cmds_nc

    # Custom 5-band parametric EQ clamps to [-12, +12] dB
    cfg.set_custom_bands([18.0, 3.5, 0.0, -4.0, -20.0])
    assert cfg.preset == "custom"
    assert cfg.custom_bands == [12.0, 3.5, 0.0, -4.0, -12.0]
    af_custom = dsp.build_mpv_af(cfg)
    assert "equalizer=f=60:t=q:w=1.1:g=12.0" in af_custom
    assert "equalizer=f=14000:t=q:w=1.1:g=-12.0" in af_custom

    # Cycle presets wraps cleanly
    cfg.set_preset("flat")
    nxt = cfg.cycle_preset(1)
    assert nxt != "flat"
    assert cfg.cycle_preset(-1) == "flat"


def test_dsp_loudness_normalization_and_silence_trim():
    cfg = dsp.DSPConfig(normalize=True, trim_silence=True)
    af = dsp.build_mpv_af(cfg)
    assert "silenceremove=" in af
    assert "dynaudnorm=f=150:g=13:p=0.92" in af


def test_smart_crossfade_gain_envelope_and_daemon_ramp():
    assert dsp.compute_crossfade_gain(50.0, 200.0, 4.0) == 1.0
    # Ramp-in over first 40% of 5s (= 2.0s)
    assert 0.15 <= dsp.compute_crossfade_gain(0.0, 200.0, 5.0) < 0.35
    assert 0.5 < dsp.compute_crossfade_gain(1.0, 200.0, 5.0) < 0.8
    # Ramp-out over final 5s
    assert 0.4 < dsp.compute_crossfade_gain(197.5, 200.0, 5.0) < 0.75
    assert dsp.compute_crossfade_gain(199.9, 200.0, 5.0) <= 0.2

    d = player.PlayerDaemon(sponsorblock=False)
    d.current_track = {"id": "trk12345", "title": "Song", "duration": 100.0}
    vol_sets: list[int] = []
    cur_pos = [98.0]

    def fake_mpv(cmd: list[Any]) -> Any:
        if cmd == ["get_property", "time-pos"]:
            return cur_pos[0]
        if cmd == ["get_property", "duration"]:
            return 100.0
        if cmd == ["get_property", "volume"]:
            return 80
        if cmd[:2] == ["set_property", "volume"]:
            vol_sets.append(int(cmd[2]))
            return None
        return None

    d._mpv = fake_mpv  # type: ignore[method-assign]
    resp = d.handle_command({"cmd": "crossfade", "arg": 4.0})
    assert resp["ok"] is True and resp["crossfade_s"] == 4.0

    d._tick_sponsorblock_and_crossfade()
    assert vol_sets and vol_sets[-1] < 80, "crossfade tail should ramp volume down"

    # Returning to mid-track restores user volume
    cur_pos[0] = 30.0
    d._tick_sponsorblock_and_crossfade()
    assert vol_sets[-1] == 80


def test_sponsorblock_parsing_and_auto_skip_watchdog(monkeypatch):
    payload = [
        {"segment": [0.0, 14.5], "category": "music_offtopic"},
        {"segment": [180.0, 198.2], "category": "outro"},
        {"segment": [50.0, 50.3], "category": "sponsor"},  # <1s ignored
    ]

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda req, timeout=4: FakeResp())
    segs = sources.sponsorblock_segments("dQw4w9WgXcQ")
    assert len(segs) == 2
    assert segs[0] == {"start": 0.0, "end": 14.5, "category": "music_offtopic"}

    # Watchdog auto-seeks past segment once and records toast event
    d = player.PlayerDaemon(sponsorblock=True)
    d.current_track = {"id": "dQw4w9WgXcQ", "title": "Song", "duration": 210.0}
    d._sb_cache["dQw4w9WgXcQ"] = segs
    seeks: list[float] = []

    def fake_mpv(cmd: list[Any]) -> Any:
        if cmd == ["get_property", "time-pos"]:
            return 3.2
        if cmd == ["get_property", "duration"]:
            return 210.0
        if cmd and cmd[0] == "seek":
            seeks.append(float(cmd[1]))
        return None

    d._mpv = fake_mpv  # type: ignore[method-assign]
    d._tick_sponsorblock_and_crossfade()
    assert seeks == [14.5]
    assert d._last_sb_skip is not None
    assert d._last_sb_skip["category"] == "music_offtopic"

    # Immediate re-tick within 6s cooldown does not spam seek
    d._tick_sponsorblock_and_crossfade()
    assert seeks == [14.5]


def test_synced_visualizer_local_cache_first_pause_and_hann_mel_fft(tmp_path, monkeypatch):
    cached_opus = tmp_path / "abc12345.opus"
    cached_opus.write_bytes(b"OggS-fake-audio")

    seen_urls: list[tuple[Any, float]] = []

    class StubTap(tui.SpectrumSource):
        def __init__(self, url: str | None, start_pos: float = 0.0):
            super().__init__()
            self.url = url
            self.start_pos = start_pos

    def fake_make(url: str | None = None, *, start_pos: float = 0.0):
        seen_urls.append((url, start_pos))
        return StubTap(url, start_pos)

    monkeypatch.setattr(tui, "make_spectrum", fake_make)
    monkeypatch.setattr(tui, "_ORIGINAL_MAKE_SPECTRUM", fake_make)

    r = tui.Renderer(out=io.StringIO())
    r.spectrum_auto = True
    state = {
        "playing": True,
        "paused": False,
        "title": "Cached Song",
        "url": "https://cdn.example.invalid/remote-stream",
        "cached_audio": str(cached_opus),
        "position": 42.5,
        "duration": 180.0,
        "queue": [],
    }
    r.render_once(state=state, size=(100, 30))
    # Must prefer local cached_audio file over remote stream URL (0 extra network bandwidth!)
    assert seen_urls == [(str(cached_opus), 42.5)]

    # Verify Hann-windowed Mel FFT separates low-freq bass vs high-freq treble
    import os
    import types

    r_fd, w_fd = os.pipe()
    fake_proc = types.SimpleNamespace(stdout=os.fdopen(r_fd, "rb"), terminate=lambda: None, pid=None)
    tap = tui.FfmpegTapSource(proc=fake_proc, sample_rate=16000)
    t = np.arange(2048, dtype=np.float32) / 16000.0
    bass_wave = (np.sin(2.0 * math.pi * 80.0 * t) * 24000.0).astype(np.int16)
    os.write(w_fd, bass_wave.tobytes())
    bass_vals = tap.values()
    assert sum(bass_vals[:6]) > sum(bass_vals[-6:]), "80Hz tone must peak in low Mel bars"

    treble_wave = (np.sin(2.0 * math.pi * 5000.0 * t) * 24000.0).astype(np.int16)
    for _ in range(3):
        os.write(w_fd, treble_wave.tobytes())
        treble_vals = tap.values()
    os.close(w_fd)
    assert sum(treble_vals[-6:]) > sum(treble_vals[:6]), "5kHz tone must peak in high Mel bars"
    tap.close()


# ---------------------------------------------------------------------------
# Pillar 2: SQLite Persistent Library, Likes, Playlists, History, Offline Cache
# ---------------------------------------------------------------------------


def test_sqlite_library_likes_persistence_and_player_commands(tmp_path):
    db_path = tmp_path / "library.db"
    lib = Library(db_path)
    d = player.PlayerDaemon(library=lib, sponsorblock=False)
    loaded_streams: list[str] = []
    d._mpv = lambda cmd: loaded_streams.append(cmd[1]) if cmd[:1] == ["loadfile"] else []  # type: ignore[method-assign]
    d._stream_for = lambda vid: f"https://cdn.example/{vid}"  # type: ignore[method-assign]

    d.current_track = {
        "id": "weeknd01",
        "title": "After Hours",
        "artist": "The Weeknd",
        "duration": 361.0,
        "thumb": None,
    }
    res1 = d.handle_command({"cmd": "like"})
    assert res1["ok"] is True and res1["liked"] is True and res1["liked_count"] == 1
    lib.close()

    # Reopen from disk to prove persistence across sessions
    lib2 = Library(db_path)
    assert lib2.is_liked("weeknd01") is True
    liked = lib2.liked_tracks()
    assert len(liked) == 1 and liked[0]["title"] == "After Hours"

    d2 = player.PlayerDaemon(library=lib2, sponsorblock=False)
    d2._mpv = lambda cmd: loaded_streams.append(cmd[1]) if cmd[:1] == ["loadfile"] else []  # type: ignore[method-assign]
    d2._stream_for = lambda vid: f"https://cdn.example/{vid}"  # type: ignore[method-assign]
    res_play = d2.handle_command({"cmd": "play_liked"})
    assert res_play == {"ok": True, "loaded": 1}
    assert "https://cdn.example/weeknd01" in loaded_streams
    lib2.close()


def test_sqlite_playlists_crud_and_m3u_roundtrip(tmp_path):
    lib = Library(tmp_path / "playlists.db")
    tracks = [
        {"id": "vid_alpha1", "title": "Alpha Song", "artist": "Artist A", "duration": 185},
        {"id": "vid_beta02", "title": "Beta Song", "artist": "Artist B", "duration": 212},
    ]
    assert lib.replace_playlist_tracks("Late Night Coding", tracks) == 2
    pls = lib.list_playlists()
    assert len(pls) == 1 and pls[0]["name"] == "Late Night Coding" and pls[0]["track_count"] == 2

    m3u = lib.export_m3u("Late Night Coding")
    assert "#EXTM3U" in m3u
    assert "https://www.youtube.com/watch?v=vid_alpha1" in m3u

    imported = lib.import_m3u("Restored Mix", m3u)
    assert imported == 2
    restored = lib.get_playlist_tracks("Restored Mix")
    assert [t["id"] for t in restored] == ["vid_alpha1", "vid_beta02"]
    assert lib.delete_playlist("Restored Mix") is True
    lib.close()


def test_youtube_playlist_import_command(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sources,
        "playlist_items",
        lambda url, limit=100: [
            {"id": "pl_item_01", "title": "Intro", "artist": "DJ", "duration": 120, "thumb": None},
            {"id": "pl_item_02", "title": "Peak", "artist": "DJ", "duration": 240, "thumb": None},
        ],
    )
    lib = Library(tmp_path / "import.db")
    d = player.PlayerDaemon(library=lib, sponsorblock=False)
    d._mpv = lambda cmd: []  # type: ignore[method-assign]
    d._stream_for = lambda vid: f"https://cdn.example/{vid}"  # type: ignore[method-assign]

    resp = d.handle_command(
        {
            "cmd": "playlist_import",
            "url": "https://www.youtube.com/playlist?list=PL1234567890abcdef",
            "name": "YT Imported",
            "replace": True,
        }
    )
    assert resp == {"ok": True, "imported": 2, "loaded": 2, "name": "YT Imported"}
    assert d.current_track and d.current_track["id"] == "pl_item_01"
    assert [q["id"] for q in d.queue] == ["pl_item_02"]
    assert len(lib.get_playlist_tracks("YT Imported")) == 2
    lib.close()


def test_play_history_most_played_and_lyrics_offset_persistence(tmp_path):
    lib = Library(tmp_path / "hist.db")
    t1 = {"id": "song_one_1", "title": "One", "artist": "A", "duration": 180}
    t2 = {"id": "song_two_2", "title": "Two", "artist": "B", "duration": 200}
    lib.record_play(t1)
    lib.record_play(t2)
    lib.record_play(t1)

    most = lib.most_played(5)
    assert most[0]["id"] == "song_one_1" and most[0]["play_count"] == 2
    recent = lib.recent_history(5)
    assert len(recent) == 3 and recent[0]["id"] == "song_one_1"

    # Per-track lyrics offset persists and loads into LyricsState
    lib.set_lyrics_offset("song_one_1", -1.5)
    assert lib.get_lyrics_offset("song_one_1") == -1.5
    ls = tui.LyricsState()
    ls.load_from_state(
        {
            "id": "song_one_1",
            "lyrics_offset": -1.5,
            "lrc": "[00:10.00]First line\n[00:12.00]Second line",
        }
    )
    assert ls.offset == -1.5
    lib.close()


def test_offline_audio_cache_priority_and_lru_budget_eviction(tmp_path, monkeypatch):
    lib = Library(tmp_path / "cache.db")
    f1 = tmp_path / "track_old.opus"
    f2 = tmp_path / "track_liked.opus"
    f3 = tmp_path / "track_new.opus"
    f1.write_bytes(b"x" * 4000)
    f2.write_bytes(b"y" * 4000)
    f3.write_bytes(b"z" * 4000)

    lib.upsert_track({"id": "track_old", "title": "Old"})
    lib.set_liked({"id": "track_liked", "title": "Liked"}, True)
    lib.upsert_track({"id": "track_new", "title": "New"})

    lib.set_cached_audio("track_old", str(f1))
    lib.set_cached_audio("track_liked", str(f2))
    lib.set_cached_audio("track_new", str(f3))

    # _stream_for must return local cached file immediately without calling sources.stream_url
    def forbid_network(vid: str) -> str:
        raise AssertionError("sources.stream_url called despite offline cached audio")

    monkeypatch.setattr(sources, "stream_url", forbid_network)
    d = player.PlayerDaemon(library=lib, sponsorblock=False)
    assert d._stream_for("track_liked") == str(f2)

    # Evict down to 8500 bytes: unliked oldest ('track_old') must be evicted before 'track_liked'
    evicted = lib.enforce_audio_cache_budget(max_bytes=8500)
    assert "track_old" in evicted
    assert not f1.exists()
    assert f2.exists() and f3.exists()
    assert tui.FfmpegTapSource._is_local_file("/etc/passwd") is False
    lib.close()


# ---------------------------------------------------------------------------
# Pillar 3: Chameleon Theme, SGR 1006 Mouse, YouTube Chapters, MCP/CLI
# ---------------------------------------------------------------------------


def test_chameleon_album_art_palette_wcag_contrast_and_toggle(tmp_path):
    art_path = tmp_path / "crimson_cover.png"
    img = Image.new("RGB", (64, 64), (195, 28, 75))
    for y in range(32, 64):
        for x in range(32, 64):
            img.putpixel((x, y), (30, 170, 220))
    img.save(art_path)

    pal = tui.extract_art_palette(str(art_path))
    assert pal is not None
    dom_hex, sec_hex = pal
    assert dom_hex != sec_hex
    # Verify luminance contrast safety against dark #0d0d0d background
    r1, g1, b1 = tui._rgb(dom_hex)
    lum = 0.2126 * r1 + 0.7152 * g1 + 0.0722 * b1
    assert lum >= 90.0

    orig_theme_accent = tui.THEME["accent"]
    renderer = tui.Renderer(out=io.StringIO())
    frame = renderer.render_once(
        state={
            "playing": True,
            "paused": False,
            "title": "Neon Track",
            "artist": "Synthwave",
            "thumb": str(art_path),
            "duration": 200,
            "position": 50,
            "queue": [],
        },
        size=(100, 30),
    )
    assert f"\x1b[38;2;{r1};{g1};{b1}m" in frame
    assert tui.THEME["accent"] == orig_theme_accent, "global THEME must remain unmutated"

    # Ctrl-T toggles Chameleon theme off -> reverts to THEME["accent"]
    renderer.handle_key("\x14")
    assert renderer.chameleon is False
    assert renderer._accent_hex() == orig_theme_accent


def test_sgr_1006_mouse_scrub_tabs_liked_queue_and_wheel(monkeypatch):
    cmds: list[tuple[str, dict[str, Any]]] = []

    def fake_send(cmd: str, **kw: Any) -> dict[str, Any]:
        cmds.append((cmd, kw))
        if cmd == "like":
            return {"ok": True, "liked": True}
        if cmd == "play_liked":
            return {"ok": True, "loaded": 4}
        return {"ok": True}

    monkeypatch.setattr(tui, "send_command", fake_send)
    r = tui.Renderer(out=io.StringIO())
    state = {
        "playing": True,
        "paused": False,
        "title": "Starboy",
        "artist": "The Weeknd",
        "id": "starboy01",
        "liked": False,
        "liked_count": 4,
        "duration": 200.0,
        "position": 20.0,
        "queue": [
            {"id": "q1", "title": "I Feel It Coming", "artist": "The Weeknd", "duration": 269},
            {"id": "q2", "title": "Party Monster", "artist": "The Weeknd", "duration": 249},
        ],
    }
    r.render_once(state=state, size=(110, 32))

    # 1. Click middle of progress bar -> scrubs to ~100s
    assert r._progress_hitbox is not None
    bar_y, bar_x0, bar_w, _ = r._progress_hitbox
    mid_col1 = bar_x0 + bar_w // 2 + 1
    r.handle_key(f"\x1b[<0;{mid_col1};{bar_y + 1}M")
    assert cmds and cmds[-1][0] == "jump"
    assert 90.0 <= cmds[-1][1]["arg"] <= 110.0

    # 2. Click "Lyrics" tab on row 0 -> switches view to lyrics
    lyrics_tab = next(tb for tb in r._tab_hitboxes if tb[2] == "lyrics")
    r.handle_key(f"\x1b[<0;{lyrics_tab[0] + 2};1M")
    assert r.view == "lyrics"

    # Switch back to Now Playing via tab click
    now_tab = next(tb for tb in r._tab_hitboxes if tb[2] == "now")
    r.handle_key(f"\x1b[<0;{now_tab[0] + 2};1M")
    assert r.view == "now"

    # 3. Click "▸ Play Liked Songs ♥ (4)" -> sends play_liked
    assert r._liked_hitbox is not None
    ly, lx0, _ = r._liked_hitbox
    r.handle_key(f"\x1b[<0;{lx0 + 3};{ly + 1}M")
    assert cmds[-1][0] == "play_liked"

    # 4. Re-render without toast so transport footer hitboxes are visible, then click Next
    r.toast = ""
    r.render_once(state=state, size=(110, 32))
    skip_btn = next(cb for cb in r._control_hitboxes if cb[3] == "skip")
    r.handle_key(f"\x1b[<0;{skip_btn[1] + 2};{skip_btn[0] + 1}M")
    assert cmds[-1][0] == "skip"

    # 5. Scroll wheel down/up navigates queue selection
    assert r.selected == 0
    r.handle_key("\x1b[<65;10;10M")  # wheel down
    assert r.selected == 1
    r.handle_key("\x1b[<64;10;10M")  # wheel up
    assert r.selected == 0


def test_youtube_chapters_extraction_progress_ticks_and_navigation():
    shaped = sources._shape(
        {
            "id": "mix_set_01",
            "title": "Boiler Room Set",
            "uploader": "Fred again..",
            "duration": 300,
            "chapters": [
                {"title": "Warmup", "start_time": 0, "end_time": 100},
                {"title": "Delilah", "start_time": 100, "end_time": 210},
                {"title": "Billie", "start_time": 210, "end_time": 300},
            ],
        }
    )
    assert len(shaped["chapters"]) == 3

    # Renderer draws '┼' chapter ticks and active '§ Delilah' chapter badge
    r = tui.Renderer(out=io.StringIO())
    frame = r.render_once(
        state={
            "playing": True,
            "paused": False,
            "title": shaped["title"],
            "artist": shaped["artist"],
            "duration": 300,
            "position": 120,
            "chapters": shaped["chapters"],
            "chapter": "Delilah",
            "queue": [],
        },
        size=(110, 32),
    )
    assert "┼" in frame
    assert "§ Delilah" in frame

    # PlayerDaemon next_chapter / prev_chapter seeks cleanly
    d = player.PlayerDaemon(sponsorblock=False)
    d.current_track = {
        "id": "mix_set_01",
        "title": "Boiler Room Set",
        "duration": 300,
        "chapters": shaped["chapters"],
    }
    pos_box = [120.0]

    def fake_mpv(cmd: list[Any]) -> Any:
        if cmd == ["get_property", "time-pos"]:
            return pos_box[0]
        if cmd and cmd[0] == "seek":
            pos_box[0] = float(cmd[1])
        return None

    d._mpv = fake_mpv  # type: ignore[method-assign]
    nxt = d.handle_command({"cmd": "next_chapter"})
    assert nxt == {"ok": True, "chapter": "Billie", "position": 210.0}
    assert pos_box[0] == 210.0

    # At 210.0s (<=3s into Billie), prev_chapter jumps back to Delilah (100.0s)
    prv = d.handle_command({"cmd": "prev_chapter"})
    assert prv == {"ok": True, "chapter": "Delilah", "position": 100.0}


def test_mcp_and_cli_pillars_1_2_3_tools(monkeypatch):
    names = {t["name"] for t in mcp.TOOLS}
    for expected in (
        "myx_eq",
        "myx_dsp",
        "myx_like",
        "myx_playlist",
        "myx_chapter",
        "myx_history",
        "myx_cache_track",
    ):
        assert expected in names

    dispatched: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        mcp,
        "_send_cmd",
        lambda cmd, **kw: dispatched.append((cmd, kw)) or {"ok": True, "cmd": cmd},
    )

    r_eq = mcp.handle_tool("myx_eq", {"preset": "bass_boost"})
    assert r_eq["isError"] is False and dispatched[-1] == ("eq", {"arg": "bass_boost"})

    r_dsp = mcp.handle_tool("myx_dsp", {"normalize": True, "crossfade_s": 4.0})
    assert r_dsp["isError"] is False

    r_like = mcp.handle_tool("myx_like", {"action": "play"})
    assert r_like["isError"] is False and dispatched[-1] == ("play_liked", {})

    r_pl = mcp.handle_tool("myx_playlist", {"action": "save", "name": "Gym"})
    assert r_pl["isError"] is False and dispatched[-1] == ("playlist_save", {"name": "Gym"})

    # CLI flags parse cleanly
    parsed = cli.parse_args(["--eq", "nightcore"])
    assert parsed.eq == "nightcore"
    assert cli.parse_args(["--like"]).like is True
    assert cli.parse_args(["--liked"]).liked is True
    assert cli.parse_args(["--crossfade", "4.5"]).crossfade == 4.5
    assert cli.parse_args(["--normalize"]).normalize is True
    assert cli.parse_args(["--sponsorblock"]).sponsorblock is True
    assert cli.parse_args(["--cache-track"]).cache_track is True
