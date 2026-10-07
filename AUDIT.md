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
| BUG-016 | P2 | Queue tracks show the current track's thumbnail or no thumbnail | Queue contains tracks with different thumbnails | `write_state()` checked `current.thumbnail` while serializing every queue item | Read each queue item's thumbnail independently | `test_queue_thumbnail_uses_each_item_thumbnail` |
| BUG-017 | P1 | Empty JSON-RPC batches receive no response | Send `[]` to `myx mcp --stdio` | Batch loop emitted only non-empty response lists | Return `-32600 Invalid Request` for an empty batch | `test_mcp_empty_batch_is_invalid_request` |
| BUG-018 | P1 | Malformed `tools/call` params produce the wrong failure path | Send `tools/call` with array params | Dispatcher assumed params and arguments were mappings | Validate params, tool name, and arguments before dispatch | `test_mcp_tools_call_rejects_non_object_params` |
| BUG-019 | P0 | Live daemon appears dead during slow lyrics lookup | Block `sources.lyrics()` while polling state | Lyrics lookup ran inline in `write_state()` and readers used mtime as liveness | Fetch lyrics in a background thread; use daemon reachability only | `test_write_state_does_not_wait_for_lyrics`, `test_cli_state_keeps_old_state_for_live_daemon`, `test_mcp_state_accepts_old_state_from_live_daemon` |
| BUG-020 | P1 | MCP emits an unsolicited initialized notification | Send an `initialize` request | Server wrote `notifications/initialized`, which is a client notification | Return only the initialize response | `test_initialize_response_precedes_no_server_notification` |
| BUG-021 | P1 | Non-object state JSON reaches MCP tool `.get()` calls | Write a JSON list to the state file | `_read_state()` returned valid non-dict JSON directly | Return the standard empty state for non-object payloads | `test_mcp_state_rejects_non_object_json` |
| BUG-022 | P0 | State polling reports a live daemon as dead while its command server is busy | Block command handling, then poll `--state` or MCP state | Liveness probe waited for a command reply with a short timeout | Treat successful command-socket connect as reachability; do not wait for a reply | `test_cli_liveness_does_not_wait_for_command_reply`, `test_mcp_liveness_does_not_wait_for_command_reply` |

## Usability Changes (what users will notice)

- Seed URL playback now shows correct track title and artist instead of raw CDN query string
- `myx --state` correctly reports `playing: false` when daemon is dead
- Headless controls (`myx --quit`, `myx --skip`, `myx --prev`, `myx --toggle`, `myx --vol N`) work without TUI
- `--no-lyrics` flag (from preserved patch) works for battery/memory savings
- State polling remains truthful during slow lyrics and command handling.
- MCP rejects malformed state payloads and malformed JSON-RPC calls with safe fallbacks/errors.

## Known Remaining Issues

- Footer/long-line clipping: content wider than the terminal keeps its left portion (tail loss is by design; a full ansi-aware splice renderer was deliberately not built)
- Partial escape-key sequences (e.g. `\x1b[1;5C` from modified arrows) leave one junk char that can auto-open search — unproven real-world impact, not fixed
- Spectrum is skipped entirely when fewer than 3 rows fit below the progress bar (rows <= ~15)
- Divider/`_draw_queue_full` row styles can still lose colors across multiple puts on one row (same class as BUG-015, not yet swept)
- Ruff broad-exception and try/except/pass findings predate this audit and are not part of the project CI contract; leave for a separate error-handling cleanup.

---
*Audit started: 2026-09-28*

## Audit Round 2 (2026-09-28)

BUG-004..BUG-022 were audited with focused hermetic regression tests; BUG-008 was disproved and dropped, while the remaining confirmed findings were fixed. Spectrum wiring (BUG-005/006) also fixed peak-hold initialization.

## Audit Round 3 — For You launch surface (2026-10-07)

Scope: make `myx` openable by someone who does not know YouTube URLs or the
keybinds. Non-negotiable rule from the captain: **nothing may look clickable and
not be.**

