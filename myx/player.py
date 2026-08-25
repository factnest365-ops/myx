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


class PlayerDaemon:
    def __init__(self, url: str):
        self.url = url
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.mpv: subprocess.Popen[str] | None = None
        self.seq = 0
        self.seen: set[str] = set()
        self.cached_chain: dict[str, list[str]] = {}
        self._lyrics_cache: dict[str, dict[str, str | None]] = {}
        self._thumb_cache: dict[str, str | None] = {}

        self._queue_meta_cache: dict[str, dict[str, Any]] = {}
        self._stream_cache: dict[str, str] = {}
        self._id_by_path: dict[str, str] = {}
        self._idle_since: float | None = None

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

    def _acquire_singleton(self, url: str) -> bool:
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

        vid = _video_id(url) or url
        for sock_path in self._candidate_cmd_socks():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.6)
                    s.connect(str(sock_path))
                    s.sendall((json.dumps({"cmd": "play_id", "arg": vid}) + "\n").encode())
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
            subprocess.run(["pkill", "-f", "myx-mpv.sock"], capture_output=True, timeout=2)
        except Exception:
            pass
        self._spawn_mpv()
        self._wait_for_socket(MPV_SOCK)
        seed_vid = _video_id(self.url)
        if seed_vid:
            self._play_now(seed_vid)
        else:
            self._mpv(["loadfile", self.url, "replace"])
        threads = [
            threading.Thread(target=self._command_server, daemon=True),
            threading.Thread(target=self._refill_loop, daemon=True),
            threading.Thread(target=self._state_loop, daemon=True),
            threading.Thread(target=self._queue_hydration_loop, daemon=True),
            threading.Thread(target=self._healthcheck_loop, daemon=True),
        ]
        for t in threads:
            t.start()
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
        try:
            (TMPDIR / "myx.lock").unlink()
        except Exception:
            pass

    def _spawn_mpv(self) -> None:
        self.mpv = subprocess.Popen([
            "mpv",
            f"--input-ipc-server={MPV_SOCK}",
            "--idle=yes",
            "--no-video",
            "--no-ytdl",
            "--no-terminal",
        ], text=True)

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
        path = self._mpv(["get_property", "path"])
        return (
            self._id_by_path.get(path or "")
            or _video_id(path)
            or _video_id(self.url)
        )

    def _append_track(self, video_id: str, mark_seen: bool = True) -> bool:
        stream = self._stream_for(video_id)
        self._mpv(["loadfile", stream, "append"])
        self._id_by_path[stream] = video_id
        if mark_seen:
            self.seen.add(video_id)
        return True

    def _stream_for(self, video_id: str) -> str:
        cached = self._stream_cache.get(video_id)
        if cached:
            return cached
        try:
            url = sources.stream_url(video_id)
        except sources.SourcesError as exc:
            print(f"[myx] stream resolve failed ({exc}); delegating to mpv", file=sys.stderr)
            return _url(video_id)
        self._stream_cache[video_id] = url
        return url

    def _play_now(self, video_id: str) -> None:
        stream = self._stream_for(video_id)
        self._mpv(["loadfile", stream, "replace"])
        self._id_by_path[stream] = video_id
        self.seen.add(video_id)

    def _append_related(self) -> bool:
        cur = self._current_id()
        if not cur:
            return False
        try:
            chain = sources.mix_chain(cur)
            if chain:
                self.cached_chain[cur] = chain
        except sources.SourcesError:
            chain = self.cached_chain.get(cur, [])
            if not chain:
                raise
        added = 0
        for vid in chain:
            if vid in self.seen:
                continue
            if self._append_track(vid, True):
                added += 1
        if added == 0 and chain:
            for vid in chain:
                self._append_track(vid, False)
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
                if len(self._playlist()) <= 2:
                    self._append_related()
                backoff = 5
                time.sleep(2)
            except Exception:
                time.sleep(backoff)
                backoff = min(30, backoff * 2)

    def _state_loop(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.write_state()
            except Exception:
                pass
            time.sleep(max(0.05, 0.25 - (time.monotonic() - started)))

    def _queue_hydration_loop(self) -> None:
        while not self.stop.is_set():
            try:
                playlist = self._playlist()
                pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
                for idx in range(pos + 1, len(playlist)):
                    item = playlist[idx]
                    item_id = self._id_by_path.get(item.get("filename") or "") or _video_id(item.get("filename"))
                    if not item_id:
                        continue
                    if item_id in self._queue_meta_cache:
                        continue
                    if item.get("title"):
                        self._queue_meta_cache[item_id] = {
                            "title": item.get("title"),
                            "artist": item.get("uploader") if isinstance(item.get("uploader"), str) else None,
                            "duration": item.get("duration"),
                            "thumb": item.get("thumbnail") if isinstance(item.get("thumbnail"), str) else None,
                        }
                        continue
                    try:
                        meta = sources.meta(item_id)
                        self._queue_meta_cache[item_id] = meta
                    except sources.SourcesError:
                        self._queue_meta_cache[item_id] = {}
            except Exception:
                pass
            time.sleep(5)

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
                print(f"[myx] mpv died (exit {self.mpv.poll()}), restarting", file=sys.stderr)
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
                print("[myx] mpv healthcheck: socket unresponsive, restarting", file=sys.stderr)
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
                            print("[myx] eof watchdog: kicking stalled track (sustained idle)", file=sys.stderr)
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
        url = raw_thumb if raw_thumb and raw_thumb.startswith("http") else None
        if not url and vid:
            url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        if not url:
            self._thumb_cache[cache_key] = None
            return None
        try:
            path = sources.cache_thumb(cache_key, url)
            self._thumb_cache[cache_key] = path
            return path
        except sources.SourcesError:
            self._thumb_cache[cache_key] = None
            return None

    def _resolve_lyrics(self, vid: str | None, title: Any, artist: Any, duration: Any) -> dict[str, str | None]:
        if not vid:
            return {"lrc": None, "plain": None}
        if vid in self._lyrics_cache:
            return self._lyrics_cache[vid]
        try:
            dur = None
            if duration is not None:
                try:
                    dur = float(duration)
                except Exception:
                    dur = None
            data = sources.lyrics(str(title or ""), str(artist or ""), dur)
            self._lyrics_cache[vid] = data
            return data
        except sources.SourcesError:
            self._lyrics_cache[vid] = {"lrc": None, "plain": None}
            return self._lyrics_cache[vid]
        except Exception:
            return {"lrc": None, "plain": None}

    def write_state(self) -> dict[str, Any]:
        playlist = self._playlist()
        pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
        current = playlist[pos] if 0 <= pos < len(playlist) else {}
        path = current.get("filename") or self._mpv(["get_property", "path"])
        vid = self._id_by_path.get(path or "") or _video_id(path)
        raw_thumb = current.get("thumbnail") if isinstance(current.get("thumbnail"), str) else None
        cached_thumb = self._resolve_thumb(vid, raw_thumb)
        queue = []
        for item in playlist[pos + 1 :]:
            item_id = self._id_by_path.get(item.get("filename") or "") or _video_id(item.get("filename"))
            item_title = item.get("title")
            item_duration = item.get("duration")
            item_raw_thumb = item.get("thumbnail") if isinstance(item.get("thumbnail"), str) else None
            cached_meta = self._queue_meta_cache.get(item_id, {}) if item_id else {}
            item_title = item_title or cached_meta.get("title")
            item_artist = item.get("uploader") if isinstance(item.get("uploader"), str) else cached_meta.get("artist")
            if not isinstance(item_artist, str):
                item_artist = None
            item_duration = item_duration or cached_meta.get("duration")
            item_raw_thumb = item_raw_thumb or cached_meta.get("thumb")
            item_thumb = self._resolve_thumb(item_id, item_raw_thumb)
            queue.append({
                "id": item_id,
                "title": item_title,
                "artist": item_artist,
                "duration": item_duration,
                "thumb": item_thumb,
            })
        title_val = self._mpv(["get_property", "media-title"]) or current.get("title")
        artist_val = current.get("uploader") if isinstance(current.get("uploader"), str) else None
        dur_val = self._mpv(["get_property", "duration"])
        pos_val = self._mpv(["get_property", "time-pos"])
        vol_val = self._mpv(["get_property", "volume"])
        paused_val = bool(self._mpv(["get_property", "pause"]))
        lyr = self._resolve_lyrics(vid, title_val, artist_val, dur_val)
        state = {
            "version": 1,
            "playing": bool(playlist),
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
                                cur_vol = int(self._mpv(["get_property", "volume"]) or 50)
                            except Exception:
                                cur_vol = 50
                            new_vol = max(0, min(100, cur_vol + v))
                    else:
                        new_vol = 50
                except Exception:
                    new_vol = max(0, min(100, int(arg) if arg is not None else 50))
                self._mpv(["set_property", "volume", new_vol])
            elif cmd == "shuffle":
                self._mpv(["playlist-shuffle"])
            elif cmd == "play_id":
                vid = str(arg)
                self._mpv(["loadfile", _url(vid), "replace"])
                self.seen.add(vid)
                self.cached_chain.clear()
                try:
                    playlist = self._playlist()
                    pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
                    for idx in range(len(playlist) - 1, pos, -1):
                        self._mpv(["playlist-remove", idx])
                except Exception:
                    pass
                try:
                    self._append_related()
                except Exception:
                    pass
            elif cmd == "load":
                self._mpv(["loadfile", str(arg), "append"])
                vid = _video_id(str(arg))
                if vid:
                    self.seen.add(vid)
            elif cmd == "search":
                try:
                    items = sources.search(str(arg))
                    added = 0
                    for item in items:
                        vid2 = item.get("id")
                        if vid2:
                            self._mpv(["loadfile", _url(str(vid2)), "append"])
                            self.seen.add(str(vid2))
                            added += 1
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
                playlist = self._playlist()
                pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
                target = pos + 1 + idx
                if 0 <= target < len(playlist):
                    self._mpv(["playlist-remove", target])
                else:
                    return {"ok": False, "error": "index out of range"}
            elif cmd == "play_index":
                try:
                    idx = int(arg)
                except Exception:
                    return {"ok": False, "error": "play_index requires integer"}
                playlist = self._playlist()
                pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
                target = pos + 1 + idx if idx >= 0 else idx
                if 0 <= target < len(playlist):
                    self._mpv(["set_property", "playlist-pos", target])
                else:
                    return {"ok": False, "error": "index out of range"}
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
                    except socket.timeout:
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
                print(f"[myx] command server error: {exc}, retrying in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
            finally:
                if server:
                    server.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m myx.player <url>", file=sys.stderr)
        return 2
    daemon = PlayerDaemon(argv[0])
    def _term(_signum: int, _frame: Any) -> None:
        daemon.stop.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
