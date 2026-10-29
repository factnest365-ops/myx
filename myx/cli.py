"""MYX command-line entrypoint."""

from __future__ import annotations

import argparse
import atexit
import datetime
import json
import os
import re
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

_RESERVED_WORDS = {
    "mcp",
    "doctor",
    "play",
    "search",
    "help",
    "status",
    "version",
    "quit",
    "skip",
    "prev",
    "toggle",
    "pause",
    "resume",
    "stop",
    "next",
    "clear",
    "shuffle",
    "queue",
    "lyrics",
}

# ANSI helpers
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


def _is_safe_path(path: Path) -> bool:
    """Reject symlinks and files owned by a different UID."""
    try:
        if path.is_symlink():
            return False
        if path.exists() and hasattr(os, "getuid"):
            if path.stat().st_uid != os.getuid():
                return False
        return True
    except OSError:
        return False


def _looks_like_url_or_id(arg: str) -> bool:
    """Heuristic: does this look like a YouTube URL or video ID?"""
    if not arg or arg.startswith("-"):
        return False
    if arg.lower() in _RESERVED_WORDS:
        return False
    if "youtube.com" in arg or "youtu.be" in arg or "music.youtube.com" in arg:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,64}", arg))


def _add_common_flags(parser: argparse.ArgumentParser, *, include_help: bool = True) -> None:
    parser.add_argument("--search", help="Open search results into the queue")
    parser.add_argument(
        "--lyrics-only", action="store_true", help="Developer aid: start in lyrics view"
    )
    parser.add_argument(
        "--state",
        action="store_true",
        help="Print current player state as JSON and exit (agent-friendly)",
    )
    parser.add_argument(
        "--no-lyrics",
        action="store_true",
        help="Battery/memory saver: skip lyrics fetch (headless/agent mode)",
    )
    parser.add_argument(
        "--quit", action="store_true", help="Stop playback and exit daemon"
    )
    parser.add_argument("--skip", action="store_true", help="Skip to next track")
    parser.add_argument("--prev", action="store_true", help="Go to previous track")
    parser.add_argument("--toggle", action="store_true", help="Toggle pause/play")
    parser.add_argument("--mute", action="store_true", help="Toggle audio mute")
    parser.add_argument(
        "--loop",
        choices=("off", "one", "all"),
        default=None,
        help="Set repeat mode (off, one, all)",
    )
    parser.add_argument("--clear", action="store_true", help="Clear upcoming queue")
    parser.add_argument("--vol", type=int, help="Set volume 0-100")
    parser.add_argument(
        "--seek", type=float, default=None, help="Seek relative seconds (e.g. 10 or -5)"
    )
    parser.add_argument(
        "--jump", type=float, default=None, help="Jump to absolute position in seconds"
    )
    parser.add_argument(
        "--like", action="store_true", help="Like / unlike the currently playing song"
    )
    parser.add_argument(
        "--liked", action="store_true", help="Play saved Liked Songs from library"
    )
    parser.add_argument(
        "--eq",
        type=str,
        default=None,
        help="Set EQ/DSP preset (flat, bass_boost, vocal, rock, spatial, slowed_reverb, nightcore, cycle)",
    )
    parser.add_argument(
        "--normalize", action="store_true", help="Toggle loudness normalization"
    )
    parser.add_argument(
        "--crossfade", type=float, default=None, help="Set smart crossfade seconds (0-12)"
    )
    parser.add_argument(
        "--sponsorblock", action="store_true", help="Toggle SponsorBlock auto-skip"
    )
    parser.add_argument(
        "--import-playlist",
        dest="import_playlist",
        type=str,
        default=None,
        help="Import YouTube/YT Music playlist URL or local .m3u file into queue",
    )
    parser.add_argument(
        "--cache-track",
        dest="cache_track",
        action="store_true",
        help="Cache current song offline as Opus/M4A",
    )
    if include_help:
        parser.add_argument("-h", "--help", action="help")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse args with custom logic to allow URL as first positional arg alongside subcommands."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return argparse.Namespace(
            url=None,
            search=None,
            lyrics_only=False,
            state=False,
            no_lyrics=False,
            subcommand=None,
            stdio=False,
            fix=False,
            quit=False,
            skip=False,
            prev=False,
            toggle=False,
            mute=False,
            loop=None,
            clear=False,
            vol=None,
            seek=None,
            jump=None,
            like=False,
            liked=False,
            eq=None,
            normalize=False,
            crossfade=None,
            sponsorblock=False,
            import_playlist=None,
            cache_track=False,
        )

    first = argv[0]
    if _looks_like_url_or_id(first):
        parser = argparse.ArgumentParser(prog="myx", add_help=False)
        parser.add_argument("url", nargs="?")
        _add_common_flags(parser, include_help=True)
        args = parser.parse_args(argv)
        args.subcommand = None
        args.stdio = False
        args.fix = False
        return args

    parser = argparse.ArgumentParser(prog="myx")
    parser.add_argument("url", nargs="?", help="YouTube or YT Music URL/video id seed")
    _add_common_flags(parser, include_help=False)
    sub = parser.add_subparsers(dest="subcommand")
    mcp_p = sub.add_parser(
        "mcp", help="Run MYX as an MCP server over stdio (for agents)"
    )
    mcp_p.add_argument(
        "--stdio", action="store_true", help="Use stdio transport (default)"
    )
    doc_p = sub.add_parser("doctor", help="Check setup and daemon health")
    doc_p.add_argument(
        "--fix",
        action="store_true",
        help="Attempt to auto-fix issues (brew install/upgrade, stale lock cleanup)",
    )
    return parser.parse_args(argv)


