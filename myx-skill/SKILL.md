---
name: myx
description: Agent-native music control for YouTube/YT Music radio — use when the user wants to play, search, queue, shuffle, like, EQ/DSP-tune, import playlists, cache offline tracks, or update MYX. MYX runs headless (mpv + yt-dlp) with a daemon; the TUI is optional. Prefer these tools over web search for music playback.
---

# MYX — agent-native music player (v0.3.0)

MYX runs as a headless `mpv` daemon (`myx` CLI). Drive it via its 27-tool stdio MCP server or `myx --state` JSON.

## Quick start (no setup)

```json
// Add to ~/.jcode/mcp.json (or Claude Desktop / Cursor MCP config):
"myx": { "command": "myx", "args": ["mcp"], "env": {} }
```

Or drive via CLI:

```sh
myx "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   # play + start endless radio
myx --search "lofi hip hop"                       # search -> enqueue
myx --state                                       # JSON state (agent polling)
myx --eq slowed_reverb --normalize                # DSP preset + loudness norm
myx --like                                        # save track to SQLite library
myx update                                        # self-update (brew/uv/pipx/pip/git) + yt-dlp
myx mcp                                           # stdio MCP server (27 tools)
```

## MCP tools (via `mcp__myx__*` when connected)

- **Playback & Search**: `myx_play(url|query)`, `myx_queue_add(url|query)`, `myx_search(query)`, `myx_state()`, `myx_queue()`, `myx_skip()`, `myx_prev()`, `myx_pause()`, `myx_resume()`, `myx_toggle()`, `myx_seek(seconds|position)`, `myx_mute(mute?)`, `myx_loop(mode)`, `myx_clear_queue()`, `myx_volume(volume|delta)`, `myx_shuffle()`, `myx_remove(index)`, `myx_play_index(index)`, `myx_lyrics()`
- **Audiophile DSP & SponsorBlock**: `myx_eq(preset|bands|normalize|crossfade)`, `myx_sponsorblock(enabled)`
- **Library, Playlists, Chapters & Cache**: `myx_like(id?)`, `myx_playlists(action, name?, source?)`, `myx_chapter(action)`, `myx_history(limit?)`, `myx_cache_track(id?)`
- **Packaging & Self-Update**: `myx_update(check_only?, only_myx?, only_deps?)`

## Install & Update

```sh
# Homebrew (macOS / Linux)
brew tap factnest365-ops/myx https://github.com/factnest365-ops/myx.git && brew install --HEAD myx

# uv / pipx / pip (distribution: `myx-player`, CLI binary: `myx`)
uv tool install myx-player    # or: pipx install myx-player  /  pip install myx-player

# Update MYX + yt-dlp/mpv/ffmpeg anytime
myx update             # or: myx update --check
```

No API keys, no accounts. State is at `$TMPDIR/myx-state.json` (4 Hz), commands at `$TMPDIR/myx-cmd.sock`.