### Findings

| ID | Severity | Symptom | Reproduction | Root Cause | Fix | Test |
|----|----------|---------|--------------|------------|-----|------|
| BUG-023 | P1 | Search was a bottom overlay summoned by `/`; the app opened on an empty now-playing panel | `myx` with no arguments | There was no launch surface; search was a modal over the player | `myx` with no seed and no `--search` opens the For You home screen; the search bar is drawn on every frame with explicit focus state | `test_bare_myx_opens_for_you`, `test_home_shows_the_search_bar_on_every_frame` |
| BUG-024 | P1 | Terminal input was keyboard-only, so every control needed a memorised key | Any mouse movement in the TUI | No mouse reporting, no hit regions | SGR (1006) decoding, a per-frame `InteractiveMap`, wheel scroll and drag-to-seek | `test_mouse.py`, `test_clicking_a_station_card_sends_search`, `test_drag_leaving_the_bar_still_seeks` |
| BUG-025 | P0 | Cards were keyed by bare track id, so a favourited track in two shelves had one copy silently unclickable | Favourite a track, open For You, click its Liked card | `card_rects` keyed by `track.id`; the later shelf overwrote the earlier rect | Keys are shelf-qualified (`card:{shelf}/{track}`) | `test_card_appearing_in_two_shelves_stays_clickable_in_both` |
| BUG-026 | P0 | Shelves that did not fit the row budget vanished with no indication; stations rendered zero cards at 100x30 | For You at 100x30 | `draw_home` broke out of the loop when rows ran out | Dropped shelves are reported on `HomeView.dropped_shelf_ids` and reachable by scrolling | `test_every_shelf_is_reachable_by_scrolling`, `test_dropped_shelves_are_reported` |
| BUG-027 | P1 | The chevron on a shelf header fired a no-op command | Click any section header | Chevron was drawn with a placeholder `noop` action | It opens that shelf, so the affordance means what it looks like | `test_clicking_the_chevron_opens_the_shelf` |
| BUG-028 | P1 | Controls acted on mouse *release*, so dragging off a control cancelled the click | Press and drag off a card | `handle_mouse` deferred the action to `RELEASE` | Actions fire on `PRESS`; release only ends the gesture | `test_clicking_a_station_card_sends_search` |
| BUG-029 | P2 | A click past the right edge of the progress bar did nothing | Click the right end of the seek bar on a wide terminal | Hit test required the point to be inside the rect | The seek bar accepts a press anywhere on its row and clamps to the end | `test_seek_fraction_is_clamped` |
| BUG-030 | P2 | `artist_key()` split collaborations on a bare case-insensitive `xX`, truncating ordinary names (`Artist` -> `art`, `Max` -> `ma`) | Any artist name containing `x` or `X` | Regex alternation had no word boundary | The `x` marker requires surrounding spaces | `test_artist_key_does_not_truncate_ordinary_names` |
| BUG-031 | P1 | A bare library always fell back to a page containing only the stations row, so the cold-start screen could never show | First run, For You | `for_you_shelves` always appended the stations shelf | Returns `[]` with no history and no favourites, so cold start wins | `test_for_you_returns_nothing_for_a_bare_library` |
| BUG-032 | P2 | The trailing `type to search` hint overwrote the placeholder on a narrow terminal, rendering `Search songs,type to searchl` | For You at 40 columns | Hint drawn without reserving its columns | Hint width is reserved before the field text is sized, and dropped if it cannot fit | `test_search_bar_hint_never_overwrites_the_placeholder` |
| BUG-033 | P2 | "Your artists" rendered `The xx` as `The Xx` | For You with history | Labels built with `.title()` on the normalised key | Shelves carry the artist's own capitalisation; the key stays an id | `test_artist_shelf_keeps_the_artists_own_capitalisation` |
| BUG-034 | P0 | The now-playing progress bar was drawn but never registered, so clicking it did nothing — a dead affordance shipped despite BUG-029 claiming it fixed | Now Playing, click the progress bar | `handle_mouse` looked up `seek:main`, but nothing ever added that region; the seek tests registered one by hand and so never caught it | The bar registers itself where it is drawn, against a real seek action | `test_now_playing_registers_the_seek_bar`, `test_clicking_the_real_seek_bar_seeks`, `test_dragging_the_real_seek_bar_seeks` |
| BUG-035 | P0 | Submitting a search from For You did nothing visible: the query landed in the bar and the screen looked inert | Type in the search bar, press Enter | `_submit_search` set `overlay="search"` but stayed on the home surface, whose `render_once` branch returns before overlays are composited | Submitting leaves the home surface for the player layout, where the results picker is drawn | `test_submitted_search_results_are_actually_visible`, `test_submit_leaves_the_home_surface` |

