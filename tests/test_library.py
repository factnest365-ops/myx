"""Tests for the For You home screen model."""

import json

from myx import library as lib
from myx.library import (
    HISTORY_LIMIT,
    Library,
    artist_key,
    cold_start_shelves,
    for_you_shelves,
    home_shelves,
    recently_played_artists,
)

NOW = 1_700_000_000.0
DAY = 86400.0


def seeded_library() -> Library:
    """A library with varied artists, ages, and play counts."""
    liby = Library()
    liby.record_play("t1", "Blinding Lights", "The Weeknd", now=NOW)
    liby.record_play("t2", "After Hours", "The Weeknd", now=NOW - DAY)
    liby.record_play("t3", "Midnight City", "M83", now=NOW - 2 * DAY)
    liby.record_play("t4", "Intro", "The xx", now=NOW - 3 * DAY)
    liby.record_play("t5", "Nightcall", "Kavinsky", now=NOW - 5 * DAY)
    return liby


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_record_play_adds_an_entry():
    liby = Library()
    liby.record_play("abc", "Song", "Artist", now=NOW)
    assert len(liby.history) == 1
    assert liby.history[0].plays == 1
    assert liby.history[0].title == "Song"


def test_repeat_plays_merge_and_count():
    liby = Library()
    liby.record_play("abc", "Song", "Artist", now=NOW)
    liby.record_play("abc", now=NOW + 1)
    assert len(liby.history) == 1, "the same track must not be duplicated"
    assert liby.history[0].plays == 2


def test_repeat_play_keeps_existing_metadata():
    liby = Library()
    liby.record_play("abc", "Song", "Artist", now=NOW)
    liby.record_play("abc", now=NOW + 1)
    assert liby.history[0].title == "Song"
    assert liby.history[0].artist == "Artist"


def test_blank_track_id_is_ignored():
    liby = Library()
    liby.record_play("", "Song", "Artist", now=NOW)
    liby.record_play("   ", now=NOW)
    assert liby.history == []


def test_history_is_capped():
    liby = Library()
    for i in range(HISTORY_LIMIT + 50):
        liby.record_play(f"id{i}", f"T{i}", "A", now=NOW - i)
    assert len(liby.history) == HISTORY_LIMIT


def test_most_recent_plays_survive_the_trim():
    liby = Library()
    for i in range(HISTORY_LIMIT + 10):
        liby.record_play(f"id{i}", f"T{i}", "A", now=NOW - i)
    assert liby.history[0].id == "id0", "newest first"


# ---------------------------------------------------------------------------
# favourites
# ---------------------------------------------------------------------------


def test_toggle_favourite_on_and_off():
    liby = Library()
    assert liby.toggle_favourite("x") is True
    assert liby.is_favourite("x")
    assert liby.toggle_favourite("x") is False
    assert not liby.is_favourite("x")


def test_favourite_of_blank_id_is_rejected():
    liby = Library()
    assert liby.toggle_favourite("") is False
    assert liby.favourites == []


def test_recent_queries_dedupe_and_reorder():
    liby = Library()
    liby.record_query("a")
    liby.record_query("b")
    liby.record_query("a")
    assert liby.recent_queries == ["a", "b"]
    assert liby.record_query("  ") is None


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_roundtrip_through_disk(tmp_path):
    path = tmp_path / "library.json"
    liby = seeded_library()
    liby.toggle_favourite("t3")
    liby.record_query("m83 radio")
    liby.save(path)

    loaded = Library.load(path)
    assert len(loaded.history) == len(liby.history)
    assert loaded.favourites == ["t3"]
    assert loaded.recent_queries == ["m83 radio"]


def test_save_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deep" / "library.json"
    Library().save(path)
    assert path.exists()


def test_load_missing_file_is_empty(tmp_path):
    assert Library.load(tmp_path / "nope.json").is_empty()


