from __future__ import annotations

import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import sources
from .dsp import DSPConfig, compute_crossfade_gain, mpv_dsp_commands
from .library import Library

TMPDIR = Path(tempfile.gettempdir())
MPV_SOCK = Path(os.environ.get("MYX_MPV_SOCK", TMPDIR / "myx-mpv.sock"))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", TMPDIR / "myx-cmd.sock"))
STATE_PATH = Path(os.environ.get("MYX_STATE", TMPDIR / "myx-state.json"))

MAX_CMD_BYTES = 65536
MAX_CACHE_ENTRIES = 512
MAX_SEEN_ENTRIES = 2000
MAX_HISTORY_ENTRIES = 50
DEFAULT_STREAM_TTL_S = 3 * 3600  # 3h fallback if URL has no expire= query param


def _video_id(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(ord(c) < 0x20 for c in raw):
        return None
    for pat in (
        r"[?&]v=([^&#/?\s]+)",
        r"youtu\.be/([^?&#/\s]+)",
        r"/shorts/([^?&#/\s]+)",
        r"/embed/([^?&#/\s]+)",
        r"/live/([^?&#/\s]+)",
        r"/watch/([^?&#/\s]+)",
    ):
        m = re.search(pat, raw)
        if m:
            candidate = m.group(1)
            if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", candidate):
                return candidate
            return None
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", raw):
        return raw
    return None


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _stream_expiry_ts(url: str) -> float:
    """Extract YouTube CDN expire timestamp (minus 60s safety margin) or default TTL."""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        exp_vals = qs.get("expire")
        if exp_vals:
            return float(exp_vals[0]) - 60.0
    except Exception:
        pass
    return time.time() + DEFAULT_STREAM_TTL_S


def _bounded_put(store: dict[Any, Any], key: Any, value: Any, limit: int = MAX_CACHE_ENTRIES) -> None:
    if key in store:
        store.pop(key, None)
    elif len(store) >= limit:
        try:
            oldest = next(iter(store))
            store.pop(oldest, None)
        except StopIteration:
            pass
    store[key] = value


def _is_garbage_title(title: str | None) -> bool:
    if not title or not str(title).strip():
        return True
    s = str(title).strip().lower()
    if s in ("web", "videoplayback", "audio", "none", "unknown"):
        return True
    if s.startswith(("http://", "https://", "videoplayback?")):
        return True
    if "videoplayback" in s and ("expire=" in s or "sparams=" in s or "id=" in s):
        return True
    return False


def _clean_title(track: dict[str, Any] | None) -> str | None:
    if not isinstance(track, dict):
        return None
    raw_title = track.get("title")
    if _is_garbage_title(raw_title):
        return None
    title = str(raw_title).strip()
    artist = track.get("artist")
    if artist and isinstance(artist, str):
        artist_clean = artist.strip()
        if artist_clean and artist_clean.lower() not in title.lower():
            return f"{title} - {artist_clean}"
    return title


def _stop_stale_mpv_socket(sock_path: Path) -> None:
    """Gracefully ask any leftover myx mpv instance on sock_path to quit."""
    if not sock_path.exists() or sock_path.is_symlink():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(sock_path))
            s.sendall(b'{"command":["quit"],"request_id":1}\n')
    except OSError:
        pass


_ORIGINAL_MIX_CHAIN = getattr(sources, "mix_chain", None)


def _active_chapter(
    chapters: list[dict[str, Any]] | None, pos: float | None
) -> dict[str, Any] | None:
    if not chapters or pos is None:
        return None
    try:
        p = float(pos)
    except (TypeError, ValueError):
        return None
    active: dict[str, Any] | None = None
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        start = float(ch.get("start_time", 0.0))
        end = float(ch.get("end_time", start))
        if start <= p < end or (p >= start and end <= start):
            return ch
        if p >= start:
            active = ch
    return active


