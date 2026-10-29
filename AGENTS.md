# MYX - terminal music player for YouTube/YT Music radio

Free-stack only: Python + mpv (playback/IPC) + yt-dlp (streams, search, metadata). No API keys, no accounts, no paid services.

The target is a complete daily-driver music client that a **human operates from the keyboard** and an **agent operates over MCP**. Orange-on-black TUI: queue sidebar left, huge centered album art, live spectrum below, synced-lyrics view, keybind footer. If a feature cannot be reached from both a keybind and an MCP tool, it is not finished.

**This file is the spec.** It is enforced by `tests/test_bible.py`: the CONTRACT block below is parsed and asserted against the code. Changing a number here without changing code fails the build, and the reverse. Fix code or fix the bible - never widen a test to make it pass.

## Run targets

- `myx "<url>"` - radio from a seed. RD chain refill, dedupe seen-set, eager first fill, fallback repeat when the chain is exhausted (`player.py`).
- `myx --search "query"` - ytsearch20 into the queue.
- `myx mcp --stdio` - MCP server, JSON-RPC 2.0, 27 tools, does not own playback.
- `myx --state` - current state as JSON, exit. Agent-friendly.
- `myx --version` - version + detected package manager (brew / uv / pipx / git-editable / pip).
- `myx update [--check] [--only-myx] [--only-deps] [--git]` - upgrade app + `yt-dlp` / `mpv` / `ffmpeg`.
- Headless controls: `--quit --skip --prev --toggle --vol N --seek --jump --mute --loop --clear --like --liked --eq --normalize --crossfade --sponsorblock --import-playlist --cache-track`, plus `--no-lyrics` and `--doctor [--fix]`.
- `myx` with no arguments and no daemon - For You / empty-state hint.

## Architecture - 9 modules, frozen contracts

Dependency direction is a strict DAG. A module may import only what its contract declares, and the CONTRACT block is the authority. Tree leaves import nothing from `myx`.

**`sources.py`** - the only module that fetches.
Owns: `search`, `mix_chain` / `mix_chain_items`, `meta`, `stream_url`, `cache_thumb`, `cache_audio`, `lyrics` (lrclib), `playlist_items`, `sponsorblock_segments`, chapters.
Never: mpv, playback state, SQLite, TUI.
Hardening is part of the contract: `_validate_video_id`, `_validate_http_url`, `_SafeRedirectHandler` (blocks loopback / link-local / private redirects), symlink + oversize rejection on disk cache, `_sanitize_text` strips terminal escapes from untrusted titles.

**`player.py`** - `PlayerDaemon`, the only module that talks to mpv.
Owns: JIT queue, 2-track mpv buffer, refill loop, stream cache with CDN-derived TTL, atomic state writes, command socket server, singleton lockfile, mpv healthcheck, EOF watchdog, DSP application, chapter tracking, crossfade, SponsorBlock skipping.
Never: TUI rendering, MCP protocol, direct network fetches (go through `sources`).

**`dsp.py`** - pure audio maths.
Owns: `DSPConfig` (presets, 5-band parametric EQ, crossfade seconds), `build_lavfi_chain`, `build_mpv_af`, `mpv_dsp_commands`, `compute_crossfade_gain`.
Never: IO of any kind. Imports nothing from `myx`.

**`library.py`** - the only module that touches SQLite.
Owns: `Library` - tracks, likes, playlists, M3U import/export, play history, most-played, lyrics offsets, offline audio cache ledger.
Never: network, mpv, TUI. Imports nothing from `myx`.

**`picker.py`** - pure search / palette logic for the TUI.
Owns: `fuzzy_score`, `fuzzy_filter`, `Debouncer`, `PickerState`, `command_palette_search`, result formatting.
Never: IO, network, rendering. Imports nothing from `myx`.

**`updater.py`** - self-update and diagnostics.
Owns: `detect_install_method`, `build_dep_update_commands`, `check_latest_version`, `build_update_plan`, `run_update`.
Never: playback, TUI. Imports nothing from `myx` except `__version__`.

**`mcp.py`** - MCP server over stdio.
Owns: JSON-RPC 2.0 framing, error codes, batch handling, the 27-tool surface, IPC to the daemon, direct-mpv fallback when no daemon is reachable.
Never: owning playback, owning daemon lifecycle, TUI.