def test_load_corrupt_json_is_empty(tmp_path):
    """A half-written library must not stop the app from opening."""
    path = tmp_path / "library.json"
    path.write_text("{not json at all")
    assert Library.load(path).is_empty()


def test_load_non_object_json_is_empty(tmp_path):
    path = tmp_path / "library.json"
    path.write_text("[1, 2, 3]")
    assert Library.load(path).is_empty()


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "library.json"
    seeded_library().save(path)
    strays = [p.name for p in tmp_path.iterdir() if p.name != "library.json"]
    assert strays == [], f"temp files left behind: {strays}"


def test_from_dict_skips_malformed_entries():
    liby = Library.from_dict(
        {
            "history": [
                {"id": "ok", "title": "T", "plays": "not-a-number", "last_played": "nope"},
                {"title": "no id"},
                "not a dict",
                {"id": "ok2"},
            ],
            "favourites": ["a", "a", "b", 7],
        }
    )
    assert [e.id for e in liby.history] == ["ok", "ok2"]
    assert liby.history[0].plays == 1, "a bad play count must not crash or zero out"
    assert liby.favourites == ["a", "b", "7"]


def test_from_dict_dedupes_history_ids():
    liby = Library.from_dict({"history": [{"id": "x"}, {"id": "x"}]})
    assert len(liby.history) == 1


def test_from_dict_handles_missing_keys():
    liby = Library.from_dict({})
    assert liby.is_empty()


def test_from_dict_rejects_none():
    assert Library.from_dict(None).is_empty()


