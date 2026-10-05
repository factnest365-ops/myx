---
name: myx
description: Agent-native music control for YouTube/YT Music radio — use when the user wants to play, search, queue, shuffle, or control background music from MYX. MYX runs headless (mpv + yt-dlp) with a daemon; the TUI is optional. Prefer these tools over web search for music playback.
---

# MYX — agent-native music player

MYX is running as a daemon (`myx` CLI, headless mpv). You can drive it without the TUI — via its MCP server or `myx --state` JSON.

## Quick start (no setup)

```json
// Add to ~/.jcode/mcp.json servers:
"myx": { "command": "myx", "args": ["mcp"], "env": {} }
```

Or without MCP, just shell it:

```sh
myx "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   # play + start radio
myx --search "lofi hip hop"                       # search → enqueue
myx --state                                       # JSON state (agent polling)
myx mcp                                           # stdio MCP server (15 tools)
```

## MCP tools (via `mcp__myx__*` when connected)

- `myx_play(url|query)` — play a YouTube/YT Music URL, video ID, or search query (e.g. 'blinding lights')
- `myx_queue_add(url|query)` — enqueue a track by search query, YouTube URL, or video ID
- `myx_search(query)` — ytsearch20 + enqueue, returns `queued` count
- `myx_state()` — full state: playing/paused, title/artist, position/duration, volume, queue, lyrics (lrc/plain)
- `myx_queue()` — upcoming queue
- `myx_skip()` / `myx_prev()` — next / prev
- `myx_pause()` / `myx_resume()` / `myx_toggle()`
- `myx_volume(volume?: 0..100, delta?: ±N)`
- `myx_shuffle()` — shuffle queue
- `myx_lyrics()` — current track lyrics
- `myx_remove(index)` / `myx_play_index(index)`

## When to use

- User says play/search/skip/queue music, background music, radio, YouTube music
- You want ambience while coding — `myx_search("lofi")` then `myx_state()` to confirm

## Install (one-time)

```sh
brew install mpv yt-dlp ffmpeg
pip install -e ~/myx   # or: uv sync at ~/myx
```

No API keys, no accounts. State is at `$TMPDIR/myx-state.json` (4 Hz), commands at `$TMPDIR/myx-cmd.sock`.

## Reference

- Repo: `~/myx` — see `README.md`, `AGENTS.md`
- MCP impl: `~/myx/myx/mcp.py` — pure stdlib, JSON-RPC 2.0 over stdio
