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
| `n` / `p` | next / prev |
| `/` | search (type query, enter) |
| `s` | shuffle |
| `+/-` (`=`) | volume ±5 |
| `l` / `tab` | cycle view: Now Playing → Lyrics → Queue |
| `j` / `k` (arrows) | move selection in queue |
| `enter` | play selected queue entry |
| `d` | delete selected queue entry |
| `[` / `]` | nudge lyrics offset ∓0.5 s |
| `q` | quit |

## Views

- **Now Playing** — album art (kitty → iterm2 → half-block), title/artist, `m:ss ──o── m:ss` bar, 24-bar spectrum.
- **Lyrics** — centered plain or synced LRC (current line highlighted, auto-follows).
- **Queue** — full-screen queue with durations; selection highlighted.

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
- `myx/player.py` — `PlayerDaemon` (`python -m myx.player <url>`): spawns `mpv --input-ipc-server=$TMPDIR/myx-mpv.sock --idle=yes --no-video --no-ytdl`, JSON IPC, refill loop, atomic `$TMPDIR/myx-state.json` at ≥ 4 Hz, command server on `$TMPDIR/myx-cmd.sock` (JSON per line).
- `myx/tui.py` — alt-screen 12 fps renderer: reads state file, writes command socket, owns zero playback logic.
- `myx/mcp.py` — stdio MCP server (JSON-RPC 2.0, no deps): exposes 14 tools over stdio by wrapping the same daemon IPC + `myx-state.json`. Agent-native, TUI-optional.
- `myx/cli.py` — entrypoint, single-instance lockfile, bin checks, `--search` / `--lyrics-only`, headless controls (`--quit`, `--skip`, `--prev`, `--toggle`, `--vol`), `--no-lyrics`, plus agent-friendly `myx --state` and `myx mcp`.

## Agent-native (MCP)

MYX is built agent-first: the TUI is a view, the daemon is the API. Agents don't need the TUI at all.

```sh
myx --state              # pollable JSON — title/artist/position/queue/lyrics
myx --no-lyrics URL      # skip lyrics fetching in headless or agent mode
myx --skip               # headless playback controls also include --quit, --prev, --toggle, --vol N
myx mcp --stdio          # stdio MCP server — 14 tools
myx "https://..."        # still works headless — mpv + yt-dlp, no TUI needed
```

### Connecting to jcode (or any MCP client)

Add to `~/.jcode/mcp.json` (jcode already has MYX installed):

```json
{ "servers": { "myx": { "command": "uv", "args": ["run", "--project", "/path/to/myx", "python", "-m", "myx.mcp"], "env": {}, "shared": true } } }
```

Skill is at `~/.jcode/skills/myx/SKILL.md` — auto-loaded. Any jcode agent can then call `myx_search`, `myx_play`, `myx_state`, etc. Also `myx-skill/SKILL.md` in the MYX repo for sharing.

**MCP tools:** `myx_play(url)` · `myx_search(query)` · `myx_state` · `myx_queue` · `myx_skip`/`myx_prev` · `myx_pause`/`myx_resume`/`myx_toggle` · `myx_volume(volume|delta)` · `myx_shuffle` · `myx_lyrics` · `myx_remove(index)` · `myx_play_index(index)` — all backed by the daemon's `myx-cmd.sock` + `myx-state.json`; none touch the TUI.

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
