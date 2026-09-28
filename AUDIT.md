# MYX Usability Bugfix Audit

## Findings Table

| ID | Severity | Symptom | Reproduction | Root Cause | Fix | Test |
|----|----------|---------|--------------|------------|-----|------|
| BUG-001 | P0 | Seeding a track by URL plays with garbage title (raw CDN query string) and artist=None | `myx "https://www.youtube.com/watch?v=<id>"` then `myx --state` | Seed path in `player.py:_play_now()` loads stream directly without calling `sources.meta()`; state reads `media-title` from mpv which returns raw URL for direct streams | In `_play_now()`, fetch metadata via `sources.meta()` and cache it; use cached metadata in `write_state()` | New hermetic test in `test_backend.py` |
| BUG-002 | P0 | Dead daemon leaves lying state file: `myx --state` reports `playing: true` with stale data forever | Start `myx <url>`, kill daemon with `pkill -f myx-mpv`, run `myx --state` | `PlayerDaemon.shutdown()` doesn't remove/invalidate `myx-state.json`; no liveness check in state readers | Remove state file in `shutdown()`; require live pid and command socket in CLI/MCP readers | `test_state_missing_after_daemon_death`, `test_cli_state_rejects_stale_state`, `test_mcp_state_rejects_dead_daemon_state` |
| BUG-003 | P1 | Headless controls missing: `--quit`, `--skip`, `--prev`, `--toggle`, `--vol` not recognized | `myx --quit` fails with "unrecognized arguments" | CLI argument parser in `cli.py` doesn't define these flags; they were documented in brief but never implemented | Add headless control flags to CLI parser, route to daemon command socket | Integration test for headless controls |
| BUG-004 | P0 | MCP server violates JSON-RPC 2.0: wrong error codes, no id echo, batch unsupported | `myx mcp --stdio` with malformed/partial/batch requests | `mcp.py` hand-rolled the protocol without the spec's error table or id handling | Spec-compliant error codes, id echo, batch support, unknown-method handling | `test_mcp.py` JSON-RPC conformance tests |
| BUG-005 | P1 | Live spectrum never appears (bars always flat baseline) | Start playback, watch bottom third | `make_spectrum()` is defined but never called; Renderer defaults to a flat `SpectrumSource` | Lazy binding via `spectrum_auto` (set by `run()`), once per stream url, rebind on change | `test_spectrum.py::test_spectrum_bound_once_per_stream_url`, `test_run_enables_spectrum_auto` |
| BUG-006 | P1 | A dead/silent cava or ffmpeg tap blocks the render loop indefinitely | Point tap at a URL that streams no data | `CavaSource.values()` used blocking `readline()`; `FfmpegTapSource.values()` blocking `read()`; both skipped `SpectrumSource.__post_init__` so peaks never initialized | Non-blocking `os.read` + buffer, flat fallback, `super().__post_init__()` for peak-hold | Pipe-based thread tests (fail-by-timeout) for both sources + peak init |
| BUG-007 | P1 | Lines overflow the terminal: footer 110 cells at 80/100 cols, CJK titles past right edge | Render at (80,24) / (100,32) with CJK title | `put()` had no display-width awareness: `cell_len` counted CJK/emoji as 1, overflow branch padded instead of clipping | Width-aware `cell_len`/`crop`/`pad`, new ANSI-preserving `clip()`, overflow branch removed | `test_narrow_terminal_lines_fit_width`, `test_wide_terminal_keeps_full_footer`, cell_len/crop/clip unit tests |
| BUG-008 | - | (dropped) Alt+key freeze theory disproved | PTY test: `sys.stdin.read(2)` returns short reads without blocking | Pipes/TTY allow short reads; no freeze. Residual nit: a partial CSI sequence (e.g. `\x1b[1;5C`) leaves junk that auto-opens search | Not fixed (speculative; documented only) | n/a |
| BUG-009 | P2 | Album art stays at old size after terminal resize | Resize terminal while a thumb is showing | `ArtRenderer` cache keyed on path only | Cache key `(path, cells, rows)` | `test_art_cache_key_includes_cell_size` |
| BUG-010 | P1 | Escape sequences counted as visible width; kitty/iterm image payloads clipped mid-escape | Render with image protocol active | `strip_ansi`/width logic only handled basic CSI, not OSC/APC/DCS image payloads | `ANSI_RE` covers CSI + OSC (BEL/ST) + APC (kitty) + DCS/SOS/PM; all width math strips first | Narrow/wide terminal tests with image escapes |
| BUG-011 | P2 | 'Updating player… run myx doctor' toast sticks forever after the player recovers | Idle state → start playback | Auto toast set but never cleared when state stops being empty | `AUTO_HINT` constant; cleared when state recovers or search overlay opens | `test_auto_hint_toast_clears_after_recovery` |
| BUG-012 | P2 | Tiny terminals render a 60x20 garble spilling over the real layout | Run in a 40x15 window | `render_once` clamped `cols=60, rows=20` | Clamp removed; spectrum skipped when <3 rows fit below the progress bar | `test_tiny_terminal_renders_at_true_size` |
| BUG-013 | P1 | Search status says 'No results' although N tracks were queued; Enter double-queues the same query | Daemon search path (results never populated locally) | Picker `results==[]` after daemon enqueue hits the no-results branch; Enter with no selection re-fires search | `_search_note` ('queued N — added to queue') preferred in status line; cleared on query edit; Enter dismisses instead of re-firing | 3 tests: queued note, Enter guard, note clears on typing |
| BUG-014 | P2 | Whitespace-only yt-dlp failure output crashes with IndexError instead of SourcesError | `_run_ytdlp` with stderr `' \n\t\n'` | `.strip().splitlines()[-1]` on an empty list | Guard: fall back to 'yt-dlp failed' | `test_ytdlp_whitespace_only_failure_raises_sources_error` |
| BUG-015 | P2 | Header 'M Y X' loses accent+bold; toast and controls hint collide on rows-2 | Render any frame | Two puts on row 0 (second strips first's styles); toast and controls share rows-2 | Single-put header; toast wins rows-2, controls only when no toast | `test_header_keeps_brand_accent_and_bold`, `test_toast_wins_over_controls_hint_row` |

## Usability Changes (what users will notice)

- Seed URL playback now shows correct track title and artist instead of raw CDN query string
- `myx --state` correctly reports `playing: false` when daemon is dead
- Headless controls (`myx --quit`, `myx --skip`, `myx --prev`, `myx --toggle`, `myx --vol N`) work without TUI
- `--no-lyrics` flag (from preserved patch) works for battery/memory savings

## Known Remaining Issues

- Footer/long-line clipping: content wider than the terminal keeps its left portion (tail loss is by design; a full ansi-aware splice renderer was deliberately not built)
- Partial escape-key sequences (e.g. `\x1b[1;5C` from modified arrows) leave one junk char that can auto-open search — unproven real-world impact, not fixed
- Spectrum is skipped entirely when fewer than 3 rows fit below the progress bar (rows <= ~15)
- Divider/`_draw_queue_full` row styles can still lose colors across multiple puts on one row (same class as BUG-015, not yet swept)

---
*Audit started: 2026-09-28*

## Audit Round 2 (2026-09-28)

BUG-004..BUG-015 found and fixed with red-verified hermetic regression tests; see table. Spectrum wiring (BUG-005/006) also fixed peak-hold initialization.