**`tui.py`** - the renderer.
Owns: theme + `~/.config/myx/config.toml`, display-width maths (`cell_len`, `crop`, `clip`, `pad`), art protocols (kitty -> iterm2 -> halfblock), spectrum taps (cava -> ffmpeg), lyrics view, picker overlay, command palette, mouse, keymap.
Never: **imports from `myx` other than `picker`**, network fetches, SQLite, playback decisions. It reads the state file and writes the command socket. That is the whole boundary.

**`cli.py`** - entrypoint.
Owns: argument parsing, bin checks, daemon boot/cleanup, lockfile, headless control routing, doctor.

## Laws

1. **Size**: no function over 80 lines. Existing offenders are listed in `grandfathered_functions` and may only shrink.
2. **One owner per resource**: a socket path, state path, or liveness probe is declared once and imported. A second copy is a defect.
3. **DAG**: the import graph equals the CONTRACT block. No cycles.
4. **Boundary**: `tui.py` never fetches and never decides playback.
5. **Hygiene**: no bare `except:`, no `import *`.
6. **Hermetic tests**: no network, no real mpv, no real yt-dlp. Everything through `monkeypatch` / fixtures.
7. **Both doors**: a user-facing capability ships with a keybind and an MCP tool, or it is not finished.

## CONTRACT

Machine-read by `tests/test_bible.py`. Keep the markers.

<!-- BEGIN CONTRACT -->
```json
{
  "python": ">=3.11",
  "deps": ["numpy>=2.0.0", "pillow>=10.0.0"],
  "dev_deps": ["pytest>=8.0.0"],
  "console_script": "myx",
  "module_count": 9,
  "mcp_tool_count": 27,
  "max_function_lines": 80,
  "modules": {
    "sources": [],
    "dsp": [],
    "library": [],
    "picker": [],
    "updater": [],
    "player": ["dsp", "library", "sources"],
    "mcp": ["player", "sources", "updater"],
    "tui": ["picker"],
    "cli": ["mcp", "tui", "updater"]
  },
  "grandfathered_functions": {
    "player.handle_command": 580,
    "mcp.handle_tool": 319,
    "player.write_state": 197,
    "tui.handle_key": 160,
    "tui._draw_now": 158,
    "tui.render_once": 143,
    "cli.main": 122,
    "updater.detect_install_method": 116,
    "tui._execute_command": 113,
    "tui.handle_mouse": 111,
    "tui._draw_search_picker": 106,
    "updater.run_update": 104,
    "cli._print_doctor": 100,
    "cli.parse_args": 99,
    "tui._draw_queue": 82
  }
}
```
<!-- END CONTRACT -->

## Known debt

Recorded here so it is not rediscovered every session. Each item shrinks the contract when fixed.

- `player.handle_command` (580 lines, 38 commands, 141 branches) and `mcp.handle_tool` (319) are if-chains. Target: dispatch tables, one small handler per command/tool.
- Socket path, state path and daemon-liveness probing are re-implemented in `cli.py`, `mcp.py` and `tui.py`. Target: single `ipc.py` owner.
- `tui.Renderer` is one 1465-line class. Target: split into draw units behind the same public surface.
- `myx/player.py` mixes queue policy, mpv IPC, state serialisation and DSP application. Target: extract state serialisation.

## Packaging and releases

- uv project. Deps exactly as declared in the CONTRACT (`numpy`, `pillow`). No others. No added dependency without replacing one.
- Version lives in `myx/__init__.py`, `pyproject.toml`, `uv.lock`, `Formula/myx.rb`. Sync with `python3 scripts/bump_release.py <version>`; CI runs `--verify`.
- Tagging `vX.Y.Z` triggers `.github/workflows/release.yml` (PyPI, GitHub Release, Homebrew formula SHA256).
- External bins checked at boot: mpv, yt-dlp, ffmpeg. Missing -> one-line install hint, exit 1.
- macOS arm64 + Linux. License MIT.

## Testing

- `uv run pytest` green before any commit. `tests/test_bible.py` must pass: it is the spec check.
- New behaviour gets a test in the closest existing file; new module gets its own. No network, no real mpv, no real yt-dlp.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session. Point to the authoritative file or command instead of repeating what the code shows. When code and bible disagree, decide which one is right, then change both in the same commit - and let `tests/test_bible.py` be the thing that catches you if you forget.
