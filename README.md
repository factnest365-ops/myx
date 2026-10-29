# MYX — terminal music player for YouTube / YT Music radio

Orange-on-black TUI, queue sidebar, huge centered album art, live spectrum, synced lyrics. Free-stack: Python + mpv + yt-dlp + ffmpeg.

![stack](https://img.shields.io/badge/stack-python%20%2B%20mpv%20%2B%20yt--dlp-orange)

## Install

Pick one — easiest first.

### Option 1 — Homebrew (recommended, macOS / Linux)

```sh
brew tap factnest365-ops/myx && brew install myx
```

This installs `myx` plus its system deps (`mpv`, `yt-dlp`, `ffmpeg`) and the Python deps (`numpy`, `pillow`). Then:

```sh
myx doctor        # check everything is happy
myx               # launches the app shell — just type to search
```

> Formula lives at `Formula/myx.rb` in this repo. On release, update `url` + `sha256` in the formula and `brew tap` picks it up.

### Option 2 — pipx / uv tool / pip (any OS with Python ≥ 3.11)

```sh
# pipx (isolated, recommended for pip users)
pipx install myx
# or uv
uv tool install myx
# or pip
pip install myx
```

Then make sure the system bins are present:

```sh
myx doctor        # reports mpv / yt-dlp / ffmpeg status
myx doctor --fix  # tries to auto-fix (brew install/upgrade, stale lock cleanup)
```

If any bin is missing, install it with your OS package manager:

```sh
# macOS
brew install mpv yt-dlp ffmpeg
# Ubuntu / Debian
sudo apt-get install mpv ffmpeg && pipx install yt-dlp
```

### Option 3 — from source (for contributors)

```sh
git clone https://github.com/factnest365-ops/myx && cd myx
uv sync --extra dev   # or: pip install -e ".[dev]"
uv run myx --help
```

Requires Python ≥ 3.11. Console script is `myx = myx.cli:main` (see `pyproject.toml`).

### First run

1. Install via any option above.
2. Run `myx` with no args — it opens the app shell with a search picker.
3. Type a query (e.g. `blinding lights`), hit `enter` — results fill the queue and playback starts. Or seed radio directly: `myx "https://www.youtube.com/watch?v=..."`.
4. If something looks off, `myx doctor` tells you what to fix.

## Use

```sh
myx "https://www.youtube.com/watch?v=..."   # radio from seed
myx "https://music.youtube.com/watch?v=..." # same — YT Music URLs work
myx --search "weeknd blinding lights"       # search → enqueue
myx --search "lofi" "https://youtube.com/watch?v=abc"  # search + seed together
```

Player refills forever via the RD mix chain: deduped `seen` set, eager first fill, keeps buffer ≥ 2, falls back to repeats when the chain is exhausted.

## Keys

| key | action |
|---|---|
| `space` | pause / resume |
| `n` / `p` | next / prev (`p` restarts track if >5 s, else previous in history) |
| `←` / `→` | seek ∓5 s |
| `/` (or just type) | search picker (`enter` plays, `tab` adds to queue) |
| `s` | shuffle |
| `ctrl-l` | like / unlike current song `♥` |
| `ctrl-e` | cycle Audiophile EQ / DSP preset (`Bass Boost`, `Vocal`, `Spatial`, `Slowed + Reverb`, `Nightcore`, ...) |
| `ctrl-b` | toggle SponsorBlock non-music auto-skip |
| `ctrl-t` | toggle Chameleon dynamic album-art palette |
| `{` / `}` | previous / next YouTube chapter |
| `ctrl-r` | cycle repeat (`off` → `one` → `all`) |
| `ctrl-g` | mute / unmute |
| `ctrl-k` | command palette |
| `+/-` (`=`) | volume ±5 |
| `l` / `tab` | cycle view: Now Playing → Lyrics → Queue |
| `j` / `k` (arrows, PgUp/PgDn, mouse wheel) | move selection in queue |
| `mouse click` | scrub progress bar, switch tabs, play Liked Songs `♥`, trigger transport buttons |
| `enter` | play selected queue entry |
| `d` | delete selected queue entry (press twice to confirm) |
| `[` / `]` | nudge lyrics offset ∓0.5 s (persisted per track in SQLite) |
| `?` | help & tour |
| `q` | quit |

## Views

- **Now Playing** — album art (kitty/Ghostty → iTerm2/WezTerm → Pillow Lanczos half-block), Chameleon album-art accent palette, live `♥` badge, chapter-marked `m:ss ──┼──o── m:ss` bar (`§ Chapter`), DSP/EQ/offline status badges, 24-bar Hann/Mel spectrum.
- **Lyrics** — centered plain or synced LRC (current line highlighted, per-track offset persisted in SQLite).
- **Queue** — sidebar + full-screen queue with durations, `▸ Play Liked Songs ♥ (N)`, and scroll viewport.

## Audiophile DSP, Library & Interactive Features

- **Audiophile DSP Engine (`myx/dsp.py`)**: 10 live `mpv` `lavfi` presets (`flat`, `bass_boost`, `vocal`, `treble_sparkle`, `rock`, `electronic`, `late_night`, `spatial`, `slowed_reverb`, `nightcore`), custom 5-band parametric EQ (`[60, 230, 910, 3600, 14000] Hz`), `dynaudnorm` loudness normalization, silence trimming, gapless playback (`--gapless-audio=yes`), and smart crossfade (`--crossfade 4`).
- **SponsorBlock Auto-Skip**: Automatically skips non-music intros/skits/outros via keyless `sponsor.ajay.app` lookups (`ctrl-b` or `--sponsorblock`).
- **Persistent SQLite Library & Offline Cache (`myx/library.py`)**: WAL-mode `sqlite3` database storing `♥` Liked Songs (`--like` / `--liked`), custom playlists, YouTube & `.m3u` playlist import/export (`--import-playlist`), play history / most played, per-track lyrics sync offsets, and local Opus/M4A offline audio cache (`--cache-track`) with LRU budget eviction.
- **Synced Zero-Bandwidth Spectrum**: `FfmpegTapSource` reads local cached Opus/M4A files first (zero extra network bandwidth), seeks to `-ss <position>`, freezes/resumes with `SIGSTOP`/`SIGCONT` on pause, auto-resyncs on scrub seeks, and computes 16 kHz Hann-windowed perceptual Mel/log-frequency bars.
- **Chameleon Theme & SGR 1006 Mouse**: Extracts WCAG-safe dominant + secondary accents from album art via Pillow (`ctrl-t` to toggle) and supports full SGR mouse click-to-scrub on the progress bar, tab/queue/heart clicking, and wheel scrolling.

## Art

Resolution order: kitty graphics protocol (`KITTY_WINDOW_ID` / `TERM` contains `kitty`) → iTerm2 inline images (`OSC 1337`) → half-block RGB fallback (`ffmpeg -vf scale` → `▀` with fg/bg, no Pillow needed for rendering; Pillow available for future resize paths). Last-drawn path is cached and only redrawn on change.

## Spectrum

Pluggable source: `CavaSource` if the `cava` binary is present (pipe raw ascii), else `FfmpegTapSource` (`ffmpeg -re -i <stream_url> -f s16le -ac 1 -ar 8000` → 64-bin log-scale FFT via numpy, 24 bars with accent gradient and peak-hold dots), else flat baseline. The `ffmpeg` tap reads the stream URL independently — **known v1 limitation**: tap latency is not sample-locked to mpv playback, so bars may lag or lead by a few hundred ms. This is documented and accepted.

## Lyrics

`lrclib.net` (no key) via `sources.lyrics(title, artist, duration_s)`. The player resolves lyrics per-track and caches by video id; the daemon never crashes on miss (returns `{lrc: None, plain: None}`). The TUI highlights the active LRC line.

## Theming

Override `~/.config/myx/config.toml` (simple `key = "#rrggbb"` lines, `#` comments). Keys: `bg`, `fg`, `accent`, `dim`, `err`. Defaults:

```toml
bg = "#0d0d0d"
fg = "#e8e3d8"
accent = "#ff6b35"
dim = "#7a756c"
err = "#ff4444"
```

## Architecture

- `myx/sources.py` — pure functions over `yt-dlp` JSON + `lrclib` (`search`, `mix_chain`, `meta`, `cache_thumb`, `lyrics`; 20 s timeout; `SourcesError`).
- `myx/player.py` — `PlayerDaemon` (`python -m myx.player <url>`): manages playback via `mpv --input-ipc-server=$TMPDIR/myx-mpv.sock --idle=yes --no-video --no-ytdl`, maintains a JIT 2-track buffer in MPV (Track 0 playing, Track 1 pre-resolved next; remaining items held in memory as metadata with on-demand stream resolution), non-blocking IPC commands (`play_id`, `load`, `search`), background radio refill via mix chain, non-blocking thumbnail and lyrics resolution, atomic `$TMPDIR/myx-state.json` at ≥ 4 Hz, and command server on `$TMPDIR/myx-cmd.sock` (JSON per line).
- `myx/tui.py` — alt-screen 12 fps renderer: reads state file, writes command socket, owns zero playback logic.
- `myx/dsp.py` — Audiophile DSP config, 10 `lavfi` EQ/speed/pitch presets, 5-band parametric EQ, `dynaudnorm`, silence trimming, and smart crossfade gain curves.
- `myx/library.py` — Thread-safe `sqlite3` WAL store (`0o700` dir / `0o600` db) for tracks, `♥` likes, playlists, M3U import/export, play history, per-track lyrics sync offsets, and offline Opus/M4A cache LRU eviction.
- `myx/mcp.py` — stdio MCP server (JSON-RPC 2.0, no deps): exposes 26 tools over stdio by wrapping the same daemon IPC + `myx-state.json`. Agent-native, TUI-optional.
- `myx/cli.py` — entrypoint, single-instance lockfile, bin checks, `--search` / `--lyrics-only`, headless controls (`--quit`, `--skip`, `--prev`, `--toggle`, `--vol`, `--seek`, `--jump`, `--mute`, `--loop`, `--clear`, `--like`, `--liked`, `--eq`, `--normalize`, `--crossfade`, `--sponsorblock`, `--import-playlist`, `--cache-track`), `--no-lyrics`, plus agent-friendly `myx --state` and `myx mcp`.

## Agent-native (MCP)

MYX is built agent-first: the TUI is a view, the daemon is the API. Agents don't need the TUI at all.

```sh
myx --state              # pollable JSON — title/artist/position/queue/lyrics/dsp/chapters
myx --no-lyrics URL      # skip lyrics fetching in headless or agent mode
myx --skip               # headless controls: --quit, --prev, --toggle, --vol N, --like, --liked, --eq bass_boost
myx mcp --stdio          # stdio MCP server — 26 tools
myx "https://..."        # still works headless — mpv + yt-dlp, no TUI needed
```

### Connecting to jcode (or any MCP client)

Add to `~/.jcode/mcp.json` (jcode already has MYX installed):

```json
{ "servers": { "myx": { "command": "uv", "args": ["run", "--project", "/path/to/myx", "python", "-m", "myx.mcp"], "env": {}, "shared": true } } }
```

Skill is at `~/.jcode/skills/myx/SKILL.md` — auto-loaded. Any jcode agent can then call `myx_search`, `myx_play`, `myx_state`, etc. Also `myx-skill/SKILL.md` in the MYX repo for sharing.

**MCP tools (26):** `myx_play(url|query)` · `myx_queue_add(url|query)` · `myx_search(query)` · `myx_state` · `myx_queue` · `myx_skip`/`myx_prev` · `myx_pause`/`myx_resume`/`myx_toggle` · `myx_seek(delta|position)` · `myx_volume(volume|delta)` · `myx_mute` · `myx_loop` · `myx_shuffle` · `myx_clear` · `myx_lyrics` · `myx_remove(index)` · `myx_play_index(index)` · `myx_eq(preset|bands)` · `myx_dsp(normalize|crossfade_s|trim_silence|sponsorblock)` · `myx_like(action)` · `myx_playlist(action|name|url|m3u)` · `myx_chapter(action)` · `myx_history(limit)` · `myx_cache_track(id)` — all backed by the daemon's `myx-cmd.sock` + `myx-state.json`; none touch the TUI.

## Security & Threat Model

MYX is a **single-user, local-machine** tool. Stated plainly:

- **No auth on IPC.** Any process running as your user can send commands to `$TMPDIR/myx-cmd.sock` and `myx-mpv.sock` (play/pause/skip/search), and read `$TMPDIR/myx-state.json` (current track, queue, lyrics). This is a feature for agents, an exposure for multi-user machines. Don't run MYX on shared hosts.
- **Network egress only**: yt-dlp talks to YouTube, lyrics fetch to lrclib.net. No inbound listeners, no telemetry.
- **Supply-chain trust anchor**: stream URLs come from whatever `yt-dlp` resolves. Update it regularly (`brew upgrade yt-dlp`) — YouTube extractor breakage is the #1 failure mode, and MYX surfaces it as `[myx] stream resolve failed (...)` rather than dying silently.
- **Tmpdir contract**: daemon and clients agree on the state/cmd socket paths via `TMPDIR`. If your MCP client sees a different `TMPDIR` than the daemon (common on macOS), set `MYX_FALLBACK_TMP=/tmp` for both, or rely on the built-in `/tmp` fallback scan.

## Dev

```sh
uv run pytest -q
```

Tests are hermetic: no network, no real mpv — fixture JSON shapes and fake socket pairs.
