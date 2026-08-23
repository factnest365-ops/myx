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

    def run(self) -> None:
        self._cleanup_sockets()
        self._spawn_mpv()
        self._wait_for_socket(MPV_SOCK)
        self._mpv(["loadfile", self.url, "replace"])
        vid = _video_id(self.url)
        if vid:
            self.seen.add(vid)
        threads = [
            threading.Thread(target=self._command_server, daemon=True),
            threading.Thread(target=self._refill_loop, daemon=True),
            threading.Thread(target=self._state_loop, daemon=True),
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

    def _spawn_mpv(self) -> None:
        self.mpv = subprocess.Popen([
            "mpv",
            f"--input-ipc-server={MPV_SOCK}",
            "--idle=yes",
            "--no-video",
            "--ytdl-format=bestaudio",
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
        return _video_id(self._mpv(["get_property", "path"])) or _video_id(self.url)

    def _append_track(self, video_id: str, mark_seen: bool = True) -> bool:
        self._mpv(["loadfile", _url(video_id), "append"])
        if mark_seen:
            self.seen.add(video_id)
        return True

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

    def write_state(self) -> dict[str, Any]:
        playlist = self._playlist()
        pos = int(self._mpv(["get_property", "playlist-pos"]) or 0)
        current = playlist[pos] if 0 <= pos < len(playlist) else {}
        path = current.get("filename") or self._mpv(["get_property", "path"])
        vid = _video_id(path)
        queue = []
        for item in playlist[pos + 1 :]:
            item_id = _video_id(item.get("filename"))
            queue.append({"id": item_id, "title": item.get("title"), "duration": item.get("duration"), "thumb": None})
        state = {
            "version": 1,
            "playing": bool(playlist),
            "paused": bool(self._mpv(["get_property", "pause"])),
            "title": self._mpv(["get_property", "media-title"]) or current.get("title"),
            "artist": None,
            "id": vid,
            "url": path,
            "thumb": None,
            "duration": self._mpv(["get_property", "duration"]),
            "position": self._mpv(["get_property", "time-pos"]),
            "volume": self._mpv(["get_property", "volume"]),
            "queue": queue,
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return state

    def handle_command(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        arg = req.get("arg")
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
                self._mpv(["set_property", "volume", int(arg)])
            elif cmd == "shuffle":
                self._mpv(["playlist-shuffle"])
            elif cmd == "play_id":
                self._mpv(["loadfile", _url(str(arg)), "replace"])
                self.seen.add(str(arg))
            elif cmd == "load":
                self._mpv(["loadfile", str(arg), "replace"])
                vid = _video_id(str(arg))
                if vid:
                    self.seen.add(vid)
            elif cmd == "jump":
                self._mpv(["seek", float(arg), "absolute"])
            elif cmd == "quit":
                self.stop.set()
            else:
                return {"ok": False, "error": f"unknown command: {cmd}"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _command_server(self) -> None:
        try:
            CMD_SOCK.unlink()
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(CMD_SOCK))
            server.listen(8)
            server.settimeout(0.25)
            while not self.stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    for line in conn.makefile("r", encoding="utf-8"):
                        try:
                            req = json.loads(line)
                            resp = self.handle_command(req)
                        except Exception as exc:
                            resp = {"ok": False, "error": str(exc)}
                        conn.sendall((json.dumps(resp) + "\n").encode())


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
