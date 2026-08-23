# MYX — terminal music player for YouTube / YT Music radio

Orange-on-black TUI, queue sidebar, huge centered album art, live spectrum, synced lyrics. Free-stack: Python + mpv + yt-dlp + ffmpeg.

![stack](https://img.shields.io/badge/stack-python%20%2B%20mpv%20%2B%20yt--dlp-orange)

## Install

```sh
brew install mpv yt-dlp ffmpeg
uv sync          # or: pip install -e .
```

Requires Python ≥ 3.11, `numpy`, `pillow`.

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
- `myx/player.py` — `PlayerDaemon` (`python -m myx.player <url>`): spawns `mpv --input-ipc-server=$TMPDIR/myx-mpv.sock --idle=yes --no-video --ytdl-format=bestaudio`, JSON IPC, refill loop, atomic `$TMPDIR/myx-state.json` at ≥ 4 Hz, command server on `$TMPDIR/myx-cmd.sock` (JSON per line).
- `myx/tui.py` — alt-screen 12 fps renderer: reads state file, writes command socket, owns zero playback logic.
- `myx/cli.py` — entrypoint, single-instance lockfile, bin checks, `--search` / `--lyrics-only`.

## Dev

```sh
uv run pytest -q
```

Tests are hermetic: no network, no real mpv — fixture JSON shapes and fake socket pairs.
