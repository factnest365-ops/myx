# MYX Usability Bugfix Audit

## Findings Table

| ID | Severity | Symptom | Reproduction | Root Cause | Fix | Test |
|----|----------|---------|--------------|------------|-----|------|
| BUG-001 | P0 | Seeding a track by URL plays with garbage title (raw CDN query string) and artist=None | `myx "https://www.youtube.com/watch?v=<id>"` then `myx --state` | Seed path in `player.py:_play_now()` loads stream directly without calling `sources.meta()`; state reads `media-title` from mpv which returns raw URL for direct streams | In `_play_now()`, fetch metadata via `sources.meta()` and cache it; use cached metadata in `write_state()` | New hermetic test in `test_backend.py` |
| BUG-002 | P0 | Dead daemon leaves lying state file: `myx --state` reports `playing: true` with stale data forever | Start `myx <url>`, kill daemon with `pkill -f myx-mpv`, run `myx --state` | `PlayerDaemon.shutdown()` doesn't remove/invalidate `myx-state.json`; no liveness check in state readers | Remove state file in `shutdown()`; add `daemon_healthy` check in CLI/MCP state readers | New hermetic test for dead-daemon state |
| BUG-003 | P1 | Headless controls missing: `--quit`, `--skip`, `--prev`, `--toggle`, `--vol` not recognized | `myx --quit` fails with "unrecognized arguments" | CLI argument parser in `cli.py` doesn't define these flags; they were documented in brief but never implemented | Add headless control flags to CLI parser, route to daemon command socket | Integration test for headless controls |

## Usability Changes (what users will notice)

- Seed URL playback now shows correct track title and artist instead of raw CDN query string
- `myx --state` correctly reports `playing: false` when daemon is dead
- Headless controls (`myx --quit`, `myx --skip`, `myx --prev`, `myx --toggle`, `myx --vol N`) work without TUI
- `--no-lyrics` flag (from preserved patch) works for battery/memory savings

## Known Remaining Issues

- TUI: Need to audit render-loop timing, resize handling, unicode/CJK width, key handling crashes
- MCP: Need to audit JSON-RPC 2.0 conformance, batch requests, unknown methods, tool arg validation
- Picker: Need to audit fuzzy-match scoring, timeout/retry behavior
- Sources: Need to audit timeout/retry, result-shape assumptions from yt-dlp, cache correctness

---
*Audit started: 2026-09-28*