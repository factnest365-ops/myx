# MYX - terminal music player for YouTube/YT Music radio

Free-stack only: Python + mpv (playback/IPC) + yt-dlp (streams, search, metadata). No API keys, no accounts, no paid services. Reference look: orange-on-black TUI, queue sidebar left, huge centered album art, live spectrum below, synced-lyrics view, keybind footer.

## Run targets
- `myx "<youtube-or-ytmusic-url>"` starts radio: plays seed, refills forever via RD-chain (port logic from ~/bin/ytradio: dedupe seen-set learned from appends, eager first fill, keep buffer >= 2, fallback repeat when chain cached).
- `myx --search "query"` opens search results into queue.

## Architecture (3 modules, fixed contracts)

### myx/sources.py - yt-dlp + lyrics wrappers (pure functions)
- `search(q) -> [{id,title,artist,duration,thumb}]` via yt-dlp `ytsearch20` json.
- `mix_chain(video_id) -> [ids]` flat-playlist RD chain, extractor-args youtube:player_client=web,music.
- `meta(video_id) -> {id,title,artist,duration,thumb}` (artist = uploader cleaned).
- `cache_thumb(url_or_id, thumb_url) -> local path` under `~/Library/Caches/myx/thumbs/`.
- `lyrics(title, artist, duration_s=None) -> {"lrc": str|None, "plain": str|None}` from https://lrclib.net API (free, no key). Never crash on miss; return Nones.
- All network calls timeout 20s, raise SourcesError(msg) with human text.

### myx/player.py - PlayerDaemon (runs standalone: `python -m myx.player <url>`)
- Spawns `mpv --input-ipc-server=$TMPDIR/myx-mpv.sock --idle=yes --no-video --ytdl-format=bestaudio`.
- JSON IPC over that socket (stdlib socket, no python-mpv dep). Commands in: toggle/pause/resume/skip/prev/vol N/shuffle/play_id ID/load URL/jump POS/quit.
- Refill loop thread: when queue <= 2, mix_chain(current) -> filter seen-set -> loadfile append each; mark_seen on successful append; eager fill at start; retry/backoff on yt-dlp failure; never let mpv go idle while tracks exist.
- State out: writes atomically every tick (>=4 Hz while playing) to `$TMPDIR/myx-state.json`: `{version, playing(bool), paused, title, artist, id, url, thumb(path|None), duration, position, volume, queue:[{id,title,duration,thumb}]}`.
- Command server: listen `$TMPDIR/myx-cmd.sock`, one JSON cmd per line, reply `{"ok":true}` or `{"ok":false,"error":"..."}`.

### myx/tui.py - renderer (reads state file, writes cmd socket; owns zero playback logic)
- Raw mode, alt screen, ~12 fps loop. Stdlib termios/tty; ANSI escapes only.
- Top bar: `M Y X` left; right view tabs `•• Now Playing · Lyrics · Queue`.
- Left panel 38% width: section header `< Library  2/6 · 1799`, rows: index, title, dim artist right-aligned; playing row accent bg black text; played rows dim.
- Center: album art max 40 rows square. Order: kitty graphics protocol (if KITTY_WINDOW_ID or term contains kitty) -> iterm2 inline images (OSC 1337 File) -> half-block RGB (ffmpeg -i thumb -vf scale=WxH -f rawvideo rgba, pure-python nearest-neighbor scale, two pixels per cell via ▀ fg/bg). Cache last drawn path; redraw only on change.
- Under art: bold title, dim artist, `m:ss elapsed ──────o── m:ss` bar.
- Bottom third: spectrum. Pluggable SpectrumSource: (a) CavaSource if `cava` binary exists (pipe raw ascii), else (b) FfmpegTapSource: `ffmpeg -re -i <stream_url> -f s16le -ac 1 -ar 8000 -` -> 64-bin log-scale FFT (numpy), 24 bars, accent color gradient, peak-hold dots. Known v1 limitation: tap latency vs playback accepted; document in README. If both unavailable: flat baseline.
- Lyrics view: replaces art+spectrum area; plain centered lines, synced LRC highlights current line (accent), auto-follow, `[`/`]` offset nudge ±0.5s.
- Footer keybinds: `space pause · n next · p prev · / search · s shuffle · +,- vol · l lyrics · tab view · j,k nav · enter play · d del · q quit`. Search prompt overlays bottom: query -> sources.search -> append all to queue, toast "queued N".
- Theme dict: bg #0d0d0d fg #e8e3d8 accent #ff6b35 dim #7a756c err #ff4444. Optional override `~/.config/myx/config.toml`.

### myx/cli.py - entrypoint
argparse: positional url optional, --search, --lyrics-only flag dev aid. Boots PlayerDaemon subprocess if not running (single-instance lockfile), then TUI. On quit: SIGTERM daemon, kill mpv.

## Rules
- uv project. deps: numpy, pillow (only if half-block path needs resize convenience; prefer rawvideo+nearest). dev: pytest.
- External bins checked at boot: mpv, yt-dlp, ffmpeg; missing -> one-line install hint, exit 1.
- No other deps. urllib for lrclib. No network calls in tui.py ever.
- Tests (pytest, no network, no real mpv): sources JSON-shape fixtures parsed correctly; player state machine against fake socket pair; cmd protocol roundtrip. `uv run pytest` green before any commit.
- Conventional commits, commit after each working unit.
- macOS/bash 3.2 irrelevant here (Python only), but keep everything runnable on macOS arm64 + Linux.
