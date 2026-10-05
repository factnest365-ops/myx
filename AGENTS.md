# MYX - terminal music player for YouTube/YT Music radio

Free-stack only: Python + mpv (playback/IPC) + yt-dlp (streams, search, metadata). No API keys, no accounts, no paid services. Reference look: orange-on-black TUI, queue sidebar left, huge centered album art, live spectrum below, synced-lyrics view, keybind footer.

## Run targets
- `myx "<youtube-or-ytmusic-url>"` starts radio: plays seed, refills forever via RD-chain (dedupe seen-set, eager first fill, keep buffer >= 2, fallback repeat when chain cached).
- `myx --search "query"` opens search results into queue.
- `myx mcp --stdio` runs as an MCP server over stdio (agent-native).
- `myx --state` prints current player state as JSON and exits (agent-friendly).
- Headless controls: `--quit`, `--skip`, `--prev`, `--toggle`, and `--vol N`; `--no-lyrics` disables lyrics lookup.

## Project memory

- State readers must require a live lockfile PID and a connectable command socket; state-file age is not a liveness signal.
- Lyrics lookup runs off the state-write path so slow network calls cannot stall state updates.
- MCP state handling falls back to the empty state for malformed JSON and non-object payloads; keep protocol behavior covered by `tests/test_mcp.py`.

## Architecture (4 modules, fixed contracts)

### myx/sources.py - yt-dlp + lyrics wrappers (pure functions)
- `search(q) -> [{id,title,artist,duration,thumb}]` via yt-dlp `ytsearch20` json.
- `mix_chain(video_id) -> [ids]` flat-playlist RD chain, extractor-args youtube:player_client=web,music.
- `meta(video_id) -> {id,title,artist,duration,thumb}` (artist = uploader cleaned).
- `cache_thumb(url_or_id, thumb_url) -> local path` under `~/Library/Caches/myx/thumbs/`.
- `lyrics(title, artist, duration_s=None) -> {"lrc": str|None, "plain": str|None}` from https://lrclib.net API (free, no key). Never crash on miss; return Nones.
- All network calls timeout 20s, raise SourcesError(msg) with human text.

### myx/player.py - PlayerDaemon (runs standalone: `python -m myx.player <url>`)
- Spawns `mpv --input-ipc-server=$TMPDIR/myx-mpv.sock --idle=yes --no-video --no-ytdl`.
- JSON IPC over that socket (stdlib socket, no python-mpv dep). Commands in: toggle/pause/resume/skip/prev/vol N/shuffle/play_id ID/load URL/jump POS/del/play_index/quit.
- JIT Queue & 2-track MPV buffer: MPV maintains a 2-track buffer (Track 0 currently playing, Track 1 pre-resolved next track for gapless transitions); remaining items (Tracks 2..N) are held in daemon memory as lightweight metadata (`id`, `title`, `artist`, `duration`, `thumb`, `stream_url=None`) with non-blocking stream resolution just-in-time as tracks approach playback.
- Non-blocking IPC commands: `play_id`, `load`, and `search` return immediately; `play_id` triggers background radio refill via `_refill_loop` without blocking `handle_command`.
- Refill loop thread: when queue <= 3, `mix_chain_items(current)` -> filter seen-set -> append metadata to queue; eager fill at start; retry/backoff on yt-dlp failure.
- Non-blocking state out: writes atomically every tick (>=4 Hz while playing) to `$TMPDIR/myx-state.json`. Uncached thumbnails and lyrics resolve in background threads without blocking the 250ms tick loop.
- Command server: listen `$TMPDIR/myx-cmd.sock`, one JSON cmd per line, reply `{"ok":true}` or `{"ok":false,"error":"..."}`.
- Singleton lockfile at `$TMPDIR/myx.lock`; healthcheck loop restarts mpv on death/unresponsiveness; EOF watchdog kicks stalled tracks.

### myx/mcp.py - MCP server (stdio JSON-RPC 2.0, no external deps)
- Exposes 15 tools over stdio by wrapping the daemon IPC + reading `$TMPDIR/myx-state.json`. Does not own playback.
- Server identity: `myx` v0.2.0, protocol `2024-11-05`.
- Tools:
  - `myx_play(url|query)` — play a YouTube URL, video ID, or search query (e.g. 'blinding lights'); boots daemon if not running; falls back to direct mpv.
  - `myx_queue_add(url|query)` — enqueue a track by search query, YouTube URL, or video ID.
  - `myx_search(query)` — ytsearch20, enqueues all results; if daemon unreachable, returns results without enqueueing.
  - `myx_state` — current player state (playing/paused, title/artist, position/duration, volume, queue, lyrics).
  - `myx_queue` — upcoming tracks + current.
  - `myx_skip` / `myx_prev` — playlist-next / playlist-prev.
  - `myx_pause` / `myx_resume` / `myx_toggle` — pause control.
  - `myx_volume(volume|delta)` — absolute 0..100 or relative ±N.
  - `myx_shuffle` — shuffle queue.
  - `myx_lyrics` — `{lrc, plain, title, artist}` for current track.
  - `myx_remove(index)` — remove queue item by index.
  - `myx_play_index(index)` — jump to queue item by index.
- IPC: sends JSON cmds to `$TMPDIR/myx-cmd.sock`; reads state from `$TMPDIR/myx-state.json`; falls back across candidate tmpdirs.
- Entry: `myx mcp --stdio` (cli.py routes here); direct: `python -m myx.mcp`.

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
argparse: positional url optional, --search, --lyrics-only flag dev aid, --no-lyrics, --state for agent JSON, headless --quit/--skip/--prev/--toggle/--vol N controls, and subcommand `mcp` for MCP server. Boots PlayerDaemon subprocess if not running (single-instance lockfile at `$TMPDIR/myx.lock`), then TUI. On quit: SIGTERM daemon, kill mpv, clean lockfile. Bin checks at boot: mpv, yt-dlp, ffmpeg — missing exits with install hint.

## Rules
- uv project. deps: numpy>=2.0.0, pillow>=10.0.0. dev: pytest>=8.0.0. Build: hatchling. Console script: `myx` -> `myx.cli:main`.
- External bins checked at boot: mpv, yt-dlp, ffmpeg; missing -> one-line install hint, exit 1.
- No other deps. urllib for lrclib. No network calls in tui.py ever.
- Tests (pytest, no network, no real mpv): sources JSON-shape fixtures parsed correctly; player state machine against fake socket pair; cmd protocol roundtrip; MCP tools list shape and handle_tool paths. `uv run pytest` green before any commit.
- Conventional commits, commit after each working unit.
- macOS/bash 3.2 irrelevant here (Python only), but keep everything runnable on macOS arm64 + Linux.
- License: MIT (Copyright © 2026 Bhavya Singh).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
