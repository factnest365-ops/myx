"""For You home screen model for MYX.

Pure logic: no network, no terminal, no rendering. Given a listening history and
a favourites list, this module decides *what* the home screen shows and in what
order. The renderer draws the result; it does not decide it.

Storage is a small JSON file under the MYX config dir. Writes are atomic and
corruption is survivable — a corrupt library must never stop the app from
starting, it just starts empty and says so.

Design notes:

- History entries are ranked by recency and play count, then by how much the
  artist already appears elsewhere in the shelf, so a home screen full of one
  artist is not a recommendation.
- Every derived track carries the action needed to play it, so the renderer
  can bind a click without inventing a lookup.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# How many entries the history keeps before the oldest are trimmed.
HISTORY_LIMIT = 400
# How many plays before a track counts as a favourite candidate.
LIKE_THRESHOLD = 3

GENRE_STATIONS: tuple[dict[str, str], ...] = (
    {"id": "chill", "label": "Chill", "query": "chill lofi beats radio"},
    {"id": "focus", "label": "Focus", "query": "deep focus concentration music"},
    {"id": "workout", "label": "Workout", "query": "high energy workout hits"},
    {"id": "party", "label": "Party", "query": "party anthems hits"},
    {"id": "retro", "label": "Retro", "query": "80s 90s retro classics"},
    {"id": "jazz", "label": "Jazz", "query": "smooth jazz radio"},
    {"id": "sleep", "label": "Sleep", "query": "sleep ambient calm music"},
    {"id": "rock", "label": "Rock", "query": "classic rock radio"},
)

# Shown when there is no history yet, so a first-run home screen is not blank.
COLD_START_TRACKS: tuple[dict[str, str], ...] = (
    {"id": "cold-1", "title": "Lofi beats to study to", "artist": "Lofi Girl", "kind": "station"},
    {"id": "cold-2", "title": "Deep focus radio", "artist": "Ambient", "kind": "station"},
    {"id": "cold-3", "title": "Today's hits", "artist": "YouTube Music", "kind": "station"},
)


def library_path() -> Path:
    """Where the library lives. Honours MYX_HOME for tests and sandboxes."""
    base = os.environ.get("MYX_HOME")
    if base:
        return Path(base).expanduser() / "library.json"
    return Path.home() / ".config" / "myx" / "library.json"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """One thing the user has played."""

    id: str
    title: str = ""
    artist: str = ""
    plays: int = 1
    last_played: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "plays": self.plays,
            "last_played": self.last_played,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HistoryEntry | None:
        vid = str(raw.get("id") or "").strip()
        if not vid:
            return None
        try:
            plays = int(raw.get("plays") or 1)
        except (TypeError, ValueError):
            plays = 1
        try:
            last = float(raw.get("last_played") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        return cls(
            id=vid,
            title=str(raw.get("title") or ""),
            artist=str(raw.get("artist") or ""),
            plays=max(1, plays),
            last_played=last,
        )


@dataclass
class Track:
    """A playable card on the home screen.

    `action` is what a click does; `keys` is how the keyboard reaches it. Both
    are always populated, so a card can never be drawn without a way to play it.
    """

    id: str
    title: str
    artist: str = ""
    kind: str = "track"  # track | station | shelf
    query: str = ""
    reason: str = ""  # why this was recommended
    action: str = "play_id"
    keys: tuple[str, ...] = field(default_factory=lambda: ("enter",))

    def label(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title


@dataclass
class Shelf:
    """A titled row of cards on the home screen."""

    id: str
    title: str
    subtitle: str = ""
    tracks: list[Track] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.tracks)


@dataclass
class Library:
    """Favourites, history, and recently-played artists."""

    history: list[HistoryEntry] = field(default_factory=list)
    favourites: list[str] = field(default_factory=list)
    recent_queries: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.history or self.favourites)

    # -- mutation --
    def record_play(
        self,
        track_id: str,
        title: str = "",
        artist: str = "",
        now: float | None = None,
    ) -> None:
        """Record a play, merging into an existing entry for the same id."""
        vid = str(track_id or "").strip()
        if not vid:
            return
        stamp = now if now is not None else time.time()
        for entry in self.history:
            if entry.id == vid:
                entry.plays += 1
                entry.last_played = stamp
                # Late-arriving metadata should still win if it is better.
                entry.title = title or entry.title
                entry.artist = artist or entry.artist
                return
        self.history.append(
            HistoryEntry(id=vid, title=title, artist=artist, plays=1, last_played=stamp)
        )
        self.history.sort(key=lambda e: e.last_played, reverse=True)
        self.history = self.history[:HISTORY_LIMIT]

    def toggle_favourite(self, track_id: str) -> bool:
        """Add or remove a favourite. Returns True when now favourited."""
        vid = str(track_id or "").strip()
        if not vid:
            return False
        if vid in self.favourites:
            self.favourites.remove(vid)
            return False
        self.favourites.append(vid)
        return True

    def is_favourite(self, track_id: str) -> bool:
        return str(track_id) in self.favourites

    def record_query(self, query: str, now: float | None = None) -> None:
        q = str(query or "").strip()
        if not q:
            return
        if q in self.recent_queries:
            self.recent_queries.remove(q)
        self.recent_queries.insert(0, q)
        self.recent_queries = self.recent_queries[:10]

    # -- persistence --
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "history": [e.to_dict() for e in self.history],
            "favourites": list(self.favourites),
            "recent_queries": list(self.recent_queries),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Library:
        """Parse stored data, skipping anything malformed rather than failing.

        A library written by a newer version, or a half-written file, must not
        stop the app from opening.
        """
        if not isinstance(raw, dict):
            return cls()
        history: list[HistoryEntry] = []
        seen: set[str] = set()
        # A hand-edited or half-written file can put a non-list where a list
        # belongs. `load` promises to fall back to empty on any problem, so
        # every read here is type-checked rather than assumed.
        raw_history = raw.get("history")
        for item in raw_history if isinstance(raw_history, list) else []:
            if not isinstance(item, dict):
                continue
            entry = HistoryEntry.from_dict(item)
            if entry is not None and entry.id not in seen:
                seen.add(entry.id)
                history.append(entry)
        raw_favs = raw.get("favourites")
        favourites = [
            str(v)
            for v in (raw_favs if isinstance(raw_favs, list) else [])
            if isinstance(v, (str, int)) and str(v).strip()
        ]
        # Preserve order, drop duplicates.
        uniq_favs = list(dict.fromkeys(favourites))
        raw_queries = raw.get("recent_queries")
        queries = [
            str(q).strip()
            for q in (raw_queries if isinstance(raw_queries, list) else [])
            if isinstance(q, (str, int)) and str(q).strip()
        ]
        return cls(
            history=history[:HISTORY_LIMIT],
            favourites=uniq_favs,
            recent_queries=list(dict.fromkeys(queries))[:10],
        )

    def save(self, path: Path | None = None) -> None:
        """Atomically write the library. Never raises on IO failure."""
        target = path or library_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.to_dict(), indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".library-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp, target)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError):
            # A library that cannot be written is not worth losing the app over.
            return

    @classmethod
    def load(cls, path: Path | None = None) -> Library:
        """Read the library, falling back to empty on any problem."""
        target = path or library_path()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        return cls.from_dict(raw)


# ---------------------------------------------------------------------------
# shelves
# ---------------------------------------------------------------------------

# Collaboration separators. The bare `x` must be surrounded by spaces: a
# case-insensitive `xX` with no boundary truncates ordinary names like
# "Artist" down to "art".
_ARTIST_SPLIT = re.compile(
    r"\s+[x×]\s+|"  # spaced x, the usual collaboration marker
    r"\s*[·•]\s*|"  # centred dot separators
    r"\s+feat\.?\s+.*$"  # featured-artist suffix
    r"|\s+with\s+.*$",
    re.IGNORECASE,
)


def artist_key(name: str) -> str:
    """Normalise an artist string for grouping (case, spacing, collabs)."""
    text = _ARTIST_SPLIT.split(str(name or "").strip())[0]
    return re.sub(r"\s+", " ", text).strip().lower()


def _score(entry: HistoryEntry, now: float) -> float:
    """Recency-weighted play count. Recency decays over ~14 days."""
    age_days = max(0.0, (now - entry.last_played) / 86400.0)
    recency = 1.0 / (1.0 + age_days / 14.0)
    return (entry.plays ** 1.5) * recency


def _reason(entry: HistoryEntry, now: float) -> str:
    age_hours = max(0.0, (now - entry.last_played) / 3600.0)
    if age_hours < 6:
        return "picked up again"
    if entry.plays >= LIKE_THRESHOLD:
        return "on repeat"
    if entry.artist:
        return f"more {artist_key(entry.artist)}"
    return "for you"


def recently_played_artists(history: Iterable[HistoryEntry], count: int = 3) -> list[str]:
    """Distinct artist keys from the most recent plays, most recent first."""
    return [artist_key(name) for name, _ in recently_played_artist_names(history, count)]


def recently_played_artist_names(
    history: Iterable[HistoryEntry], count: int = 3
) -> list[tuple[str, str]]:
    """(normalised key, display name) for the most recent distinct artists.

    The display name keeps the artist's own capitalisation: normalising to a
    key and then title-casing it for the UI turns "The xx" into "The Xx".
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for entry in sorted(history, key=lambda e: e.last_played, reverse=True):
        key = artist_key(entry.artist)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((key, str(entry.artist).strip()))
        if len(out) >= count:
            break
    return out