> BUG-034 and BUG-035 were found *after* the first pass reported itself done,
> by re-running the acceptance criteria against the real rendered screen rather
> than against the unit tests. BUG-029 had been recorded as fixed when the code
> path it described was unreachable; BUG-035 was a defect the test suite could
> not see because it asserted on state rather than on what was drawn. Both are
> recorded here because the lesson is the point: a green suite is not evidence
> that a control is reachable, only evidence that the tests reach it.

### Design system

Screens no longer invent padding. `myx/design.py` owns a cell rhythm
(`space()`), semantic type roles (`Role`/`TypeScale`), and breakpoints
(`for_size()`, `Metrics`). Rows are an independent axis from width, so a
200x8 window classifies as wide *and* short rather than being mistaken for a
small one. `myx/ansi.py` holds the ANSI and display-width primitives shared by
the renderer and the surface.

### The no-dead-affordances rule

Enforced structurally rather than by convention: `SurfaceDrawer._element()` is
the only way to draw something interactive and refuses to register a region
with no action or no area. `InteractiveMap.dead_affordances()` reports anything
registered but inert, and `missing_keyboard_parity()` reports controls
reachable only by mouse. The home frame asserts both are empty at 40, 60, 120
and 80x12.

### Usability Changes (what users will notice)

- `myx` with no arguments opens For You: shelves seeded from listening history,
  one-tap genre stations, favourites, and a persistent search bar
- Search is a real field with focus state; typing anywhere focuses it
- Mouse works: clicks, wheel scroll, drag-to-seek, and reporting is turned on at
  start and off at exit so no terminal is left in a reporting state
- Every visible control is clickable *and* keyboard-reachable

### Known Remaining Issues

- `myx/design.py` is currently used by the For You surface; the older
  now/lyrics/queue views still carry their own hand-rolled padding. Migrating
  them is follow-up work, not a regression.
- The home screen shows a fixed window of shelves; there is no paging UI beyond
  scroll, so a very short terminal still needs scrolling to reach the last shelf
  (reported on screen as `▼ N more — scroll`).
- `ansi.py` duplicates the colour/width helpers that `tui.py` still defines.
  `tui.py` was left untouched to keep this change surgical; the duplication
  should collapse once the older views move to the shared module.
- The four ruff `F401` findings in `tests/test_jit_queue.py` and
  `tests/test_mcp_fast.py` predate this work and are outside its scope. Every
  file added or changed here is ruff-clean under `--select E9,F`.

### Verification

343 tests pass: the 120 pre-existing ones plus 223 added across
`test_design.py`, `test_mouse.py`, `test_library.py`, `test_surface.py`,
`test_wireup.py`, and `test_tty_mouse.py`. The Round 2 regression tests for
JSON-RPC conformance, daemon lifecycle, CJK/emoji cell widths, spectrum wiring,
and non-blocking ffmpeg taps all still pass.

The mouse-reporting lifecycle is verified by `test_tty_mouse.py`, which drives
`tui.run()` through a real PTY and inspects the bytes it writes. Asserting on the
`ENABLE_MOUSE`/`DISABLE_MOUSE` constants would only prove the constants exist;
that lifecycle is gated on `isatty()` and cannot be observed any other way.
