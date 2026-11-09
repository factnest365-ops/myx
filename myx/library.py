"""Persistent SQLite Library for MYX — Liked Songs, Playlists, History & Offline Cache.

Stdlib `sqlite3` only (zero external deps). Uses WAL journal mode, strict file
permissions (0o700 dir / 0o600 db), and thread-safe locking so PlayerDaemon,
CLI, and MCP tools can query/mutate library state safely.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_AUDIO_CACHE_MAX_BYTES = 500 * 1024 * 1024  # 500 MiB default offline cache cap


def default_data_dir() -> Path:
    if env_dir := os.environ.get("MYX_DATA_DIR"):
        return Path(env_dir)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "myx"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "myx"


def _clean_playlist_name(name: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(name or "")).strip()
    if not cleaned:
        raise ValueError("playlist name cannot be empty")
    return cleaned[:80]


def _row_to_track(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "duration": row["duration"],
        "thumb": row["thumb"],
    }
    keys = row.keys()
    if "play_count" in keys:
        d["play_count"] = row["play_count"]
    if "cached_audio_path" in keys and row["cached_audio_path"]:
        d["cached_audio_path"] = row["cached_audio_path"]
    if "lyrics_offset" in keys and row["lyrics_offset"]:
        d["lyrics_offset"] = row["lyrics_offset"]
    return d


class Library:
    """Thread-safe persistent SQLite store for MYX."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        if db_path == ":memory:":
            self.db_path: Path | None = None
            target = ":memory:"
        else:
            p = Path(db_path) if db_path is not None else (default_data_dir() / "library.db")
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if p.is_symlink():
                raise PermissionError(f"refusing to open symlinked library db: {p}")
            self.db_path = p
            target = str(p)

        self._conn = sqlite3.connect(target, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        if self.db_path is not None and self.db_path.exists():
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    duration REAL,
                    thumb TEXT,
                    cached_audio_path TEXT,
                    cached_audio_bytes INTEGER DEFAULT 0,
                    cached_at REAL,
                    lyrics_offset REAL DEFAULT 0.0,
                    play_count INTEGER DEFAULT 0,
                    last_played_at REAL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS likes (
                    video_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                    liked_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playlists (
                    name TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playlist_items (
                    playlist_name TEXT NOT NULL REFERENCES playlists(name) ON DELETE CASCADE,
                    video_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    added_at REAL NOT NULL,
                    PRIMARY KEY (playlist_name, video_id)
                );

                CREATE TABLE IF NOT EXISTS play_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    played_at REAL NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Tracks & Metadata
    # ------------------------------------------------------------------

    def upsert_track(self, track: dict[str, Any] | str) -> str:
        if isinstance(track, str):
            item: dict[str, Any] = {"id": track}
        else:
            item = dict(track)
        vid = str(item.get("id") or "").strip()
        if not vid:
            raise ValueError("track requires non-empty 'id'")
        now = time.time()
        title = item.get("title")
        artist = item.get("artist")
        dur = item.get("duration")
        thumb = item.get("thumb")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tracks (id, title, artist, duration, thumb, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = COALESCE(excluded.title, tracks.title),
                    artist = COALESCE(excluded.artist, tracks.artist),
                    duration = COALESCE(excluded.duration, tracks.duration),
                    thumb = COALESCE(excluded.thumb, tracks.thumb),
                    updated_at = excluded.updated_at
                """,
                (vid, title, artist, dur, thumb, now),
            )
        return vid

    def get_track(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (str(video_id),)
            ).fetchone()
            return _row_to_track(row) if row else None

    # ------------------------------------------------------------------
    # Likes (♥ Liked Songs)
    # ------------------------------------------------------------------

    def like(self, track: dict[str, Any] | str) -> bool:
        vid = self.upsert_track(track)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO likes (video_id, liked_at) VALUES (?, ?)",
                (vid, now),
            )
        return True

    def unlike(self, video_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM likes WHERE video_id = ?", (str(video_id),)
            )
            return cur.rowcount > 0

    def toggle_like(self, track: dict[str, Any] | str) -> bool:
        vid = str(track if isinstance(track, str) else track.get("id") or "").strip()
        if not vid:
            raise ValueError("track requires non-empty 'id'")
        with self._lock:
            if self.is_liked(vid):
                self.unlike(vid)
                return False
            self.like(track)
            return True

    def set_liked(self, track: dict[str, Any] | str, liked: bool) -> bool:
        vid = str(track if isinstance(track, str) else track.get("id") or "").strip()
        if not vid:
            raise ValueError("track requires non-empty 'id'")
        with self._lock:
            if liked:
                self.like(track)
                return True
            self.unlike(vid)
            return False

    def is_liked(self, video_id: str | None) -> bool:
        if not video_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM likes WHERE video_id = ?", (str(video_id),)
            ).fetchone()
            return row is not None

    def liked_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM likes").fetchone()
            return int(row["n"]) if row else 0

    def liked_tracks(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.*
                FROM likes l
                JOIN tracks t ON t.id = l.video_id
                ORDER BY l.liked_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [_row_to_track(r) for r in rows]

    # ------------------------------------------------------------------
    # Custom Playlists & M3U Import/Export
    # ------------------------------------------------------------------

    def create_playlist(self, name: str) -> str:
        clean = _clean_playlist_name(name)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO playlists (name, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (clean, now, now),
            )
        return clean

    def delete_playlist(self, name: str) -> bool:
        clean = _clean_playlist_name(name)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM playlists WHERE name = ?", (clean,)
            )
            return cur.rowcount > 0

    def list_playlists(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.name, p.updated_at, COUNT(pi.video_id) AS track_count
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_name = p.name
                GROUP BY p.name
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
            return [
                {
                    "name": r["name"],
                    "track_count": int(r["track_count"]),
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def add_to_playlist(
        self, name: str, tracks: dict[str, Any] | str | list[dict[str, Any] | str]
    ) -> int:
        clean = self.create_playlist(name)
        items = tracks if isinstance(tracks, list) else [tracks]
        added = 0
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_pos FROM playlist_items WHERE playlist_name = ?",
                (clean,),
            ).fetchone()
            pos = int(row["max_pos"]) + 1 if row else 0
            for item in items:
                try:
                    vid = self.upsert_track(item)
                except ValueError:
                    continue
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO playlist_items (playlist_name, video_id, position, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (clean, vid, pos, now),
                )
                if cur.rowcount > 0:
                    pos += 1
                    added += 1
            self._conn.execute(
                "UPDATE playlists SET updated_at = ? WHERE name = ?", (now, clean)
            )
        return added

    def replace_playlist_tracks(
        self, name: str, tracks: list[dict[str, Any] | str]
    ) -> int:
        clean = self.create_playlist(name)
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM playlist_items WHERE playlist_name = ?", (clean,)
            )
        return self.add_to_playlist(clean, tracks)

    def remove_from_playlist(self, name: str, video_id: str) -> bool:
        clean = _clean_playlist_name(name)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM playlist_items WHERE playlist_name = ? AND video_id = ?",
                (clean, str(video_id)),
            )
            return cur.rowcount > 0

    def get_playlist_tracks(self, name: str) -> list[dict[str, Any]]:
        clean = _clean_playlist_name(name)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.*
                FROM playlist_items pi
                JOIN tracks t ON t.id = pi.video_id
                WHERE pi.playlist_name = ?
                ORDER BY pi.position ASC
                """,
                (clean,),
            ).fetchall()
            return [_row_to_track(r) for r in rows]

    def export_m3u(self, name: str) -> str:
        tracks = (
            self.liked_tracks()
            if name.strip().lower() in ("liked", "liked songs")
            else self.get_playlist_tracks(name)
        )
        lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
        for t in tracks:
            dur = int(float(t.get("duration") or -1))
            artist = t.get("artist") or ""
            title = t.get("title") or t["id"]
            label = f"{artist} - {title}" if artist else str(title)
            lines.append(f"#EXTINF:{dur},{label}")
            lines.append(f"https://www.youtube.com/watch?v={t['id']}")
        return "\n".join(lines) + "\n"

    def import_m3u(self, name: str, m3u_text: str) -> int:
        entries: list[dict[str, Any]] = []
        pending_title: str | None = None
        pending_dur: float | None = None
        for raw in m3u_text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                m = re.match(r"#EXTINF:(-?\d+(?:\.\d+)?),(.*)", line)
                if m:
                    d = float(m.group(1))
                    pending_dur = d if d > 0 else None
                    pending_title = m.group(2).strip() or None
                continue
            if line.startswith("#"):
                continue
            m_vid = re.search(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{6,64})", line)
            vid = m_vid.group(1) if m_vid else (
                line if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", line) else None
            )
            if vid:
                entries.append(
                    {"id": vid, "title": pending_title, "duration": pending_dur}
                )
            pending_title = None
            pending_dur = None
        return self.add_to_playlist(name, entries)

    # ------------------------------------------------------------------
    # History, Top Played & Lyrics Sync Offsets
    # ------------------------------------------------------------------

    def record_play(self, track: dict[str, Any] | str) -> None:
        vid = self.upsert_track(track)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE tracks
                SET play_count = COALESCE(play_count, 0) + 1,
                    last_played_at = ?
                WHERE id = ?
                """,
                (now, vid),
            )
            self._conn.execute(
                "INSERT INTO play_history (video_id, played_at) VALUES (?, ?)",
                (vid, now),
            )
            # Keep history bounded at 1000 rows
            self._conn.execute(
                """
                DELETE FROM play_history
                WHERE seq NOT IN (
                    SELECT seq FROM play_history ORDER BY seq DESC LIMIT 1000
                )
                """
            )

    def recent_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.*, h.played_at
                FROM play_history h
                JOIN tracks t ON t.id = h.video_id
                ORDER BY h.seq DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [_row_to_track(r) for r in rows]

    def most_played(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tracks
                WHERE play_count > 0
                ORDER BY play_count DESC, last_played_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [_row_to_track(r) for r in rows]

    def set_lyrics_offset(self, video_id: str, offset: float) -> None:
        if not video_id or not math.isfinite(offset):
            return
        self.upsert_track(video_id)
        clamped = round(max(-60.0, min(60.0, float(offset))), 2)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tracks SET lyrics_offset = ? WHERE id = ?",
                (clamped, str(video_id)),
            )

    def get_lyrics_offset(self, video_id: str | None) -> float:
        if not video_id:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT lyrics_offset FROM tracks WHERE id = ?", (str(video_id),)
            ).fetchone()
            if not row or row["lyrics_offset"] is None:
                return 0.0
            return float(row["lyrics_offset"])

    # ------------------------------------------------------------------
    # Offline Audio Cache Registry & Budget Eviction
    # ------------------------------------------------------------------

    def set_cached_audio(self, video_id: str, path: str, size_bytes: int = 0) -> None:
        vid = self.upsert_track(video_id)
        p = Path(path)
        actual_size = size_bytes
        if actual_size <= 0 and p.is_file() and not p.is_symlink():
            try:
                actual_size = p.stat().st_size
            except OSError:
                actual_size = 0
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE tracks
                SET cached_audio_path = ?, cached_audio_bytes = ?, cached_at = ?
                WHERE id = ?
                """,
                (str(p), int(actual_size), time.time(), vid),
            )

    def get_cached_audio(self, video_id: str | None) -> str | None:
        if not video_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT cached_audio_path FROM tracks WHERE id = ?", (str(video_id),)
            ).fetchone()
            if not row or not row["cached_audio_path"]:
                return None
            p = Path(row["cached_audio_path"])
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_size > 0:
                    return str(p)
            except OSError:
                pass
            # Clean stale reference if file was removed externally
            with self._conn:
                self._conn.execute(
                    "UPDATE tracks SET cached_audio_path = NULL, cached_audio_bytes = 0 WHERE id = ?",
                    (str(video_id),),
                )
            return None

    def enforce_audio_cache_budget(
        self, max_bytes: int = DEFAULT_AUDIO_CACHE_MAX_BYTES
    ) -> list[str]:
        """Evict least-recently-used cached audio files (unliked first) to stay <= max_bytes."""
        evicted: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.id, t.cached_audio_path, t.cached_audio_bytes,
                       CASE WHEN l.video_id IS NOT NULL THEN 1 ELSE 0 END AS is_liked,
                       COALESCE(t.last_played_at, t.cached_at, 0) AS touch_ts
                FROM tracks t
                LEFT JOIN likes l ON l.video_id = t.id
                WHERE t.cached_audio_path IS NOT NULL
                ORDER BY is_liked ASC, touch_ts ASC
                """
            ).fetchall()
            total = sum(int(r["cached_audio_bytes"] or 0) for r in rows)
            if total <= max_bytes:
                return []
            for r in rows:
                if total <= max_bytes:
                    break
                vid = r["id"]
                p = Path(r["cached_audio_path"])
                sz = int(r["cached_audio_bytes"] or 0)
                try:
                    if not p.is_symlink() and p.exists():
                        p.unlink()
                except OSError:
                    pass
                with self._conn:
                    self._conn.execute(
                        "UPDATE tracks SET cached_audio_path = NULL, cached_audio_bytes = 0 WHERE id = ?",
                        (vid,),
                    )
                total -= sz
                evicted.append(str(vid))
        return evicted