def for_you_shelves(library: Library, now: float | None = None, limit: int = 12) -> list[Shelf]:
    """Derive the home screen shelves from a library.

    Returns [] when there is nothing *personal* to recommend — no plays and no
    favourites — so the caller can fall back to `cold_start_shelves()` instead
    of rendering a near-empty page whose only content is the stations row.
    """
    if not library.history and not library.favourites:
        return []
    stamp = now if now is not None else time.time()
    shelves: list[Shelf] = []

    # Favourites first: the most deliberate signal a user gives us.
    fav_ids = set(library.favourites)
    fav_tracks = [
        Track(
            id=e.id,
            title=e.title or "Unknown",
            artist=e.artist,
            reason="liked",
            action="play_id",
        )
        for e in library.history
        if e.id in fav_ids
    ]
    if fav_tracks:
        shelves.append(Shelf("favourites", "Liked", f"{len(fav_tracks)} tracks", fav_tracks))

    # Recently played, ranked, and spread across artists.
    recent = sorted(library.history, key=lambda e: _score(e, stamp), reverse=True)
    seen_ids: set[str] = set()
    seen_artists: dict[str, int] = {}
    recent_tracks: list[Track] = []
    for entry in recent:
        if entry.id in seen_ids:
            continue
        key = artist_key(entry.artist)
        # Allow a couple of tracks per artist, never a wall of one artist.
        if key and seen_artists.get(key, 0) >= 2:
            continue
        seen_ids.add(entry.id)
        if key:
            seen_artists[key] = seen_artists.get(key, 0) + 1
        recent_tracks.append(
            Track(
                id=entry.id,
                title=entry.title or "Unknown",
                artist=entry.artist,
                reason=_reason(entry, stamp),
                action="play_id",
            )
        )
        if len(recent_tracks) >= limit:
            break
    if recent_tracks:
        shelves.append(
            Shelf(
                "recent",
                "Jump back in",
                "from your listening",
                recent_tracks,
            )
        )

    # Artists we keep hearing, as one-tap entries.
    artist_names = recently_played_artist_names(library.history, 4)
    if artist_names:
        shelves.append(
            Shelf(
                "artists",
                "Your artists",
                "tap to open",
                [
                    Track(
                        id=f"artist:{key}",
                        title=display,
                        artist="",
                        kind="artist",
                        query=display,
                        action="open_artist",
                    )
                    for key, display in artist_names
                ],
            )
        )

    # One-tap genre stations — always available, even on a cold start.
    shelves.append(
        Shelf(
            "stations",
            "Stations",
            "one tap, endless",
            [
                Track(
                    id=f"station:{s['id']}",
                    title=s["label"],
                    artist="",
                    kind="station",
                    query=s["query"],
                    action="play_station",
                )
                for s in GENRE_STATIONS
            ],
        )
    )
    return shelves


def cold_start_shelves() -> list[Shelf]:
    """The home screen a brand-new user sees."""
    return [
        Shelf(
            "welcome",
            "Welcome to MYX",
            "pick a station to start",
            [
                Track(
                    id=t["id"],
                    title=t["title"],
                    artist=t["artist"],
                    kind="station",
                    query=t["title"],
                    action="play_station",
                )
                for t in COLD_START_TRACKS
            ],
        ),
        Shelf(
            "stations",
            "Stations",
            "one tap, endless",
            [
                Track(
                    id=f"station:{s['id']}",
                    title=s["label"],
                    artist="",
                    kind="station",
                    query=s["query"],
                    action="play_station",
                )
                for s in GENRE_STATIONS
            ],
        ),
    ]


def home_shelves(library: Library, now: float | None = None) -> list[Shelf]:
    """The shelves to draw: derived ones when we have history, else cold start."""
    shelves = for_you_shelves(library, now=now)
    if not shelves:
        return cold_start_shelves()
    return shelves
