"""MYX command-line entrypoint."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import gettempdir

from . import tui

LOCK_PATH = Path(gettempdir()) / "myx.lock"
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", Path(gettempdir()) / "myx-cmd.sock"))
MPV_SOCK = Path(os.environ.get("MYX_MPV_SOCK", Path(gettempdir()) / "myx-mpv.sock"))
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
    parser.add_argument("--state", action="store_true", help="Print current player state as JSON and exit (agent-friendly)")
    parser.add_argument("--no-lyrics", action="store_true", help="Battery/memory saver: skip lyrics fetch (headless/agent mode)")
    parser.add_argument("extra_search", nargs="*", help="Search words when no URL flag is given (e.g. myx lofi hip hop)")
    sub = parser.add_subparsers(dest="subcommand")
    mcp_p = sub.add_parser("mcp", help="Run MYX as an MCP server over stdio (for agents)")
    mcp_p.add_argument("--stdio", action="store_true", help="Use stdio transport (default)")
    return parser.parse_args(argv)


def split_search_words(argv: list[str] | None) -> tuple[list[str] | None, str | None]:
    """Split bare search words from argv before argparse sees them.

    Needed because argparse routes a second positional to the `mcp`
    subcommand choice. `myx lofi hip hop` -> (rest, "lofi hip hop").
    A real URL/id in position 1 stays a seed, never a search.
    """
    if not argv:
        return argv, None
    if argv[0].startswith("-") or argv[0] == "mcp":
        return argv, None
    if len(argv) == 1:
        return argv, None
    first = argv[0]
    looks_like_seed = (
        "://" in first
        or first.startswith("www.")
        or (len(first) >= 6 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in first))
    )
    if looks_like_seed:
        return argv, None
    return [argv[0]], " ".join(argv)


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


def boot_daemon(seed: str | None = None, no_lyrics: bool = False) -> subprocess.Popen[str] | None:
    if daemon_running():
        return None
    args = [sys.executable, "-m", "myx.player"]
    if seed:
        args.append(seed)
    if no_lyrics:
        args.append("--no-lyrics")
    env = {**os.environ, "MYX_NO_LYRICS": "1"} if no_lyrics else None
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env)
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
    _kill_mpv_socket()
    try:
        if LOCK_PATH.read_text().strip() == str(proc.pid):
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def queue_search(query: str) -> None:
    tui.send_command("search", arg=query)


def pick_interactive(message: str = "Search music") -> str | None:
    """Minimal guided picker: works with or without a TTY.

    Returns the chosen query/URL or None if the user aborts.
    """
    prompt = f"{message} (type a search or paste a link, empty aborts): "
    try:
        if not sys.stdin.isatty():
            line = sys.stdin.readline()
        else:
            line = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    line = (line or "").strip()
    return line or None


def main(argv: list[str] | None = None) -> int:
    argv, bare_search = split_search_words(list(argv) if argv is not None else None)
    args = parse_args(argv)
    # Agent-native entrypoints — no bin checks, no TUI needed
    if getattr(args, "subcommand", None) == "mcp":
        from .mcp import serve_stdio
        return serve_stdio()
    if getattr(args, "state", False):
        import json as _json
        from pathlib import Path as _Path
        from tempfile import gettempdir as _gettmp
        p = _Path(gettempdir()) / "myx-state.json"
        try:
            print(_Path(p).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(_json.dumps({"playing": False, "paused": True, "queue": []}))
        return 0
    check_bins()
    if bare_search and not args.url and not args.search:
        args.search = bare_search
    elif getattr(args, "extra_search", None) and not args.url and not args.search:
        args.search = " ".join(args.extra_search)
    # No-args guided entry: pick a search/link instead of idling on "Waiting".
    if not args.url and not args.search and not args.lyrics_only:
        choice = pick_interactive()
        if not choice:
            print("MYX — pass a link, or: myx --search \"lofi\" | myx mcp --stdio | myx --state")
            return 0
        looks_like_url = "://" in choice or choice.startswith("www.") or len(choice) >= 6 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in choice)
        if looks_like_url:
            args.url = choice
        else:
            args.search = choice
    # if daemon already running and a seed url was given, append it instead of ignoring
    already = daemon_running()
    proc = boot_daemon(args.url, no_lyrics=getattr(args, "no_lyrics", False))
    if proc:
        atexit.register(cleanup, proc)
        signal.signal(signal.SIGTERM, lambda *_: (cleanup(proc), raise_sigterm()))
        # Give the command server a short head start. The TUI tolerates absent state.
        deadline = time.time() + 3
        while time.time() < deadline and not CMD_SOCK.exists() and proc.poll() is None:
            time.sleep(0.05)
    elif already and args.url:
        try:
            tui.send_command("load", arg=args.url)
        except Exception as exc:
            print(f"could not queue seed: {exc}", file=sys.stderr)
    if args.search:
        try:
            queue_search(args.search)
        except Exception as exc:
            print(f"search failed: {exc}", file=sys.stderr)
    if args.lyrics_only:
        # Lyrics-only aid must not kill a daemon we just booted.
        if proc:
            atexit.unregister(cleanup)
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


def _kill_mpv_socket() -> None:
    if not MPV_SOCK.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(MPV_SOCK))
            payload = {"command": ["quit"], "request_id": 1}
            sock.sendall((json.dumps(payload) + "\n").encode())
    except Exception:
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
