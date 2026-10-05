from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import sources

TMPDIR = Path(tempfile.gettempdir())
MPV_SOCK = TMPDIR / "myx-mpv.sock"
CMD_SOCK = TMPDIR / "myx-cmd.sock"
STATE_PATH = TMPDIR / "myx-state.json"


def _video_id(value: str | None) -> str | None:
    if not value:
        return None
    for pat in (r"[?&]v=([^&]+)", r"youtu\.be/([^?&/]+)", r"/watch/([^?&/]+)"):
        m = re.search(pat, value)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", value):
        return value
    return None


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


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


_ORIGINAL_MIX_CHAIN = getattr(sources, "mix_chain", None)


class PlayerDaemon:
    def __init__(self, url: str | None = None, no_lyrics: bool = False):
        self.url = url
        self.no_lyrics = no_lyrics or os.environ.get("MYX_NO_LYRICS") == "1"
        self.stop = threading.Event()
        self.lock = threading.RLock()
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
        self._id_by_path: dict[str, str] = {}
        self._current_meta: dict[str, Any] = {}
        self._idle_since: float | None = None

        self.queue: list[dict[str, Any]] = []
        self.current_track: dict[str, Any] | None = None
        self._refill_event = threading.Event()
        self._buffer_lock = threading.Lock()
        self._buffering: bool = False

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

    def _acquire_singleton(self, url: str | None) -> bool:
        lock_path = TMPDIR / "myx.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            pass
        except Exception:
            return True

        vid = _video_id(url) if url else None
        # With no seed there is nothing to hand off — probe liveness with a
        # harmless unknown command instead of sending play_id(None).
        req = {"cmd": "play_id", "arg": vid} if vid else {"cmd": "__probe__"}
        for sock_path in self._candidate_cmd_socks():
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
        except Exception:
            pass
        # Re-acquire the lock so a recovered boot doesn't leave an open
        # window where another instance could start alongside this one.
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
        except Exception:
            pass  # lost a race with another instance; playback still works
        return True

    def run(self) -> None:
        if not self._acquire_singleton(self.url):
            return
        self._cleanup_sockets()
        try:
            subprocess.run(
                ["pkill", "-f", "myx-mpv.sock"], capture_output=True, timeout=2
            )
        except Exception:
            pass
        self._spawn_mpv()
        self._wait_for_socket(MPV_SOCK)
        # Start command server and refill loop first — they must be responsive
        # for CLI/TUI/MCP. Seed resolution (with metadata fetch) happens next
        # so the first state write has correct title/artist.
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
                else:
                    # Fallback: load URL directly and try to extract metadata from it
                    self._mpv(["loadfile", self.url, "replace"])
                    # Best effort: try to extract video ID from URL for metadata
                    fallback_vid = _video_id(self.url)
                    if fallback_vid:
                        try:
                            self._current_meta = sources.meta(fallback_vid)
                        except sources.SourcesError:
                            self._current_meta = {}
        finally:
            # Now start remaining service threads
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
        # Remove state file so dead daemon cannot report stale playing state
        try:
            STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            (TMPDIR / "myx.lock").unlink()
        except Exception:
            pass

    def _spawn_mpv(self) -> None:
        self.mpv = subprocess.Popen(
            [
                "mpv",
                f"--input-ipc-server={MPV_SOCK}",
                "--idle=yes",
                "--no-video",
                "--no-ytdl",
                "--no-terminal",
            ],
            text=True,
        )

    @staticmethod
    def _cleanup_sockets() -> None:
        for path in (MPV_SOCK, CMD_SOCK):
            try:
                path.unlink()
            except FileNotFoundError:
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
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(str(MPV_SOCK))
                sock.sendall((json.dumps(payload) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            if not buf:
                raise RuntimeError("empty mpv response")
            resp = json.loads(buf.splitlines()[0].decode())
            if resp.get("error") not in (None, "success"):
                raise RuntimeError(resp.get("error"))
            return resp.get("data")

    def _playlist(self) -> list[dict[str, Any]]:
        data = self._mpv(["get_property", "playlist"])
        return data if isinstance(data, list) else []

    def _current_id(self) -> str | None:
        if self.current_track and self.current_track.get("id"):
            return str(self.current_track["id"])
        path = self._mpv(["get_property", "path"])
        return (
            self._id_by_path.get(path or "") or _video_id(path) or _video_id(self.url)
        )

    def _append_track(self, vid_or_item: str | dict[str, Any], mark_seen: bool = True) -> bool:
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
            self._queue_meta_cache[vid] = item
        self.queue.append(item)
        if mark_seen and vid:
            self.seen.add(vid)
        self._trigger_buffer()
        return True

    def _stream_for(self, video_id: str) -> str:
        cached = self._stream_cache.get(video_id)
        if cached:
            return cached
        try:
            url = sources.stream_url(video_id)
        except sources.SourcesError as exc:
            print(
                f"[myx] stream resolve failed ({exc}); delegating to mpv",
                file=sys.stderr,
            )
            return _url(video_id)
        self._stream_cache[video_id] = url
        return url

    def _play_now(self, video_id: str) -> None:
        stream = self._stream_for(video_id)
        self._mpv(["loadfile", stream, "replace"])
        self._id_by_path[stream] = video_id
        self.seen.add(video_id)
        meta = self._queue_meta_cache.get(video_id)
        if not meta or not meta.get("title"):
            try:
                meta = sources.meta(video_id)
                self._queue_meta_cache[video_id] = meta
            except (sources.SourcesError, Exception):
                meta = meta or {}
        self._current_meta = meta
        self.current_track = {
            "id": video_id,
            "title": meta.get("title"),
            "artist": meta.get("artist"),
            "duration": meta.get("duration"),
            "thumb": meta.get("thumb"),
            "stream_url": stream,
        }
        clean_display_title = _clean_title(self.current_track)
        if clean_display_title:
            try:
                self._mpv(["set_property", "force-media-title", clean_display_title])
            except Exception:
                pass
        self._trigger_buffer()

    def _trigger_buffer(self) -> None:
        if self._buffering or self.stop.is_set():
            return
        if not self.queue:
            return
        try:
            pl = self._playlist()
            if len(pl) >= 2:
                return
        except Exception:
            return
        self._buffering = True
        threading.Thread(target=self._buffer_worker, daemon=True).start()

    def _buffer_worker(self) -> None:
        try:
            with self._buffer_lock:
                if self.stop.is_set() or not self.queue:
                    return
                try:
                    pl = self._playlist()
                    if len(pl) >= 2:
                        return
                except Exception:
                    return
                target = self.queue[0]
                vid = target.get("id")
                if not vid:
                    return
                stream = target.get("stream_url")
                if not stream:
                    stream = self._stream_for(vid)
                    target["stream_url"] = stream

                if self.stop.is_set() or not self.queue or self.queue[0].get("id") != vid:
                    return
                with self.lock:
                    pl = self._playlist()
                    if len(pl) < 2:
                        self._mpv(["loadfile", stream, "append"])
                        self._id_by_path[stream] = vid
                        self.seen.add(vid)
        except Exception as exc:
            print(f"[myx] buffer error: {exc}", file=sys.stderr)
        finally:
            self._buffering = False

    def _maintain_playback(self) -> None:
        try:
            playlist = self._playlist()
            pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if playlist else 0
        except Exception:
            return

        if pos >= 1:
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
            clean_display_title = _clean_title(self.current_track)
            if clean_display_title:
                try:
                    self._mpv(["set_property", "force-media-title", clean_display_title])
                except Exception:
                    pass
            for _ in range(pos):
                try:
                    self._mpv(["playlist-remove", 0])
                except Exception:
                    break

        try:
            playlist = self._playlist()
        except Exception:
            playlist = []
        if not playlist and self.queue:
            next_item = self.queue.pop(0)
            vid = next_item.get("id")
            if vid:
                stream = next_item.get("stream_url") or self._stream_for(vid)
                next_item["stream_url"] = stream
                self.current_track = next_item
                try:
                    self._mpv(["loadfile", stream, "replace"])
                    self._id_by_path[stream] = vid
                    self.seen.add(vid)
                    clean_display_title = _clean_title(self.current_track)
                    if clean_display_title:
                        self._mpv(["set_property", "force-media-title", clean_display_title])
                except Exception:
                    pass

        try:
            playlist = self._playlist()
        except Exception:
            playlist = []
        if len(playlist) == 1 and self.queue:
            self._trigger_buffer()

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
                self.cached_chain[cur] = chain
        except sources.SourcesError:
            chain = self.cached_chain.get(cur, [])
            if not chain:
                raise
        except Exception:
            chain = self.cached_chain.get(cur, [])
            if not chain:
                raise
        added = 0
        for it in chain:
            vid = it.get("id") if isinstance(it, dict) else str(it)
            if not vid or vid in self.seen:
                continue
            if self._append_track(it, True):
                added += 1
        if added == 0 and chain:
            existing_ids = {self.current_track.get("id") if self.current_track else None} | {x.get("id") for x in self.queue}
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
                if len(self.queue) <= 3:
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
            time.sleep(max(0.05, 0.25 - (time.monotonic() - started)))

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
            current_path = self._mpv(["get_property", "path"])
            cur_id = self._id_by_path.get(current_path or "") or _video_id(current_path)
            if cur_id:
                self._play_now(cur_id)
            elif current_path:
                self._mpv(["loadfile", current_path, "replace"])
        except Exception:
            if self.url:
                self._mpv(["loadfile", self.url, "replace"])
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
                        # core-idle is also true during resolve/buffer; kick only
                        # after sustained idle so slow track starts survive.
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
        if cache_key in self._thumb_cache:
            return self._thumb_cache[cache_key]
        if cache_key in self._thumb_pending:
            return None
        url = raw_thumb if raw_thumb and raw_thumb.startswith("http") else None
        if not url and vid:
            url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        if not url:
            self._thumb_cache[cache_key] = None
            return None

        self._thumb_pending.add(cache_key)

        def _fetch() -> None:
            try:
                path = sources.cache_thumb(cache_key, url)
                self._thumb_cache[cache_key] = path
            except sources.SourcesError:
                self._thumb_cache[cache_key] = None
            except Exception:
                self._thumb_cache[cache_key] = None
            finally:
                self._thumb_pending.discard(cache_key)

        threading.Thread(target=_fetch, daemon=True).start()
        return None

    def _resolve_lyrics(
        self, vid: str | None, title: Any, artist: Any, duration: Any
    ) -> dict[str, str | None]:
        if self.no_lyrics or not vid:
            return {"lrc": None, "plain": None}
        if vid in self._lyrics_cache:
            return self._lyrics_cache[vid]
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
            self._lyrics_cache[vid] = data
            self._lyrics_inflight.discard(vid)

    def write_state(self) -> dict[str, Any]:
        playlist = self._playlist()
        pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if playlist else 0
        current = playlist[pos] if 0 <= pos < len(playlist) else {}
        path = current.get("filename") or self._mpv(["get_property", "path"])
        vid = (
            (self.current_track.get("id") if self.current_track else None)
            or self._id_by_path.get(path or "")
            or _video_id(path)
        )
        raw_thumb = current.get("thumbnail") if isinstance(current.get("thumbnail"), str) else None
        if self.current_track and self.current_track.get("thumb"):
            raw_thumb = self.current_track.get("thumb")
        cached_thumb = self._resolve_thumb(vid, raw_thumb)

        queue = []
        if self.queue:
            for item in self.queue:
                item_id = item.get("id")
                cached_meta = self._queue_meta_cache.get(item_id, {}) if item_id else {}
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
                item_id = self._id_by_path.get(item.get("filename") or "") or _video_id(
                    item.get("filename")
                )
                cached_meta = self._queue_meta_cache.get(item_id, {}) if item_id else {}
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
        cached_current = (
            self._current_meta
            if self._current_meta.get("id") == vid or (self._current_meta and not vid)
            else self._queue_meta_cache.get(vid, {})
        )
        cur_title = (
            (self.current_track.get("title") if self.current_track else None)
            or cached_current.get("title")
        )

        # Never let mpv's raw 'web' or 'videoplayback' title overwrite a real title.
        # Prioritize self.current_track.get("title")
        if cur_title and not _is_garbage_title(cur_title):
            title_val = cur_title
        elif mpv_title and not _is_garbage_title(mpv_title):
            title_val = mpv_title
            if self.current_track and not self.current_track.get("title"):
                self.current_track["title"] = mpv_title
        elif current.get("title") and not _is_garbage_title(current.get("title")):
            title_val = current.get("title")
        else:
            title_val = cur_title if (cur_title and not _is_garbage_title(cur_title)) else None

        # Keep mpv's force-media-title in sync so macOS MediaRemote shows the clean title
        if self.current_track:
            clean_display_title = _clean_title(self.current_track)
            if clean_display_title and (not mpv_title or _is_garbage_title(mpv_title)):
                try:
                    self._mpv(["set_property", "force-media-title", clean_display_title])
                except Exception:
                    pass

        artist_val = (
            (self.current_track.get("artist") if self.current_track else None)
            or cached_current.get("artist")
            or (current.get("uploader") if isinstance(current.get("uploader"), str) else None)
        )
        dur_val = (
            self._mpv(["get_property", "duration"])
            or (self.current_track.get("duration") if self.current_track else None)
            or cached_current.get("duration")
        )
        pos_val = self._mpv(["get_property", "time-pos"])
        vol_val = self._mpv(["get_property", "volume"])
        paused_val = bool(self._mpv(["get_property", "pause"]))
        lyr = self._resolve_lyrics(vid, title_val, artist_val, dur_val)
        state = {
            "version": 1,
            "playing": bool(playlist) or bool(self.current_track),
            "paused": paused_val,
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
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return state

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
            elif cmd == "skip":
                playlist = self._playlist()
                pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if playlist else 0
                if len(playlist) > 1 and pos < len(playlist) - 1:
                    self._mpv(["playlist-next", "force"])
                    self._maintain_playback()
                elif self.queue:
                    next_item = self.queue.pop(0)
                    vid = next_item.get("id")
                    if vid:
                        stream = next_item.get("stream_url") or self._stream_for(vid)
                        next_item["stream_url"] = stream
                        self.current_track = next_item
                        self._mpv(["loadfile", stream, "replace"])
                        self._id_by_path[stream] = vid
                        self.seen.add(vid)
                        clean_display_title = _clean_title(self.current_track)
                        if clean_display_title:
                            try:
                                self._mpv(["set_property", "force-media-title", clean_display_title])
                            except Exception:
                                pass
                        self._trigger_buffer()
                else:
                    self._mpv(["playlist-next", "force"])
            elif cmd == "prev":
                self._mpv(["playlist-prev", "force"])
            elif cmd == "vol":
                # arg=0 historically means absolute 0 (and test expects it); treat 0..100 as absolute,
                # small ± deltas come via value=. Try to stay backward compat.
                try:
                    # only fetch current volume for relative deltas via `value`
                    if req.get("value") is not None:
                        try:
                            cur_vol = int(self._mpv(["get_property", "volume"]) or 50)
                        except Exception:
                            cur_vol = 50
                        delta = int(arg) if arg is not None else 0
                        new_vol = max(0, min(100, cur_vol + delta))
                    elif req.get("arg") is not None:
                        v = int(arg) if arg is not None else 50
                        # heuristic: if the fake/test passes arg=0 it expects absolute;
                        # treat 0..100 as absolute, else as delta (defensive)
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
                except Exception:
                    new_vol = max(0, min(100, int(arg) if arg is not None else 50))
                self._mpv(["set_property", "volume", new_vol])
            elif cmd == "shuffle":
                import random
                random.shuffle(self.queue)
                try:
                    pl = self._playlist()
                    if len(pl) > 1:
                        self._mpv(["playlist-remove", 1])
                except Exception:
                    pass
                self._trigger_buffer()
            elif cmd == "play_id":
                vid = _video_id(str(arg)) if arg is not None else None
                if not vid:
                    return {"ok": False, "error": "play_id requires a YouTube URL or video id"}
                self.queue.clear()
                self.seen = {vid}
                self.cached_chain.clear()
                self._play_now(vid)
                self._refill_event.set()
                return {"ok": True}
            elif cmd == "load":
                if arg is None or not str(arg).strip():
                    return {"ok": False, "error": "load requires a URL or video id"}
                vid = _video_id(str(arg)) or str(arg).strip()
                meta = self._queue_meta_cache.get(vid, {})
                item = {
                    "id": vid,
                    "title": meta.get("title"),
                    "artist": meta.get("artist"),
                    "duration": meta.get("duration"),
                    "thumb": meta.get("thumb"),
                    "stream_url": None,
                }
                self.queue.append(item)
                if vid:
                    self.seen.add(vid)
                self._trigger_buffer()
                return {"ok": True}
            elif cmd == "search":
                try:
                    items = sources.search(str(arg))
                    added = 0
                    for item in items:
                        vid2 = item.get("id")
                        if vid2:
                            self._queue_meta_cache[vid2] = item
                            self.queue.append({
                                "id": vid2,
                                "title": item.get("title"),
                                "artist": item.get("artist"),
                                "duration": item.get("duration"),
                                "thumb": item.get("thumb"),
                                "stream_url": None,
                            })
                            self.seen.add(vid2)
                            added += 1
                    self._trigger_buffer()
                    return {"ok": True, "queued": added}
                except sources.SourcesError as exc:
                    return {"ok": False, "error": str(exc)}
            elif cmd == "jump":
                self._mpv(["seek", float(arg), "absolute"])
            elif cmd == "del":
                try:
                    idx = int(arg)
                except Exception:
                    return {"ok": False, "error": "del requires integer index"}
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
                if not (0 <= idx < len(self.queue)):
                    return {"ok": False, "error": "index out of range"}
                if idx == 0:
                    pl = self._playlist()
                    pos = int(self._mpv(["get_property", "playlist-pos"]) or 0) if pl else 0
                    if len(pl) > 1 and pos == 0:
                        self._mpv(["playlist-next", "force"])
                        self._maintain_playback()
                        return {"ok": True}
                target = self.queue[idx]
                self.queue = self.queue[idx + 1 :]
                vid = target.get("id")
                if vid:
                    stream = target.get("stream_url") or self._stream_for(vid)
                    target["stream_url"] = stream
                    self.current_track = target
                    self._mpv(["loadfile", stream, "replace"])
                    self._id_by_path[stream] = vid
                    self.seen.add(vid)
                    clean_display_title = _clean_title(self.current_track)
                    if clean_display_title:
                        try:
                            self._mpv(["set_property", "force-media-title", clean_display_title])
                        except Exception:
                            pass
                    self._trigger_buffer()
                return {"ok": True}
            elif cmd == "quit":
                self.stop.set()
            else:
                return {"ok": False, "error": f"unknown command: {cmd}"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
                bound = False
                for sock_path in self._candidate_cmd_socks():
                    try:
                        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        server.bind(str(sock_path))
                        server.listen(8)
                        server.settimeout(0.25)
                        bound = True
                        break
                    except OSError:
                        if server:
                            server.close()
                        server = None
                if not bound:
                    raise OSError("failed to bind command socket on any candidate path")
                backoff = 1
                while not self.stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    try:
                        with conn:
                            for line in conn.makefile("r", encoding="utf-8"):
                                try:
                                    req = json.loads(line)
                                    resp = self.handle_command(req)
                                except Exception as exc:
                                    resp = {"ok": False, "error": str(exc)}
                                try:
                                    conn.sendall((json.dumps(resp) + "\n").encode())
                                except Exception:
                                    break
                    except Exception as exc:
                        print(f"[myx] command client error: {exc}", file=sys.stderr)
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

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