def test_save_failure_does_not_raise(tmp_path):
    """A read-only config dir must not crash the app."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        Library().save(blocked / "library.json")  # must not raise
    finally:
        blocked.chmod(0o700)


def test_saved_file_is_valid_json(tmp_path):
    path = tmp_path / "library.json"
    seeded_library().save(path)
    assert isinstance(json.loads(path.read_text()), dict)


# ---------------------------------------------------------------------------
# artist normalisation
# ---------------------------------------------------------------------------


def test_artist_key_normalises_case_and_space():
    assert artist_key("The Weeknd") == "the weeknd"
    assert artist_key("  THE   WEEKND ") == "the weeknd"


def test_artist_key_splits_collaborations():
    assert artist_key("DAOKO × 米津玄師") == "daoko"
    assert artist_key("Artist feat. Someone") == "artist"
    assert artist_key("Alex x Charli") == "alex"
    assert artist_key("One · Two") == "one"


def test_artist_key_does_not_truncate_ordinary_names():
    """A bare case-insensitive `xX` truncates names like 'Artist' to 'art'."""
    assert artist_key("Artist") == "artist"
    for name in ("Max", "Xylophone", "Box", "Pixies", "Extreme", "Alex"):
        assert artist_key(name) == name.lower(), f"{name} was mangled"


def test_artist_key_handles_blank():
    assert artist_key("") == ""
    assert artist_key(None) == ""


def test_recently_played_artists_are_distinct_and_ordered():
    liby = seeded_library()
    artists = recently_played_artists(liby.history, count=3)
    assert artists[0] == "the weeknd", "most recent play leads"
    assert len(set(artists)) == len(artists), "artists must not repeat"


# ---------------------------------------------------------------------------
# shelves
# ---------------------------------------------------------------------------


def test_shelves_always_include_stations():
    shelves = home_shelves(seeded_library(), now=NOW)
    ids = [s.id for s in shelves]
    assert "stations" in ids


def test_favourites_become_their_own_shelf():
    liby = seeded_library()
    liby.toggle_favourite("t3")
    shelves = {s.id: s for s in for_you_shelves(liby, now=NOW)}
    assert "favourites" in shelves
    assert [t.id for t in shelves["favourites"].tracks] == ["t3"]


def test_no_favourites_shelf_when_nothing_is_liked():
    shelves = {s.id for s in for_you_shelves(seeded_library(), now=NOW)}
    assert "favourites" not in shelves


def test_recent_shelf_respects_the_limit():
    liby = Library()
    for i in range(30):
        liby.record_play(f"id{i}", f"T{i}", f"Artist{i}", now=NOW - i * 60)
    shelves = {s.id: s for s in for_you_shelves(liby, now=NOW, limit=5)}
    assert len(shelves["recent"].tracks) == 5


def test_recent_shelf_is_not_one_artist_on_repeat():
    """Five plays of the same artist must not fill the whole shelf."""
    liby = Library()
    for i in range(5):
        liby.record_play(f"w{i}", f"Weeknd {i}", "The Weeknd", now=NOW - i)
    liby.record_play("m1", "Midnight City", "M83", now=NOW - 10)
    tracks = for_you_shelves(liby, now=NOW)[0].tracks
    weeknd = sum(1 for t in tracks if artist_key(t.artist) == "the weeknd")
    assert weeknd <= 2, f"artist flooding: {weeknd} of {len(tracks)} cards"


def test_shelf_tracks_always_carry_an_action():
    """A card that cannot be played is a dead affordance by construction."""
    for shelf in home_shelves(seeded_library(), now=NOW):
        for track in shelf.tracks:
            assert track.action, f"{shelf.id}/{track.id} has no action"
            assert track.keys, f"{shelf.id}/{track.id} has no keybinding"


def test_station_tracks_carry_a_playable_query():
    shelves = {s.id: s for s in home_shelves(seeded_library(), now=NOW)}
    for track in shelves["stations"].tracks:
        assert track.action == "play_station"
        assert track.query, "a station with no query cannot start a stream"


def test_cold_start_never_renders_blank():
    shelves = home_shelves(Library(), now=NOW)
    assert shelves, "a brand-new user must still get shelves"
    assert [s.id for s in shelves] == ["welcome", "stations"]


def test_for_you_returns_nothing_for_a_bare_library():
    """No plays and no favourites means there is nothing personal to show."""
    assert for_you_shelves(Library(), now=NOW) == []


def test_for_you_shows_shelves_from_favourites_alone():
    liby = Library()
    liby.favourites = ["never-played-id"]
    shelves = for_you_shelves(liby, now=NOW)
    assert shelves, "a favourite with no play history still deserves a home screen"
    assert "stations" in {s.id for s in shelves}


def test_cold_start_cards_are_playable():
    for shelf in cold_start_shelves():
        for track in shelf.tracks:
            assert track.action == "play_station"
            assert track.query


def test_shelves_have_titles():
    for shelf in home_shelves(seeded_library(), now=NOW):
        assert shelf.title.strip(), f"shelf {shelf.id} has no title"


def test_artist_shelf_appears_with_history():
    liby = seeded_library()
    shelves = {s.id for s in for_you_shelves(liby, now=NOW)}
    assert "artists" in shelves


def test_empty_shelf_is_falsy():
    assert not lib.Shelf("x", "X")
    assert lib.Shelf("x", "X", tracks=[lib.Track(id="a", title="A")])


def test_recency_outranks_a_single_older_play():
    liby = Library()
    liby.record_play("many", "Many", "A", now=NOW - 10 * DAY)
    for _ in range(4):
        liby.record_play("few", "Few", "B", now=NOW)
    tracks = for_you_shelves(liby, now=NOW)[0].tracks
    assert tracks[0].id == "few", "played five times just now should lead"


def test_history_recorded_now_says_picked_up_again():
    liby = Library()
    liby.record_play("x", "X", "A", now=NOW)
    reason = for_you_shelves(liby, now=NOW)[0].tracks[0].reason
    assert reason == "picked up again"


def test_repeat_play_says_on_repeat():
    liby = Library()
    for _ in range(lib.LIKE_THRESHOLD + 1):
        liby.record_play("x", "X", "A", now=NOW - 10 * DAY)
    reason = for_you_shelves(liby, now=NOW)[0].tracks[0].reason
    assert reason == "on repeat"