def _bin_version(bin_name: str) -> str | None:
    for flag in ("--version", "-version"):
        try:
            proc = subprocess.run(
                [bin_name, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = (proc.stdout or proc.stderr or "").strip().splitlines()
            if out and out[0].strip():
                return out[0].strip()[:120]
            if proc.returncode == 0 and out:
                return out[0].strip()[:120] or None
        except Exception:
            continue
    return None


def _yt_dlp_stale(version: str | None) -> bool:
    if not version:
        return False
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", version)
    if not m:
        return False
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        vdate = datetime.date(y, mo, d)
        today = datetime.date.today()
        return (today - vdate).days > 30
    except Exception:
        return False


def doctor_check() -> dict:
    """Return a report dict with bin status, yt-dlp staleness, and daemon health."""
    bins: dict[str, dict] = {}
    for name, hint in REQUIRED_BINS.items():
        installed = bool(shutil.which(name))
        version = _bin_version(name) if installed else None
        bins[name] = {"installed": installed, "version": version, "hint": hint}

    yt_version = bins.get("yt-dlp", {}).get("version")
    yt_dlp_stale = bool(
        bins.get("yt-dlp", {}).get("installed") and _yt_dlp_stale(yt_version)
    )

    socket_exists = CMD_SOCK.exists() and not CMD_SOCK.is_symlink()
    lock_pid: int | None = None
    lock_running = False
    stale_lock = False
    try:
        if _is_safe_path(LOCK_PATH):
            raw = LOCK_PATH.read_text().strip()
            if raw:
                lock_pid = int(raw)
                lock_running = _pid_running(lock_pid)
                if not lock_running:
                    stale_lock = True
    except Exception:
        lock_pid = None
        lock_running = False
        stale_lock = False
        if LOCK_PATH.exists():
            stale_lock = True

    socket_perms_ok = True
    if socket_exists:
        try:
            mode = CMD_SOCK.stat().st_mode & 0o077
            socket_perms_ok = mode == 0
        except OSError:
            socket_perms_ok = True

    daemon = {
        "socket_exists": socket_exists,
        "socket_perms_ok": socket_perms_ok,
        "lock_pid": lock_pid,
        "lock_running": lock_running,
        "healthy": bool(socket_exists and lock_running),
        "stale_lock": stale_lock,
    }

    report = {
        "mpv": bins["mpv"],
        "yt-dlp": bins["yt-dlp"],
        "ffmpeg": bins["ffmpeg"],
        "yt_dlp": bins["yt-dlp"],
        "yt_dlp_stale": yt_dlp_stale,
        "yt-dlp_stale": yt_dlp_stale,
        "daemon": daemon,
        "bins": bins,
        "brew_available": bool(shutil.which("brew")),
    }
    return report


def _print_doctor(report: dict, fix: bool = False) -> int:
    missing = [
        name
        for name, info in report.get("bins", {}).items()
        if not info.get("installed")
    ]
    brew_available = report.get("brew_available", False)

    print(f"{_BOLD}MYX doctor{_RESET}")
    print()
    for name in ("mpv", "yt-dlp", "ffmpeg"):
        info = report.get(name) or report.get("bins", {}).get(name, {})
        installed = info.get("installed")
        version = info.get("version")
        hint = info.get("hint", "")
        if installed:
            ver_str = f" ({version})" if version else ""
            print(f"  {name}: {_GREEN}OK{_RESET}{_DIM}{ver_str}{_RESET}")
        else:
            print(f"  {name}: {_RED}MISSING{_RESET} — {hint}")

    if report.get("yt_dlp_stale") or report.get("yt-dlp_stale"):
        print(
            f"  yt-dlp: {_YELLOW}stale{_RESET} — yt-dlp is older than 30 days; run `brew upgrade yt-dlp` or `yt-dlp -U`"
        )

    daemon = report.get("daemon", {})
    if daemon.get("healthy"):
        print(f"  daemon: {_GREEN}healthy{_RESET} (pid {daemon.get('lock_pid')})")
        if daemon.get("socket_perms_ok") is False:
            print(f"  ipc: {_YELLOW}socket has broad permissions{_RESET} (run `myx doctor --fix`)")
            if fix and CMD_SOCK.exists() and not CMD_SOCK.is_symlink():
                try:
                    os.chmod(CMD_SOCK, 0o600)
                except OSError:
                    pass
    elif daemon.get("stale_lock"):
        print(
            f"  daemon: {_YELLOW}stale lock{_RESET} (pid {daemon.get('lock_pid')} not running)"
        )
    else:
        print(f"  daemon: {_DIM}not running{_RESET}")

    if missing:
        brew_cmd = f"brew install {' '.join(missing)}"
        if fix and brew_available:
            print(f"\n  fixing: running `{brew_cmd}` ...")
            try:
                subprocess.run(["brew", "install", *missing], timeout=120)
                print(f"  {_GREEN}brew install completed{_RESET}")
            except Exception as exc:
                print(f"  {_RED}brew install failed: {exc}{_RESET}")
                print(f"  try manually: `{brew_cmd}`")
        else:
            if brew_available:
                print(
                    f"\n  fix: `{brew_cmd}`"
                    + (" (run `myx doctor --fix` to auto-install)" if not fix else "")
                )
            else:
                print(f"\n  fix: install missing bins: {', '.join(missing)}")
                print(f"       macOS: `{brew_cmd}`")

    if report.get("yt_dlp_stale") or report.get("yt-dlp_stale"):
        if fix:
            if brew_available:
                print("  fixing yt-dlp: `brew upgrade yt-dlp` ...")
                try:
                    subprocess.run(["brew", "upgrade", "yt-dlp"], timeout=120)
                except Exception as exc:
                    print(f"  brew upgrade failed: {exc} — try `yt-dlp -U`")
            else:
                print("  fix yt-dlp: `yt-dlp -U`")
                try:
                    subprocess.run(["yt-dlp", "-U"], timeout=60)
                except Exception:
                    pass
        else:
            print("  hint: update yt-dlp via `brew upgrade yt-dlp` or `yt-dlp -U`")

    if daemon.get("stale_lock"):
        if fix:
            try:
                LOCK_PATH.unlink(missing_ok=True)
                print(f"  cleaned stale lockfile at {LOCK_PATH}")
            except Exception as exc:
                print(f"  failed to clean lockfile: {exc}")
        else:
            print(
                f"  hint: stale lock at {LOCK_PATH} — run `myx doctor --fix` to clean up"
            )

    has_issue = bool(missing or report.get("yt_dlp_stale") or daemon.get("stale_lock"))
    if not has_issue:
        print(f"\n{_GREEN}All checks passed.{_RESET}")
    else:
        if not fix:
            print(f"\n{_DIM}Run `myx doctor --fix` to attempt auto-fixes.{_RESET}")

    return 0 if not missing else 1


def run_doctor(fix: bool = False) -> int:
    report = doctor_check()
    return _print_doctor(report, fix=fix)


def check_bins() -> None:
    missing = [name for name in REQUIRED_BINS if not shutil.which(name)]
    if missing:
        hints = "; ".join(REQUIRED_BINS[name] for name in missing)
        raise SystemExit(
            f"missing required binary: {', '.join(missing)}. {hints} — run `myx doctor` for setup help"
        )


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_lock() -> int | None:
    try:
        if not _is_safe_path(LOCK_PATH):
            return None
        pid = int(LOCK_PATH.read_text().strip())
    except Exception:
        return None
    return pid if _pid_running(pid) else None


def write_lock(pid: int) -> None:
    if LOCK_PATH.is_symlink():
        raise PermissionError("refusing to write symlinked lockfile")
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK_PATH, flags, 0o600)
    try:
        os.write(fd, str(pid).encode("utf-8"))
    finally:
        os.close(fd)


def daemon_running() -> bool:
    pid = read_lock()
    if not pid or not CMD_SOCK.exists() or not _is_safe_path(CMD_SOCK):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(CMD_SOCK))
            return True
    except OSError:
        return False


