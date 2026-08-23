"""MYX command-line entrypoint."""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import gettempdir

from . import tui

LOCK_PATH = Path(gettempdir()) / "myx.lock"
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", Path(gettempdir()) / "myx-cmd.sock"))
REQUIRED_BINS = {
    "mpv": "install mpv (macOS: brew install mpv)",
    "yt-dlp": "install yt-dlp (macOS: brew install yt-dlp)",
    "ffmpeg": "install ffmpeg (macOS: brew install ffmpeg)",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="myx")
    parser.add_argument("url", nargs="?", help="YouTube or YT Music URL/video id seed")
    parser.add_argument("--search", help="Open search results into the queue")
    parser.add_argument("--lyrics-only", action="store_true", help="Developer aid: start in lyrics view")
    return parser.parse_args(argv)


def check_bins() -> None:
    missing = [name for name in REQUIRED_BINS if not shutil.which(name)]
    if missing:
        hints = "; ".join(REQUIRED_BINS[name] for name in missing)
        raise SystemExit(f"missing required binary: {', '.join(missing)}. {hints}")


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_lock() -> int | None:
    try:
        pid = int(LOCK_PATH.read_text().strip())
    except Exception:
        return None
    return pid if _pid_running(pid) else None


def write_lock(pid: int) -> None:
    LOCK_PATH.write_text(str(pid))


def daemon_running() -> bool:
    pid = read_lock()
    return bool(pid and CMD_SOCK.exists())


def boot_daemon(seed: str | None = None) -> subprocess.Popen[str] | None:
    if daemon_running():
        return None
    args = [sys.executable, "-m", "myx.player"]
    if seed:
        args.append(seed)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    write_lock(proc.pid)
    return proc


def cleanup(proc: subprocess.Popen[str] | None) -> None:
    if not proc:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        if LOCK_PATH.read_text().strip() == str(proc.pid):
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def queue_search(query: str) -> None:
    from myx import sources  # type: ignore

    for item in sources.search(query):
        tui.send_command("load", url=item.get("id") or item.get("url") or "")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    check_bins()
    proc = boot_daemon(args.url)
    if proc:
        atexit.register(cleanup, proc)
        signal.signal(signal.SIGTERM, lambda *_: (cleanup(proc), raise_sigterm()))
        # Give the command server a short head start. The TUI tolerates absent state.
        deadline = time.time() + 3
        while time.time() < deadline and not CMD_SOCK.exists() and proc.poll() is None:
            time.sleep(0.05)
    if args.search:
        try:
            queue_search(args.search)
        except Exception as exc:
            print(f"search failed: {exc}", file=sys.stderr)
    if args.lyrics_only:
        renderer = tui.Renderer()
        renderer.view = "lyrics"
        renderer.render_once()
        return 0
    try:
        return tui.run()
    finally:
        cleanup(proc)


def raise_sigterm() -> None:
    raise SystemExit(143)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