class PlayerDaemon:
    def __init__(
        self,
        url: str | None = None,
        no_lyrics: bool = False,
        *,
        library: Library | None = None,
        sponsorblock: bool | None = None,
    ):
        self.url = url
        self.no_lyrics = no_lyrics or os.environ.get("MYX_NO_LYRICS") == "1"
        self.stop = threading.Event()
        self.lock = threading.RLock()  # guards mpv IPC socket request/response
        self._state_lock = threading.RLock()  # guards queue/history/caches
        self.mpv: subprocess.Popen[str] | None = None
        self.seq = 0
        self.seen: set[str] = set()
        self.cached_chain: dict[str, list[Any]] = {}
        self._lyrics_cache: dict[str, dict[str, str | None]] = {}
        self._lyrics_inflight: set[str] = set()
        self._lyrics_lock = threading.Lock()
        self._thumb_cache: dict[str, str | None] = {}
        self._thumb_pending: set[str] = set()

        self._queue_meta_cache: dict[str, dict[str, Any]] = {}
        self._stream_cache: dict[str, str] = {}
        self._stream_expiry: dict[str, float] = {}
        self._id_by_path: dict[str, str] = {}
        self._current_meta: dict[str, Any] = {}
        self._idle_since: float | None = None

        self.queue: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.current_track: dict[str, Any] | None = None
        self.loop_mode: str = "off"  # "off" | "one" | "all"
        self.dsp = DSPConfig()
        self._user_volume: int | None = None
        self._xfade_active: bool = False

        self._library_instance: Library | None = library
        self.auto_cache_likes: bool = os.environ.get("MYX_AUTO_CACHE_LIKES") == "1"
        self._audio_cache_inflight: set[str] = set()

        default_sb = (
            os.environ.get("MYX_NO_SPONSORBLOCK") != "1"
            and "PYTEST_CURRENT_TEST" not in os.environ
        )
        self.sponsorblock: bool = (
            bool(sponsorblock) if sponsorblock is not None else default_sb
        )
        self._sb_cache: dict[str, list[dict[str, Any]]] = {}
        self._sb_inflight: set[str] = set()
        self._sb_skipped_keys: dict[tuple[str, float, float], float] = {}
        self._last_sb_skip: dict[str, Any] | None = None

        self._refill_event = threading.Event()
        self._wake_event = threading.Event()
        self._buffer_lock = threading.Lock()
        self._buffering: bool = False
        self._conn_sem = threading.Semaphore(16)

    @property
    def library(self) -> Library:
        with self._state_lock:
            if self._library_instance is None:
                in_hermetic_pytest = (
                    "PYTEST_CURRENT_TEST" in os.environ
                    and "MYX_DATA_DIR" not in os.environ
                )
                try:
                    self._library_instance = (
                        Library(":memory:") if in_hermetic_pytest else Library()
                    )
                except Exception:
                    self._library_instance = Library(":memory:")
            return self._library_instance

    def _apply_dsp(self) -> None:
        for cmd in mpv_dsp_commands(self.dsp):
            try:
                self._mpv(cmd)
            except Exception:
                pass

    def _schedule_audio_cache(self, video_id: str) -> None:
        if not video_id:
            return
        with self._state_lock:
            if video_id in self._audio_cache_inflight:
                return
            if self.library.get_cached_audio(video_id):
                return
            self._audio_cache_inflight.add(video_id)

        def _worker() -> None:
            try:
                path = sources.cache_audio(video_id)
                self.library.set_cached_audio(video_id, path)
                self.library.enforce_audio_cache_budget()
            except Exception:
                pass
            finally:
                with self._state_lock:
                    self._audio_cache_inflight.discard(video_id)

        threading.Thread(target=_worker, daemon=True).start()

    def _resolve_sponsorblock(self, vid: str | None) -> list[dict[str, Any]]:
        if not self.sponsorblock or not vid:
            return []
        with self._state_lock:
            if vid in self._sb_cache:
                return self._sb_cache[vid]
            if vid in self._sb_inflight:
                return []
            self._sb_inflight.add(vid)

        def _fetch() -> None:
            try:
                segs = sources.sponsorblock_segments(vid)
            except Exception:
                segs = []
            with self._state_lock:
                _bounded_put(self._sb_cache, vid, segs, limit=256)
                self._sb_inflight.discard(vid)

        threading.Thread(target=_fetch, daemon=True).start()
        return []

    def _candidate_cmd_socks(self) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        fallback_tmpdirs = [Path("/tmp"), Path(tempfile.gettempdir())]
        if tmp := os.environ.get("MYX_FALLBACK_TMP"):
            fallback_tmpdirs.append(Path(tmp))
        for p in [CMD_SOCK] + [d / "myx-cmd.sock" for d in fallback_tmpdirs]:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _mark_seen(self, vid: str) -> None:
        with self._state_lock:
            if len(self.seen) >= MAX_SEEN_ENTRIES:
                # Retain active queue + current IDs, clear ancient history
                keep = {x.get("id") for x in self.queue if x.get("id")}
                if self.current_track and self.current_track.get("id"):
                    keep.add(self.current_track["id"])
                self.seen = {str(x) for x in keep if x}
            self.seen.add(vid)

    def _push_history(self, track: dict[str, Any] | None) -> None:
        if not isinstance(track, dict) or not track.get("id"):
            return
        with self._state_lock:
            if self.history and self.history[-1].get("id") == track.get("id"):
                return
            snap = {
                "id": track.get("id"),
                "title": track.get("title"),
                "artist": track.get("artist"),
                "duration": track.get("duration"),
                "thumb": track.get("thumb"),
                "stream_url": track.get("stream_url"),
            }
            self.history.append(snap)
            if len(self.history) > MAX_HISTORY_ENTRIES:
                self.history = self.history[-MAX_HISTORY_ENTRIES:]

    def _open_lock_exclusive(self, lock_path: Path) -> bool:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True

    def _acquire_singleton(self, url: str | None) -> bool:
        lock_path = TMPDIR / "myx.lock"
        try:
            return self._open_lock_exclusive(lock_path)
        except FileExistsError:
            pass
        except OSError:
            return True

        vid = _video_id(url) if url else None
        req = {"cmd": "play_id", "arg": vid} if vid else {"cmd": "__probe__"}
        for sock_path in self._candidate_cmd_socks():
            if sock_path.is_symlink():
                continue
            try:
                st = sock_path.stat()
                if hasattr(os, "getuid") and st.st_uid != os.getuid():
                    continue
            except OSError:
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.6)
                    s.connect(str(sock_path))
                    s.sendall((json.dumps(req) + "\n").encode())
                    s.recv(1024)
                print(f"delegated to existing daemon at {sock_path}", file=sys.stderr)
                return False
            except Exception:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            self._open_lock_exclusive(lock_path)
        except OSError:
            pass
        return True

    def run(self) -> None:
        if not self._acquire_singleton(self.url):
            return
        _stop_stale_mpv_socket(MPV_SOCK)
        self._cleanup_sockets()
        self._spawn_mpv()
        self._wait_for_socket(MPV_SOCK)
        try:
            if MPV_SOCK.exists() and not MPV_SOCK.is_symlink():
                os.chmod(MPV_SOCK, 0o600)
        except OSError:
            pass
        self._command_server_thread = threading.Thread(
            target=self._command_server, daemon=True
        )
        self._refill_loop_thread = threading.Thread(
            target=self._refill_loop, daemon=True
        )
        self._command_server_thread.start()
        self._refill_loop_thread.start()
        try:
            if self.url:
                seed_vid = _video_id(self.url)
                if seed_vid:
                    self._play_now(seed_vid)
                elif self.url.startswith(("https://", "http://")):
                    try:
                        sources._validate_http_url(self.url)
                        self._mpv(["loadfile", self.url, "replace"])
                    except sources.SourcesError as exc:
                        print(f"[myx] rejected unsafe seed url: {exc}", file=sys.stderr)
        finally:
            self._state_loop_thread = threading.Thread(
                target=self._state_loop, daemon=True
            )
            self._queue_hydration_loop_thread = threading.Thread(
                target=self._queue_hydration_loop, daemon=True
            )
            self._healthcheck_loop_thread = threading.Thread(
                target=self._healthcheck_loop, daemon=True
            )
            self._state_loop_thread.start()
            self._queue_hydration_loop_thread.start()
            self._healthcheck_loop_thread.start()
        try:
            while not self.stop.is_set():
                if self.mpv and self.mpv.poll() is not None:
                    break
                time.sleep(0.2)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.stop.set()
        self._wake_event.set()
        self._refill_event.set()
        try:
            self._mpv(["quit"])
        except Exception:
            pass
        if self.mpv and self.mpv.poll() is None:
            self.mpv.terminate()
            try:
                self.mpv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mpv.kill()
        self._cleanup_sockets()
        try:
            STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            lock_path = TMPDIR / "myx.lock"
            if lock_path.exists() and not lock_path.is_symlink():
                raw = lock_path.read_text(encoding="utf-8", errors="ignore").strip()
                if not raw or raw == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._library_instance is not None:
            self._library_instance.close()

    def _spawn_mpv(self) -> None:
        old_umask = os.umask(0o077)
        try:
            self.mpv = subprocess.Popen(
                [
                    "mpv",
                    f"--input-ipc-server={MPV_SOCK}",
                    "--idle=yes",
                    "--gapless-audio=yes",
                    "--no-video",
                    "--no-ytdl",
                    "--no-terminal",
                ],
                text=True,
            )
        finally:
            os.umask(old_umask)

    @staticmethod
    def _cleanup_sockets() -> None:
        for path in (MPV_SOCK, CMD_SOCK):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _wait_for_socket(path: Path) -> None:
        for _ in range(100):
            if path.exists():
                return
            time.sleep(0.1)
        raise RuntimeError(f"socket did not appear: {path}")

    def _mpv(self, command: list[Any]) -> Any:
        with self.lock:
            self.seq += 1
            payload = {"command": command, "request_id": self.seq}
            raw_json = json.dumps(payload, allow_nan=False)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(str(MPV_SOCK))
                sock.sendall((raw_json + "\n").encode())
                buf = b""
                while b"\n" not in buf and len(buf) < 1024 * 1024:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            if not buf:
                raise RuntimeError("empty mpv response")
            for raw_line in buf.splitlines():
                if not raw_line.strip():
                    continue
                resp = json.loads(raw_line.decode(errors="replace"))
                if "request_id" in resp and resp.get("request_id") != self.seq:
                    continue
                if resp.get("error") not in (None, "success"):
                    raise RuntimeError(resp.get("error"))
                return resp.get("data")
            raise RuntimeError("missing matching mpv response")

    def _playlist(self) -> list[dict[str, Any]]:
        data = self._mpv(["get_property", "playlist"])
        return data if isinstance(data, list) else []

    def _current_id(self) -> str | None:
        with self._state_lock:
            if self.current_track and self.current_track.get("id"):
                return str(self.current_track["id"])
        path = self._mpv(["get_property", "path"])
        with self._state_lock:
            mapped = self._id_by_path.get(path or "")
        return mapped or _video_id(path) or _video_id(self.url)

    def _append_track(self, vid_or_item: str | dict[str, Any], mark_seen: bool = True) -> bool:
        with self._state_lock:
            if isinstance(vid_or_item, dict):
                item = dict(vid_or_item)
                vid = item.get("id")
            else:
                vid = str(vid_or_item)
                meta = self._queue_meta_cache.get(vid, {})
                item = {
                    "id": vid,
                    "title": meta.get("title"),
                    "artist": meta.get("artist"),
                    "duration": meta.get("duration"),
                    "thumb": meta.get("thumb"),
                    "stream_url": None,
                }
            item.setdefault("stream_url", None)
            if vid:
                _bounded_put(self._queue_meta_cache, vid, item)
            self.queue.append(item)
            if mark_seen and vid:
                self._mark_seen(vid)
        self._trigger_buffer()
        self._wake_event.set()
        return True

    def _stream_for(self, video_id: str) -> str:
        try:
            local_cached = self.library.get_cached_audio(video_id)
            if local_cached:
                return local_cached
        except Exception:
            pass
        now = time.time()
        with self._state_lock:
            cached = self._stream_cache.get(video_id)
            exp = self._stream_expiry.get(video_id, float("inf"))
            if cached and now < exp:
                return cached
            if cached:
                self._stream_cache.pop(video_id, None)
                self._stream_expiry.pop(video_id, None)
        try:
            url = sources.stream_url(video_id)
        except sources.SourcesError as exc:
            print(
                f"[myx] stream resolve failed ({exc}); delegating to mpv",
                file=sys.stderr,
            )
            return _url(video_id)
        with self._state_lock:
            _bounded_put(self._stream_cache, video_id, url)
            _bounded_put(self._stream_expiry, video_id, _stream_expiry_ts(url))
        return url

    def _sync_media_title(self, track: dict[str, Any] | None) -> None:
        clean_display_title = _clean_title(track)
        if clean_display_title:
            try:
                self._mpv(["set_property", "force-media-title", clean_display_title])
            except Exception:
                pass

    def _record_track_play(self, track: dict[str, Any] | None) -> None:
        if not isinstance(track, dict) or not track.get("id"):
            return
        try:
            self.library.record_play(track)
        except Exception:
            pass

    def _play_now(self, video_id: str, *, record_history: bool = False) -> None:
        stream = self._stream_for(video_id)
        with self._state_lock:
            if record_history and self.current_track:
                self._push_history(self.current_track)
            meta = self._queue_meta_cache.get(video_id)
        self._mpv(["loadfile", stream, "replace"])
        if not meta or not meta.get("title"):
            try:
                meta = sources.meta(video_id)
                with self._state_lock:
                    _bounded_put(self._queue_meta_cache, video_id, meta)
            except (sources.SourcesError, Exception):
                meta = meta or {}
        with self._state_lock:
            _bounded_put(self._id_by_path, stream, video_id)
            self._mark_seen(video_id)
            self._current_meta = meta
            self.current_track = {
                "id": video_id,
                "title": meta.get("title"),
                "artist": meta.get("artist"),
                "duration": meta.get("duration"),
                "thumb": meta.get("thumb"),
                "chapters": meta.get("chapters") or [],
                "stream_url": stream,
            }
            cur_snap = dict(self.current_track)
        self._record_track_play(cur_snap)
        self._sync_media_title(cur_snap)
        self._resolve_sponsorblock(video_id)
        self._trigger_buffer()

    def _trigger_buffer(self) -> None:
        if self._buffering or self.stop.is_set():
            return
        with self._state_lock:
            if not self.queue:
                return
        try:
            pl = self._playlist()
            # Pre-buffer queue[0] as Track 1 ONLY when Track 0 is already loaded.
            # If pl is empty (idle daemon), _maintain_playback starts queue[0] via replace.
            if len(pl) != 1:
                return
        except Exception:
            return
        self._buffering = True
        threading.Thread(target=self._buffer_worker, daemon=True).start()

    def _buffer_worker(self) -> None:
        try:
            with self._buffer_lock:
                if self.stop.is_set():
                    return
                try:
                    pl = self._playlist()
                    if len(pl) != 1:
                        return
                except Exception:
                    return
                with self._state_lock:
                    if not self.queue:
                        return
                    target = self.queue[0]
                    vid = target.get("id")
                    existing_stream = target.get("stream_url")
                if not vid:
                    return
                stream = existing_stream or self._stream_for(vid)
                with self._state_lock:
                    if self.stop.is_set() or not self.queue or self.queue[0].get("id") != vid:
                        return
                    if stream != _url(vid):
                        self.queue[0]["stream_url"] = stream
                with self.lock:
                    pl = self._playlist()
                    if len(pl) == 1:
                        self._mpv(["loadfile", stream, "append"])
                        with self._state_lock:
                            _bounded_put(self._id_by_path, stream, vid)
                            self._mark_seen(vid)
        except Exception as exc:
            print(f"[myx] buffer error: {exc}", file=sys.stderr)
        finally:
            self._buffering = False

    def _advance_track_on_transition(self, playlist: list[dict[str, Any]], pos: int) -> None:
        with self._state_lock:
            outgoing = self.current_track
            if outgoing:
                self._push_history(outgoing)
                if self.loop_mode == "all" and outgoing.get("id"):
                    recycled = dict(outgoing)
                    recycled["stream_url"] = None
                    self.queue.append(recycled)
            if self.queue:
                self.current_track = self.queue.pop(0)
            elif 0 <= pos < len(playlist):
                item = playlist[pos]
                path = item.get("filename")
                vid = self._id_by_path.get(path or "") or _video_id(path)
                cached = self._queue_meta_cache.get(vid, {}) if vid else {}
                self.current_track = {
                    "id": vid,
                    "title": item.get("title") or cached.get("title"),
                    "artist": cached.get("artist"),
                    "duration": item.get("duration") or cached.get("duration"),
                    "thumb": cached.get("thumb"),
                    "stream_url": path,
                }
            cur_snap = dict(self.current_track) if self.current_track else None
        self._record_track_play(cur_snap)
        self._sync_media_title(cur_snap)
        if cur_snap and cur_snap.get("id"):
            self._resolve_sponsorblock(str(cur_snap["id"]))
        if self._xfade_active and self._user_volume is not None:
            self._xfade_active = False
            try:
                self._mpv(["set_property", "volume", self._user_volume])
            except Exception:
                pass
        for _ in range(pos):
            try:
                self._mpv(["playlist-remove", 0])
            except Exception:
                break
        if len(self.queue) <= 3:
            self._refill_event.set()

    def _tick_sponsorblock_and_crossfade(self) -> None:
        if not self.sponsorblock and self.dsp.crossfade_s <= 0:
            return
        with self._state_lock:
            vid = self.current_track.get("id") if self.current_track else None
            cur_dur = (
                self.current_track.get("duration") if self.current_track else None
            )
        try:
            pos_raw = self._mpv(["get_property", "time-pos"])
            dur_raw = self._mpv(["get_property", "duration"]) or cur_dur
            pos = float(pos_raw) if pos_raw is not None else None
            dur = float(dur_raw) if dur_raw is not None else None
        except Exception:
            return
        if pos is None:
            return

        # 1. SponsorBlock auto-skip
        if self.sponsorblock and vid:
            segs = self._resolve_sponsorblock(str(vid))
            now = time.time()
            for seg in segs:
                s_start = float(seg["start"])
                s_end = float(seg["end"])
                if not (s_start <= pos < s_end - 0.35):
                    continue
                if dur and (s_end - s_start) >= dur * 0.85:
                    continue
                key = (str(vid), s_start, s_end)
                with self._state_lock:
                    last_t = self._sb_skipped_keys.get(key, 0.0)
                    if now - last_t < 6.0:
                        continue
                    self._sb_skipped_keys[key] = now
                    self._last_sb_skip = {
                        "id": str(vid),
                        "category": seg.get("category", "music_offtopic"),
                        "start": s_start,
                        "end": s_end,
                        "ts": now,
                    }
                try:
                    self._mpv(["seek", s_end, "absolute"])
                    pos = s_end
                except Exception:
                    pass
                break

        # 2. Smart Crossfade volume envelope
        if self.dsp.crossfade_s > 0 and dur:
            if self._user_volume is None:
                try:
                    self._user_volume = int(
                        float(self._mpv(["get_property", "volume"]) or 80)
                    )
                except Exception:
                    self._user_volume = 80
            gain = compute_crossfade_gain(pos, dur, self.dsp.crossfade_s)
            if gain < 0.999:
                self._xfade_active = True
                target_vol = max(1, min(100, round(self._user_volume * gain)))
                try:
                    self._mpv(["set_property", "volume", target_vol])
                except Exception:
                    pass
            elif self._xfade_active:
                self._xfade_active = False
                try:
                    self._mpv(["set_property", "volume", self._user_volume])
                except Exception:
                    pass

    def _maintain_playback(self) -> None:
        try:
            playlist = self._playlist()
            pos_raw = self._mpv(["get_property", "playlist-pos"]) if playlist else 0
            pos = int(pos_raw) if pos_raw is not None else 0
        except Exception:
            return

        if pos >= 1:
            self._advance_track_on_transition(playlist, pos)

        try:
            playlist = self._playlist()
        except Exception:
            playlist = []
        if not playlist:
            next_item = None
            with self._state_lock:
                if self.queue:
                    if self.current_track:
                        self._push_history(self.current_track)
                        if self.loop_mode == "all" and self.current_track.get("id"):
                            recycled = dict(self.current_track)
                            recycled["stream_url"] = None
                            self.queue.append(recycled)
                    next_item = self.queue.pop(0)
            if next_item:
                vid = next_item.get("id")
                if vid:
                    stream = next_item.get("stream_url") or self._stream_for(vid)
                    if stream != _url(vid):
                        next_item["stream_url"] = stream
                    with self._state_lock:
                        self.current_track = next_item
                        _bounded_put(self._id_by_path, stream, vid)
                        self._mark_seen(vid)
                    try:
                        self._mpv(["loadfile", stream, "replace"])
                        self._record_track_play(next_item)
                        self._sync_media_title(next_item)
                        self._resolve_sponsorblock(str(vid))
                    except Exception:
                        pass
                    self._refill_event.set()

        try:
            playlist = self._playlist()
        except Exception:
            playlist = []
        if len(playlist) == 1 and self.queue:
            self._trigger_buffer()
        if playlist:
            self._tick_sponsorblock_and_crossfade()

    def _append_related(self) -> bool:
        cur = self._current_id()
        if not cur:
            return False
        try:
            if sources.mix_chain is _ORIGINAL_MIX_CHAIN and hasattr(sources, "mix_chain_items"):
                chain: list[Any] = sources.mix_chain_items(cur)
            else:
                chain = sources.mix_chain(cur)
            if chain:
                with self._state_lock:
                    _bounded_put(self.cached_chain, cur, chain, limit=128)
        except sources.SourcesError:
            with self._state_lock:
                chain = list(self.cached_chain.get(cur, []))
            if not chain:
                raise
        except Exception:
            with self._state_lock:
                chain = list(self.cached_chain.get(cur, []))
            if not chain:
                raise
        added = 0
        for it in chain:
            vid = it.get("id") if isinstance(it, dict) else str(it)
            with self._state_lock:
                already_seen = not vid or vid in self.seen
            if already_seen:
                continue
            if self._append_track(it, True):
                added += 1
        if added == 0 and chain:
            with self._state_lock:
                existing_ids = {
                    self.current_track.get("id") if self.current_track else None
                } | {x.get("id") for x in self.queue}
            for it in chain:
                vid = it.get("id") if isinstance(it, dict) else str(it)
                if not vid or vid in existing_ids:
                    continue
                if self._append_track(it, False):
                    added += 1
        return added > 0

    def _refill_loop(self) -> None:
        for _ in range(10):
            if self.stop.is_set():
                return
            try:
                if self._append_related():
                    break
            except Exception:
                time.sleep(2)
        backoff = 5
        while not self.stop.is_set():
            try:
                with self._state_lock:
                    q_len = len(self.queue)
                if q_len <= 3:
                    self._append_related()
                backoff = 5
                self._refill_event.wait(timeout=2)
                self._refill_event.clear()
            except Exception:
                time.sleep(backoff)
                backoff = min(30, backoff * 2)

    def _state_loop(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._maintain_playback()
                self.write_state()
            except Exception:
                pass
            wait_s = max(0.05, 0.25 - (time.monotonic() - started))
            self._wake_event.wait(timeout=wait_s)
            self._wake_event.clear()

    def _queue_hydration_loop(self) -> None:
        """Deprecated: Queue items are captured with metadata at enqueue time."""
        pass

    def _restart_mpv(self) -> None:
        if self.mpv and self.mpv.poll() is None:
            try:
                self.mpv.terminate()
                self.mpv.wait(timeout=5)
            except Exception:
                try:
                    self.mpv.kill()
                except Exception:
                    pass
        self.mpv = None
        self._spawn_mpv()
        self._wait_for_socket(MPV_SOCK)
        try:
            if MPV_SOCK.exists() and not MPV_SOCK.is_symlink():
                os.chmod(MPV_SOCK, 0o600)
        except OSError:
            pass
        try:
            with self._state_lock:
                cur_id = self.current_track.get("id") if self.current_track else None
            if not cur_id:
                current_path = self._mpv(["get_property", "path"])
                with self._state_lock:
                    cur_id = self._id_by_path.get(current_path or "") or _video_id(current_path)
            if cur_id:
                self._play_now(str(cur_id))
            elif self.url:
                vid = _video_id(self.url)
                if vid:
                    self._play_now(vid)
            if self.loop_mode == "one":
                self._mpv(["set_property", "loop-file", "inf"])
        except Exception:
            pass
        with self._state_lock:
            self.cached_chain.clear()
        try:
            self._append_related()
        except Exception:
            pass

    def _healthcheck_loop(self) -> None:
        while not self.stop.is_set():
            time.sleep(5)
            if self.stop.is_set():
                break
            if self.mpv and self.mpv.poll() is not None:
                print(
                    f"[myx] mpv died (exit {self.mpv.poll()}), restarting",
                    file=sys.stderr,
                )
                try:
                    self._restart_mpv()
                except Exception as exc2:
                    print(f"[myx] mpv restart failed: {exc2}", file=sys.stderr)
                continue
            fails = 0
            for _ in range(2):
                try:
                    self._mpv(["get_property", "idle-active"])
                    fails = 0
                    break
                except Exception:
                    fails += 1
                    time.sleep(0.4)
            if fails >= 2:
                print(
                    "[myx] mpv healthcheck: socket unresponsive, restarting",
                    file=sys.stderr,
                )
                try:
                    self._restart_mpv()
                except Exception as exc2:
                    print(f"[myx] mpv restart failed: {exc2}", file=sys.stderr)
                continue
            # EOF watchdog: track ended but mpv stalled instead of advancing
            try:
                core_idle = bool(self._mpv(["get_property", "core-idle"]))
                paused_now = bool(self._mpv(["get_property", "pause"]))
                if core_idle and not paused_now:
                    pl = self._playlist()
                    if len(pl) > 1:
                        now = time.monotonic()
                        if self._idle_since is None:
                            self._idle_since = now
                        elif now - self._idle_since > 25:
                            self._idle_since = None
                            print(
                                "[myx] eof watchdog: kicking stalled track (sustained idle)",
                                file=sys.stderr,
                            )
                            self._mpv(["playlist-next", "force"])
                    else:
                        self._idle_since = None
                else:
                    self._idle_since = None
            except Exception:
                self._idle_since = None

    def _resolve_thumb(self, vid: str | None, raw_thumb: str | None) -> str | None:
        if not vid and not raw_thumb:
            return None
        cache_key = vid or raw_thumb or ""
        with self._state_lock:
            if cache_key in self._thumb_cache:
                return self._thumb_cache[cache_key]
            if cache_key in self._thumb_pending:
                return None
            url = (
                raw_thumb
                if isinstance(raw_thumb, str)
                and raw_thumb.startswith(("https://", "http://"))
                else None
            )
            if not url and vid and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", str(vid)):
                url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            if not url:
                _bounded_put(self._thumb_cache, cache_key, None)
                return None
            self._thumb_pending.add(cache_key)

        def _fetch() -> None:
            try:
                path = sources.cache_thumb(cache_key, url)
                with self._state_lock:
                    _bounded_put(self._thumb_cache, cache_key, path)
            except Exception:
                with self._state_lock:
                    _bounded_put(self._thumb_cache, cache_key, None)
            finally:
                with self._state_lock:
                    self._thumb_pending.discard(cache_key)

        threading.Thread(target=_fetch, daemon=True).start()
        return None

    def _resolve_lyrics(
        self, vid: str | None, title: Any, artist: Any, duration: Any
    ) -> dict[str, str | None]:
        if self.no_lyrics or not vid:
            return {"lrc": None, "plain": None}
        with self._lyrics_lock:
            if vid in self._lyrics_cache:
                return self._lyrics_cache[vid]
            if vid not in self._lyrics_inflight:
                self._lyrics_inflight.add(vid)
                threading.Thread(
                    target=self._fetch_lyrics,
                    args=(vid, title, artist, duration),
                    daemon=True,
                ).start()
        return {"lrc": None, "plain": None}

    def _fetch_lyrics(self, vid: str, title: Any, artist: Any, duration: Any) -> None:
        try:
            dur = None
            if duration is not None:
                try:
                    dur = float(duration)
                except Exception:
                    dur = None
            data = sources.lyrics(str(title or ""), str(artist or ""), dur)
        except Exception:
            data = {"lrc": None, "plain": None}
        with self._lyrics_lock:
            _bounded_put(self._lyrics_cache, vid, data)
            self._lyrics_inflight.discard(vid)

    def write_state(self) -> dict[str, Any]:
        playlist = self._playlist()
        pos_raw = self._mpv(["get_property", "playlist-pos"]) if playlist else 0
        pos = int(pos_raw) if pos_raw is not None else 0
        current = playlist[pos] if 0 <= pos < len(playlist) else {}
        path = current.get("filename") or self._mpv(["get_property", "path"])

        with self._state_lock:
            cur_track = dict(self.current_track) if self.current_track else None
            queue_snapshot = [dict(item) for item in self.queue]
            meta_snapshot = dict(self._current_meta)
            mapped_vid = self._id_by_path.get(path or "")

        vid = (
            (cur_track.get("id") if cur_track else None)
            or mapped_vid
            or _video_id(path)
        )
        raw_thumb = current.get("thumbnail") if isinstance(current.get("thumbnail"), str) else None
        if cur_track and cur_track.get("thumb"):
            raw_thumb = cur_track.get("thumb")
        cached_thumb = self._resolve_thumb(vid, raw_thumb)

        queue = []
        if queue_snapshot:
            for item in queue_snapshot:
                item_id = item.get("id")
                with self._state_lock:
                    cached_meta = dict(self._queue_meta_cache.get(item_id, {})) if item_id else {}
                item_title = item.get("title") or cached_meta.get("title")
                item_artist = item.get("artist") or cached_meta.get("artist")
                if not isinstance(item_artist, str):
                    item_artist = None
                item_duration = item.get("duration") or cached_meta.get("duration")
                item_raw_thumb = item.get("thumb") or cached_meta.get("thumb")
                item_thumb = self._resolve_thumb(item_id, item_raw_thumb)
                queue.append(
                    {
                        "id": item_id,
                        "title": item_title,
                        "artist": item_artist,
                        "duration": item_duration,
                        "thumb": item_thumb,
                    }
                )
        elif playlist and pos + 1 < len(playlist):
            for item in playlist[pos + 1 :]:
                with self._state_lock:
                    item_id = self._id_by_path.get(item.get("filename") or "") or _video_id(
                        item.get("filename")
                    )
                    cached_meta = dict(self._queue_meta_cache.get(item_id, {})) if item_id else {}
                item_title = item.get("title") or cached_meta.get("title")
                item_artist = (
                    item.get("uploader")
                    if isinstance(item.get("uploader"), str)
                    else cached_meta.get("artist")
                )
                if not isinstance(item_artist, str):
                    item_artist = None
                item_duration = item.get("duration") or cached_meta.get("duration")
                item_raw_thumb = (
                    item.get("thumbnail")
                    if isinstance(item.get("thumbnail"), str)
                    else cached_meta.get("thumb")
                )
                item_thumb = self._resolve_thumb(item_id, item_raw_thumb)
                queue.append(
                    {
                        "id": item_id,
                        "title": item_title,
                        "artist": item_artist,
                        "duration": item_duration,
                        "thumb": item_thumb,
                    }
                )
        mpv_title = self._mpv(["get_property", "media-title"])
        if meta_snapshot.get("id") == vid or (meta_snapshot and not vid):
            cached_current = meta_snapshot
        else:
            with self._state_lock:
                cached_current = dict(self._queue_meta_cache.get(vid, {})) if vid else {}
        cur_title = (
            (cur_track.get("title") if cur_track else None)
            or cached_current.get("title")
        )

        if cur_title and not _is_garbage_title(cur_title):
            title_val = cur_title
        elif mpv_title and not _is_garbage_title(mpv_title):
            title_val = mpv_title
            with self._state_lock:
                if self.current_track and not self.current_track.get("title"):
                    self.current_track["title"] = mpv_title
                    cur_track = dict(self.current_track)
        elif current.get("title") and not _is_garbage_title(current.get("title")):
            title_val = current.get("title")
        else:
            title_val = cur_title if (cur_title and not _is_garbage_title(cur_title)) else None

        if cur_track:
            clean_display_title = _clean_title(cur_track)
            if clean_display_title and (not mpv_title or _is_garbage_title(mpv_title)):
                try:
                    self._mpv(["set_property", "force-media-title", clean_display_title])
                except Exception:
                    pass

        artist_val = (
            (cur_track.get("artist") if cur_track else None)
            or cached_current.get("artist")
            or (current.get("uploader") if isinstance(current.get("uploader"), str) else None)
        )
        dur_val = (
            self._mpv(["get_property", "duration"])
            or (cur_track.get("duration") if cur_track else None)
            or cached_current.get("duration")
        )
        pos_val = self._mpv(["get_property", "time-pos"])
        vol_val = self._mpv(["get_property", "volume"])
        paused_val = bool(self._mpv(["get_property", "pause"]))
        try:
            muted_val = bool(self._mpv(["get_property", "mute"]))
        except Exception:
            muted_val = False
        lyr = self._resolve_lyrics(vid, title_val, artist_val, dur_val)
        chapters = (
            (cur_track.get("chapters") if cur_track else None)
            or cached_current.get("chapters")
            or []
        )
        active_ch = _active_chapter(chapters, pos_val)
        try:
            lib = self.library
            liked_val = lib.is_liked(vid) if vid else False
            liked_count = lib.liked_count()
            cached_audio_path = lib.get_cached_audio(vid) if vid else None
            lyrics_off = lib.get_lyrics_offset(vid) if vid else 0.0
        except Exception:
            liked_val = False
            liked_count = 0
            cached_audio_path = None
            lyrics_off = 0.0
        with self._state_lock:
            sb_skip = (
                dict(self._last_sb_skip)
                if self._last_sb_skip
                and (time.time() - float(self._last_sb_skip.get("ts", 0.0))) < 4.0
                else None
            )
            dsp_dict = self.dsp.to_dict()
            sb_enabled = self.sponsorblock
        state = {
            "version": 1,
            "playing": bool(playlist) or bool(cur_track),
            "paused": paused_val,
            "muted": muted_val,
            "loop": self.loop_mode,
            "title": title_val,
            "artist": artist_val,
            "id": vid,
            "url": path,
            "thumb": cached_thumb,
            "duration": dur_val,
            "position": pos_val,
            "volume": vol_val,
            "queue": queue,
            "lrc": lyr.get("lrc"),
            "plain": lyr.get("plain"),
            "lyrics": lyr.get("plain"),
            "liked": liked_val,
            "liked_count": liked_count,
            "cached_audio": cached_audio_path,
            "lyrics_offset": lyrics_off,
            "chapters": chapters,
            "chapter": active_ch.get("title") if active_ch else None,
            "dsp": dsp_dict,
            "sponsorblock": sb_enabled,
            "sb_skipped": sb_skip,
        }
        if STATE_PATH.is_symlink():
            raise RuntimeError("refusing to overwrite symlinked state path")
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(STATE_PATH.parent), prefix=".myx-state.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(state))
            os.replace(tmp_name, STATE_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return state

    def _load_track_list(
        self, tracks: list[dict[str, Any]], *, replace_current: bool = True
    ) -> int:
        valid = [t for t in tracks if isinstance(t, dict) and _video_id(str(t.get("id") or ""))]
        if not valid:
            return 0
        with self._state_lock:
            for t in valid:
                vid = _video_id(str(t.get("id")))
                if vid:
                    t["id"] = vid
                    _bounded_put(self._queue_meta_cache, vid, t)
        if replace_current:
            first = valid[0]
            first_vid = str(first["id"])
            with self._state_lock:
                if self.current_track:
                    self._push_history(self.current_track)
                self.queue.clear()
                self.seen = {first_vid}
            self._play_now(first_vid)
            with self._state_lock:
                for t in valid[1:]:
                    vid2 = str(t["id"])
                    self.queue.append(
                        {
                            "id": vid2,
                            "title": t.get("title"),
                            "artist": t.get("artist"),
                            "duration": t.get("duration"),
                            "thumb": t.get("thumb"),
                            "chapters": t.get("chapters") or [],
                            "stream_url": None,
                        }
                    )
                    self._mark_seen(vid2)
            self._trigger_buffer()
        else:
            with self._state_lock:
                for t in valid:
                    vid2 = str(t["id"])
                    self.queue.append(
                        {
                            "id": vid2,
                            "title": t.get("title"),
                            "artist": t.get("artist"),
                            "duration": t.get("duration"),
                            "thumb": t.get("thumb"),
                            "chapters": t.get("chapters") or [],
                            "stream_url": None,
                        }
                    )
                    self._mark_seen(vid2)
            self._trigger_buffer()
            self._wake_event.set()
        return len(valid)

    def handle_command(self, req: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(req, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        cmd = req.get("cmd")
        arg = req.get("arg") if "arg" in req else req.get("value")
        try:
            if cmd == "toggle":
                self._mpv(["cycle", "pause"])
            elif cmd == "pause":
                self._mpv(["set_property", "pause", True])
            elif cmd == "resume":
                self._mpv(["set_property", "pause", False])
            elif cmd == "mute":
                if isinstance(arg, bool):
                    self._mpv(["set_property", "mute", arg])
                else:
                    self._mpv(["cycle", "mute"])
            elif cmd == "loop":
                order = ("off", "one", "all")
                if isinstance(arg, str) and arg in order:
                    self.loop_mode = arg
                else:
                    idx = order.index(self.loop_mode) if self.loop_mode in order else 0
                    self.loop_mode = order[(idx + 1) % len(order)]
                try:
                    self._mpv(
                        [
                            "set_property",
                            "loop-file",
                            "inf" if self.loop_mode == "one" else "no",
                        ]
                    )
                except Exception:
                    pass
                return {"ok": True, "loop": self.loop_mode}
            elif cmd == "skip":
                playlist = self._playlist()
                pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if playlist else 0
                if len(playlist) > 1 and pos < len(playlist) - 1:
                    self._mpv(["playlist-next", "force"])
                    self._maintain_playback()
                else:
                    next_item = None
                    with self._state_lock:
                        if self.queue:
                            if self.current_track:
                                self._push_history(self.current_track)
                                if self.loop_mode == "all" and self.current_track.get("id"):
                                    recycled = dict(self.current_track)
                                    recycled["stream_url"] = None
                                    self.queue.append(recycled)
                            next_item = self.queue.pop(0)
                    if next_item is not None:
                        vid = next_item.get("id")
                        if vid:
                            stream = next_item.get("stream_url") or self._stream_for(vid)
                            if stream != _url(vid):
                                next_item["stream_url"] = stream
                            with self._state_lock:
                                self.current_track = next_item
                                _bounded_put(self._id_by_path, stream, vid)
                                self._mark_seen(vid)
                            self._mpv(["loadfile", stream, "replace"])
                            self._sync_media_title(next_item)
                            self._trigger_buffer()
                            self._refill_event.set()
                    else:
                        self._mpv(["playlist-next", "force"])
            elif cmd == "prev":
                try:
                    cur_pos = float(self._mpv(["get_property", "time-pos"]) or 0.0)
                except Exception:
                    cur_pos = 0.0
                with self._state_lock:
                    has_cur = bool(self.current_track)
                    has_hist = bool(self.history)
                if cur_pos > 5.0 and has_cur:
                    self._mpv(["seek", 0, "absolute"])
                elif has_hist:
                    with self._state_lock:
                        prev_item = self.history.pop()
                        if self.current_track:
                            requeue = dict(self.current_track)
                            self.queue.insert(0, requeue)
                    vid = prev_item.get("id")
                    if vid:
                        stream = self._stream_for(vid)
                        if stream != _url(vid):
                            prev_item["stream_url"] = stream
                        with self._state_lock:
                            self.current_track = prev_item
                            _bounded_put(self._id_by_path, stream, vid)
                        self._mpv(["loadfile", stream, "replace"])
                        self._sync_media_title(prev_item)
                        try:
                            pl = self._playlist()
                            if len(pl) > 1:
                                self._mpv(["playlist-remove", 1])
                        except Exception:
                            pass
                        self._trigger_buffer()
                else:
                    self._mpv(["playlist-prev", "force"])
            elif cmd == "vol":
                try:
                    if req.get("value") is not None:
                        try:
                            cur_vol = int(self._mpv(["get_property", "volume"]) or 50)
                        except Exception:
                            cur_vol = 50
                        delta_f = float(arg) if arg is not None else 0.0
                        if not math.isfinite(delta_f):
                            return {"ok": False, "error": "volume delta must be finite"}
                        new_vol = max(0, min(100, cur_vol + int(delta_f)))
                    elif req.get("arg") is not None:
                        val_f = float(arg)
                        if not math.isfinite(val_f):
                            return {"ok": False, "error": "volume must be finite"}
                        v = int(val_f)
                        if 0 <= v <= 100:
                            new_vol = v
                        else:
                            try:
                                cur_vol = int(
                                    self._mpv(["get_property", "volume"]) or 50
                                )
                            except Exception:
                                cur_vol = 50
                            new_vol = max(0, min(100, cur_vol + v))
                    else:
                        new_vol = 50
                except (TypeError, ValueError, OverflowError):
                    return {"ok": False, "error": "invalid volume argument"}
                self._user_volume = new_vol
                self._xfade_active = False
                self._mpv(["set_property", "volume", new_vol])
            elif cmd == "shuffle":
                import random

                with self._state_lock:
                    random.shuffle(self.queue)
                try:
                    pl = self._playlist()
                    if len(pl) > 1:
                        self._mpv(["playlist-remove", 1])
                except Exception:
                    pass
                self._trigger_buffer()
            elif cmd == "clear":
                with self._state_lock:
                    self.queue.clear()
                try:
                    pl = self._playlist()
                    if len(pl) > 1:
                        self._mpv(["playlist-remove", 1])
                except Exception:
                    pass
                return {"ok": True}
            elif cmd == "play_id":
                vid = _video_id(str(arg)) if arg is not None else None
                if not vid:
                    return {"ok": False, "error": "play_id requires a YouTube URL or video id"}
                with self._state_lock:
                    if self.current_track:
                        self._push_history(self.current_track)
                    self.queue.clear()
                    self.seen = {vid}
                    self.cached_chain.clear()
                self._play_now(vid)
                self._refill_event.set()
                return {"ok": True}
            elif cmd == "load":
                if arg is None or not str(arg).strip():
                    return {"ok": False, "error": "load requires a URL or video id"}
                vid = _video_id(str(arg))
                if not vid:
                    return {"ok": False, "error": "invalid YouTube URL or video id"}
                with self._state_lock:
                    meta = dict(self._queue_meta_cache.get(vid, {}))
                    item = {
                        "id": vid,
                        "title": meta.get("title"),
                        "artist": meta.get("artist"),
                        "duration": meta.get("duration"),
                        "thumb": meta.get("thumb"),
                        "stream_url": None,
                    }
                    self.queue.append(item)
                    self._mark_seen(vid)
                self._trigger_buffer()
                self._wake_event.set()
                return {"ok": True}
            elif cmd == "search_query":
                q = str(arg or "").strip()
                if not q:
                    return {"ok": True, "results": []}
                try:
                    items = sources.search(q)
                    with self._state_lock:
                        for item in items:
                            vid2 = item.get("id")
                            if vid2:
                                _bounded_put(self._queue_meta_cache, vid2, item)
                    return {"ok": True, "results": items}
                except sources.SourcesError as exc:
                    return {"ok": False, "error": str(exc)}
            elif cmd == "search":
                try:
                    items = sources.search(str(arg))
                    added = 0
                    with self._state_lock:
                        for item in items:
                            vid2 = item.get("id")
                            if vid2:
                                _bounded_put(self._queue_meta_cache, vid2, item)
                                self.queue.append(
                                    {
                                        "id": vid2,
                                        "title": item.get("title"),
                                        "artist": item.get("artist"),
                                        "duration": item.get("duration"),
                                        "thumb": item.get("thumb"),
                                        "stream_url": None,
                                    }
                                )
                                self._mark_seen(vid2)
                                added += 1
                    self._trigger_buffer()
                    self._wake_event.set()
                    return {"ok": True, "queued": added}
                except sources.SourcesError as exc:
                    return {"ok": False, "error": str(exc)}
            elif cmd == "jump":
                try:
                    target_pos = float(arg)
                except (TypeError, ValueError, OverflowError):
                    return {"ok": False, "error": "jump requires numeric seconds"}
                if not math.isfinite(target_pos):
                    return {"ok": False, "error": "jump position must be finite"}
                clamped = max(0.0, min(86400.0, target_pos))
                self._mpv(["seek", clamped, "absolute"])
            elif cmd == "seek":
                try:
                    delta = float(arg)
                except (TypeError, ValueError, OverflowError):
                    return {"ok": False, "error": "seek requires numeric delta"}
                if not math.isfinite(delta):
                    return {"ok": False, "error": "seek delta must be finite"}
                clamped_delta = max(-86400.0, min(86400.0, delta))
                self._mpv(["seek", clamped_delta, "relative"])
            elif cmd == "del":
                try:
                    idx = int(arg)
                except Exception:
                    return {"ok": False, "error": "del requires integer index"}
                with self._state_lock:
                    if not (0 <= idx < len(self.queue)):
                        return {"ok": False, "error": "index out of range"}
                    self.queue.pop(idx)
                if idx == 0:
                    try:
                        pl = self._playlist()
                        if len(pl) > 1:
                            self._mpv(["playlist-remove", 1])
                    except Exception:
                        pass
                    self._trigger_buffer()
            elif cmd == "play_index":
                try:
                    idx = int(arg)
                except Exception:
                    return {"ok": False, "error": "play_index requires integer"}
                with self._state_lock:
                    if not (0 <= idx < len(self.queue)):
                        return {"ok": False, "error": "index out of range"}
                if idx == 0:
                    pl = self._playlist()
                    pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if pl else 0
                    if len(pl) > 1 and pos == 0:
                        self._mpv(["playlist-next", "force"])
                        self._maintain_playback()
                        return {"ok": True}
                with self._state_lock:
                    if not (0 <= idx < len(self.queue)):
                        return {"ok": False, "error": "index out of range"}
                    if self.current_track:
                        self._push_history(self.current_track)
                    target = self.queue[idx]
                    self.queue = self.queue[idx + 1 :]
                vid = target.get("id")
                if vid:
                    stream = target.get("stream_url") or self._stream_for(vid)
                    if stream != _url(vid):
                        target["stream_url"] = stream
                    with self._state_lock:
                        self.current_track = target
                        _bounded_put(self._id_by_path, stream, vid)
                        self._mark_seen(vid)
                    self._mpv(["loadfile", stream, "replace"])
                    self._sync_media_title(target)
                    self._trigger_buffer()
                    self._refill_event.set()
                return {"ok": True}
            elif cmd == "eq":
                bands = req.get("bands")
                with self._state_lock:
                    if isinstance(bands, (list, tuple)) and len(bands) == 5:
                        self.dsp.set_custom_bands(bands)
                    elif arg is None or str(arg).strip().lower() in ("", "cycle", "next"):
                        self.dsp.cycle_preset(1)
                    elif str(arg).strip().lower() == "prev":
                        self.dsp.cycle_preset(-1)
                    elif not self.dsp.set_preset(str(arg)):
                        return {"ok": False, "error": f"unknown EQ preset: {arg}"}
                    dsp_snap = self.dsp.to_dict()
                self._apply_dsp()
                return {"ok": True, "dsp": dsp_snap}
            elif cmd == "normalize":
                with self._state_lock:
                    if isinstance(arg, bool):
                        self.dsp.normalize = arg
                    else:
                        self.dsp.normalize = not self.dsp.normalize
                    dsp_snap = self.dsp.to_dict()
                self._apply_dsp()
                return {"ok": True, "normalize": dsp_snap["normalize"], "dsp": dsp_snap}
            elif cmd == "trim_silence":
                with self._state_lock:
                    if isinstance(arg, bool):
                        self.dsp.trim_silence = arg
                    else:
                        self.dsp.trim_silence = not self.dsp.trim_silence
                    dsp_snap = self.dsp.to_dict()
                self._apply_dsp()
                return {"ok": True, "trim_silence": dsp_snap["trim_silence"], "dsp": dsp_snap}
            elif cmd == "crossfade":
                with self._state_lock:
                    try:
                        val = self.dsp.set_crossfade(float(arg if arg is not None else 0.0))
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "crossfade requires seconds (0..12)"}
                    dsp_snap = self.dsp.to_dict()
                if val <= 0 and self._xfade_active and self._user_volume is not None:
                    self._xfade_active = False
                    try:
                        self._mpv(["set_property", "volume", self._user_volume])
                    except Exception:
                        pass
                return {"ok": True, "crossfade_s": val, "dsp": dsp_snap}
            elif cmd == "sponsorblock":
                with self._state_lock:
                    if isinstance(arg, bool):
                        self.sponsorblock = arg
                    else:
                        self.sponsorblock = not self.sponsorblock
                    enabled = self.sponsorblock
                    cur_vid = self.current_track.get("id") if self.current_track else None
                if enabled and cur_vid:
                    self._resolve_sponsorblock(str(cur_vid))
                return {"ok": True, "sponsorblock": enabled}
            elif cmd in ("next_chapter", "prev_chapter"):
                with self._state_lock:
                    cur_vid = self.current_track.get("id") if self.current_track else None
                    cached_meta = (
                        self._queue_meta_cache.get(str(cur_vid), {}) if cur_vid else {}
                    )
                    chapters = (
                        (self.current_track.get("chapters") if self.current_track else None)
                        or cached_meta.get("chapters")
                        or []
                    )
                if not chapters:
                    return {"ok": False, "error": "current track has no chapters"}
                try:
                    cur_pos = float(self._mpv(["get_property", "time-pos"]) or 0.0)
                except Exception:
                    cur_pos = 0.0
                if cmd == "next_chapter":
                    nxt = next(
                        (
                            ch
                            for ch in chapters
                            if float(ch.get("start_time", 0.0)) > cur_pos + 0.5
                        ),
                        None,
                    )
                    if nxt is None:
                        return {"ok": False, "error": "already on last chapter"}
                    target_t = float(nxt["start_time"])
                    self._mpv(["seek", target_t, "absolute"])
                    return {"ok": True, "chapter": nxt.get("title"), "position": target_t}
                else:
                    prev_candidates = [
                        ch
                        for ch in chapters
                        if float(ch.get("start_time", 0.0)) <= cur_pos
                    ]
                    if not prev_candidates:
                        self._mpv(["seek", 0.0, "absolute"])
                        return {"ok": True, "chapter": chapters[0].get("title"), "position": 0.0}
                    cur_ch = prev_candidates[-1]
                    if cur_pos - float(cur_ch.get("start_time", 0.0)) > 3.0 or len(prev_candidates) == 1:
                        target_ch = cur_ch
                    else:
                        target_ch = prev_candidates[-2]
                    target_t = float(target_ch.get("start_time", 0.0))
                    self._mpv(["seek", target_t, "absolute"])
                    return {"ok": True, "chapter": target_ch.get("title"), "position": target_t}
            elif cmd == "like":
                with self._state_lock:
                    target_track = (
                        req.get("track")
                        if isinstance(req.get("track"), dict)
                        else (dict(self.current_track) if self.current_track else None)
                    )
                if not target_track and isinstance(arg, str):
                    vid_arg = _video_id(arg)
                    if vid_arg:
                        with self._state_lock:
                            target_track = dict(
                                self._queue_meta_cache.get(vid_arg) or {"id": vid_arg}
                            )
                if not target_track or not target_track.get("id"):
                    return {"ok": False, "error": "no track playing to like"}
                explicit = req.get("liked")
                if isinstance(explicit, bool):
                    now_liked = self.library.set_liked(target_track, explicit)
                else:
                    now_liked = self.library.toggle_like(target_track)
                if now_liked and (self.auto_cache_likes or bool(req.get("cache"))):
                    self._schedule_audio_cache(str(target_track["id"]))
                return {
                    "ok": True,
                    "liked": now_liked,
                    "id": target_track["id"],
                    "liked_count": self.library.liked_count(),
                }
            elif cmd == "liked_list":
                tracks = self.library.liked_tracks()
                return {"ok": True, "tracks": tracks, "count": len(tracks)}
            elif cmd == "play_liked":
                tracks = self.library.liked_tracks()
                if not tracks:
                    return {"ok": False, "error": "no liked songs saved yet"}
                loaded = self._load_track_list(
                    tracks, replace_current=bool(req.get("replace", True))
                )
                return {"ok": True, "loaded": loaded}
            elif cmd == "playlist_list":
                return {
                    "ok": True,
                    "playlists": self.library.list_playlists(),
                    "liked_count": self.library.liked_count(),
                }
            elif cmd == "playlist_save":
                name = str(req.get("name") or arg or "").strip()
                if not name:
                    return {"ok": False, "error": "playlist name required"}
                with self._state_lock:
                    snap_tracks: list[dict[str, Any]] = []
                    if self.current_track and self.current_track.get("id"):
                        snap_tracks.append(dict(self.current_track))
                    snap_tracks.extend(dict(t) for t in self.queue if t.get("id"))
                saved = self.library.replace_playlist_tracks(name, snap_tracks)
                return {"ok": True, "name": name, "count": saved}
            elif cmd == "playlist_add":
                name = str(req.get("name") or "").strip()
                if not name:
                    return {"ok": False, "error": "playlist name required"}
                with self._state_lock:
                    target_track = (
                        req.get("track")
                        if isinstance(req.get("track"), dict)
                        else (dict(self.current_track) if self.current_track else None)
                    )
                if not target_track and isinstance(arg, str):
                    vid_arg = _video_id(arg)
                    if vid_arg:
                        with self._state_lock:
                            target_track = dict(
                                self._queue_meta_cache.get(vid_arg) or {"id": vid_arg}
                            )
                if not target_track or not target_track.get("id"):
                    return {"ok": False, "error": "no track to add to playlist"}
                added = self.library.add_to_playlist(name, target_track)
                return {"ok": True, "name": name, "added": added}
            elif cmd == "playlist_load":
                name = str(req.get("name") or arg or "").strip()
                if not name:
                    return {"ok": False, "error": "playlist name required"}
                tracks = self.library.get_playlist_tracks(name)
                if not tracks:
                    return {"ok": False, "error": f"playlist '{name}' is empty or missing"}
                loaded = self._load_track_list(
                    tracks, replace_current=bool(req.get("replace", True))
                )
                return {"ok": True, "name": name, "loaded": loaded}
            elif cmd == "playlist_delete":
                name = str(req.get("name") or arg or "").strip()
                if not name:
                    return {"ok": False, "error": "playlist name required"}
                deleted = self.library.delete_playlist(name)
                return {"ok": True, "name": name, "deleted": deleted}
            elif cmd == "playlist_export":
                name = str(req.get("name") or arg or "").strip()
                if not name:
                    return {"ok": False, "error": "playlist name required"}
                return {"ok": True, "name": name, "m3u": self.library.export_m3u(name)}
            elif cmd == "playlist_import":
                m3u_text = req.get("m3u")
                url_arg = str(req.get("url") or arg or "").strip()
                save_name = str(req.get("name") or "").strip()
                replace_cur = bool(req.get("replace", False))
                if isinstance(m3u_text, str) and m3u_text.strip():
                    target_name = save_name or "Imported Playlist"
                    count = self.library.import_m3u(target_name, m3u_text)
                    tracks = self.library.get_playlist_tracks(target_name)
                    loaded = self._load_track_list(tracks, replace_current=replace_cur)
                    return {"ok": True, "name": target_name, "imported": count, "loaded": loaded}
                if not url_arg:
                    return {"ok": False, "error": "playlist URL or M3U text required"}
                try:
                    items = sources.playlist_items(url_arg, limit=int(req.get("limit") or 100))
                except sources.SourcesError as exc:
                    return {"ok": False, "error": str(exc)}
                if not items:
                    return {"ok": False, "error": "no tracks found in playlist"}
                if save_name:
                    self.library.replace_playlist_tracks(save_name, items)
                loaded = self._load_track_list(items, replace_current=replace_cur)
                return {"ok": True, "imported": len(items), "loaded": loaded, "name": save_name or None}
            elif cmd == "history":
                limit = max(1, min(100, int(req.get("limit") or 25)))
                return {
                    "ok": True,
                    "recent": self.library.recent_history(limit),
                    "most_played": self.library.most_played(limit),
                }
            elif cmd == "lyrics_offset":
                with self._state_lock:
                    cur_vid = (
                        str(req.get("id"))
                        if req.get("id")
                        else (self.current_track.get("id") if self.current_track else None)
                    )
                if not cur_vid:
                    return {"ok": False, "error": "no active track for lyrics_offset"}
                try:
                    off = float(arg if arg is not None else 0.0)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "lyrics_offset requires numeric seconds"}
                saved_off = self.library.set_lyrics_offset(str(cur_vid), off)
                return {"ok": True, "id": cur_vid, "lyrics_offset": saved_off}
            elif cmd == "cache_track":
                vid_target = (
                    _video_id(str(arg))
                    if arg is not None
                    else (self.current_track.get("id") if self.current_track else None)
                )
                if not vid_target:
                    return {"ok": False, "error": "no track to cache offline"}
                if req.get("blocking"):
                    try:
                        path = sources.cache_audio(str(vid_target))
                        self.library.set_cached_audio(str(vid_target), path)
                        self.library.enforce_audio_cache_budget()
                        return {"ok": True, "id": vid_target, "path": path}
                    except sources.SourcesError as exc:
                        return {"ok": False, "error": str(exc)}
                self._schedule_audio_cache(str(vid_target))
                return {"ok": True, "id": vid_target, "scheduled": True}
            elif cmd == "quit":
                self.stop.set()
                self._wake_event.set()
                self._refill_event.set()
            else:
                return {"ok": False, "error": f"unknown command: {cmd}"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(5.0)
                buf = b""
                while not self.stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > MAX_CMD_BYTES and b"\n" not in buf:
                        conn.sendall(b'{"ok":false,"error":"command too large"}\n')
                        break
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        if len(raw_line) > MAX_CMD_BYTES:
                            resp: dict[str, Any] = {
                                "ok": False,
                                "error": "command too large",
                            }
                        else:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                req = json.loads(line)
                                resp = self.handle_command(req)
                            except Exception as exc:
                                resp = {"ok": False, "error": str(exc)}
                        try:
                            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                        except OSError:
                            return
        except Exception as exc:
            print(f"[myx] command client error: {exc}", file=sys.stderr)
        finally:
            self._conn_sem.release()

    def _command_server(self) -> None:
        backoff = 1
        while not self.stop.is_set():
            server = None
            try:
                for sock_path in self._candidate_cmd_socks():
                    try:
                        sock_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                bound = False
                for sock_path in self._candidate_cmd_socks():
                    try:
                        old_umask = os.umask(0o077)
                        try:
                            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            server.bind(str(sock_path))
                            try:
                                os.chmod(sock_path, 0o600)
                            except OSError:
                                pass
                        finally:
                            os.umask(old_umask)
                        server.listen(16)
                        server.settimeout(0.25)
                        bound = True
                        break
                    except OSError:
                        if server:
                            server.close()
                        server = None
                if not bound or server is None:
                    raise OSError("failed to bind command socket on any candidate path")
                backoff = 1
                while not self.stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    if not self._conn_sem.acquire(blocking=False):
                        try:
                            conn.sendall(b'{"ok":false,"error":"server busy"}\n')
                        except OSError:
                            pass
                        conn.close()
                        continue
                    threading.Thread(
                        target=self._serve_client, args=(conn,), daemon=True
                    ).start()
            except Exception as exc:
                print(
                    f"[myx] command server error: {exc}, retrying in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
            finally:
                if server:
                    server.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_lyrics = "--no-lyrics" in argv
    idle = "--idle" in argv
    argv = [a for a in argv if a not in ("--no-lyrics", "--idle")]
    if not argv and not idle:
        print(
            "usage: python -m myx.player <url> [--idle] [--no-lyrics]", file=sys.stderr
        )
        return 2
    daemon = PlayerDaemon(argv[0] if argv else None, no_lyrics=no_lyrics)

    def _term(_signum: int, _frame: Any) -> None:
        daemon.stop.set()
        daemon._wake_event.set()
        daemon._refill_event.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