def _read_live_state() -> dict:
    empty = {"playing": False, "paused": True, "queue": []}
    if not daemon_running():
        return empty
    path = Path(os.environ.get("MYX_STATE") or (Path(gettempdir()) / "myx-state.json"))
    if not _is_safe_path(path):
        return empty
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else empty
    except (OSError, ValueError, TypeError):
        return empty


SOCK_WAIT_S = 5.0


def _wait_for_cmd_sock(
    proc: subprocess.Popen[str] | None = None, timeout: float = SOCK_WAIT_S
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if CMD_SOCK.exists():
            return True
        if proc is not None and proc.poll() is not None:
            return CMD_SOCK.exists()
        if proc is None and read_lock() is None:
            return CMD_SOCK.exists()
        time.sleep(0.05)
    return CMD_SOCK.exists()


def boot_daemon(
    seed: str | None = None, no_lyrics: bool = False
) -> subprocess.Popen[str] | None:
    if read_lock():
        return None
    args = [sys.executable, "-m", "myx.player"]
    if seed:
        args.append(seed)
    else:
        args.append("--idle")
    if no_lyrics:
        args.append("--no-lyrics")
    env = {**os.environ, "MYX_NO_LYRICS": "1"} if no_lyrics else None
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env
    )
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
        if _is_safe_path(LOCK_PATH) and LOCK_PATH.read_text().strip() == str(proc.pid):
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def queue_search(query: str) -> None:
    tui.send_command("search", arg=query)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if getattr(args, "subcommand", None) == "mcp":
        from .mcp import serve_stdio

        return serve_stdio()
    if getattr(args, "state", False):
        print(json.dumps(_read_live_state()))
        return 0
    if getattr(args, "subcommand", None) == "doctor":
        fix = bool(getattr(args, "fix", False))
        return run_doctor(fix=fix)

    headless_cmd = None
    if getattr(args, "quit", False):
        headless_cmd = ("quit", None)
    elif getattr(args, "skip", False):
        headless_cmd = ("skip", None)
    elif getattr(args, "prev", False):
        headless_cmd = ("prev", None)
    elif getattr(args, "toggle", False):
        headless_cmd = ("toggle", None)
    elif getattr(args, "mute", False):
        headless_cmd = ("mute", None)
    elif getattr(args, "loop", None) is not None:
        headless_cmd = ("loop", args.loop)
    elif getattr(args, "clear", False):
        headless_cmd = ("clear", None)
    elif getattr(args, "vol", None) is not None:
        headless_cmd = ("vol", args.vol)
    elif getattr(args, "seek", None) is not None:
        headless_cmd = ("seek", args.seek)
    elif getattr(args, "jump", None) is not None:
        headless_cmd = ("jump", args.jump)
    elif getattr(args, "like", False):
        headless_cmd = ("like", None)
    elif getattr(args, "liked", False):
        headless_cmd = ("play_liked", None)
    elif getattr(args, "eq", None) is not None:
        headless_cmd = ("eq", args.eq)
    elif getattr(args, "normalize", False):
        headless_cmd = ("normalize", None)
    elif getattr(args, "crossfade", None) is not None:
        headless_cmd = ("crossfade", args.crossfade)
    elif getattr(args, "sponsorblock", False):
        headless_cmd = ("sponsorblock", None)
    elif getattr(args, "cache_track", False):
        headless_cmd = ("cache_track", None)
    elif getattr(args, "import_playlist", None) is not None:
        imp_val = str(args.import_playlist).strip()
        p_m3u = Path(imp_val).expanduser()
        if p_m3u.is_file() and not p_m3u.is_symlink() and p_m3u.stat().st_size <= 2 * 1024 * 1024:
            if not daemon_running():
                print("daemon not running", file=sys.stderr)
                return 1
            try:
                tui.send_command(
                    "playlist_import",
                    m3u=p_m3u.read_text(encoding="utf-8", errors="replace"),
                    name=p_m3u.stem,
                )
                return 0
            except (OSError, ValueError) as exc:
                print(f"command failed: {exc}", file=sys.stderr)
                return 1
        headless_cmd = ("playlist_import", imp_val)

    if headless_cmd:
        if not daemon_running():
            print("daemon not running", file=sys.stderr)
            return 1
        try:
            tui.send_command(headless_cmd[0], arg=headless_cmd[1])
            return 0
        except (OSError, ValueError) as exc:
            print(f"command failed: {exc}", file=sys.stderr)
            return 1

    check_bins()
    already = daemon_running()
    proc = boot_daemon(args.url, no_lyrics=getattr(args, "no_lyrics", False))
    if proc:
        atexit.register(cleanup, proc)
        signal.signal(signal.SIGTERM, lambda *_: (cleanup(proc), raise_sigterm()))
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
    if not MPV_SOCK.exists() or not _is_safe_path(MPV_SOCK):
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
