"""MYX IPC - one owner for socket paths, state file, lockfile and liveness.

The daemon, the TUI, the MCP server and one-shot CLI calls all share this
wire. Before this module each of them re-declared the paths and re-implemented
the liveness probe, so a fix landed in some callers and not others (AUDIT
BUG-022 was patched twice, by hand).

Channels:
    CMD_SOCK    Unix socket, one JSON object per line, client -> daemon
    MPV_SOCK    Unix socket, mpv's own JSON IPC, daemon -> mpv
    STATE_PATH  atomically replaced file, daemon -> everyone else
    LOCK_PATH   daemon pid, the singleton guard

This module holds transport only. Policy stays with the caller: the TUI
tolerates a missing state file and shows a placeholder, `myx --state` refuses
to report state from a dead daemon. Callers keep their own module-level path
bindings so a test can point one process at a temp directory.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

TMPDIR = Path(tempfile.gettempdir())
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", TMPDIR / "myx-cmd.sock"))
MPV_SOCK = Path(os.environ.get("MYX_MPV_SOCK", TMPDIR / "myx-mpv.sock"))
STATE_PATH = Path(os.environ.get("MYX_STATE", TMPDIR / "myx-state.json"))
LOCK_PATH = Path(os.environ.get("MYX_LOCK", TMPDIR / "myx.lock"))

MAX_MESSAGE = 1024 * 1024  # 1 MiB cap on one reply
# Commands that wait on yt-dlp before they can answer, so they get a longer reply budget.
SLOW_COMMANDS = frozenset({"search", "search_query"})
CONNECT_TIMEOUT_S = 2.0
LIVENESS_TIMEOUT_S = 0.5
REPLY_TIMEOUT_S = 2.5
SLOW_REPLY_TIMEOUT_S = 22.0


def empty_state() -> dict[str, Any]:
    """State when no daemon answers. A fresh dict, so callers cannot poison it."""
    return {"playing": False, "paused": True, "queue": []}


class IpcError(OSError):
    """The daemon could not be reached or did not answer.

    OSError so existing `except OSError` handlers around socket sends keep
    working; callers that catch Exception still see it.
    """


def is_trusted(path: Path) -> bool:
    """Reject symlinks and paths owned by a different UID."""
    try:
        if path.is_symlink():
            return False
        if path.exists() and hasattr(os, "getuid"):
            if path.stat().st_uid != os.getuid():
                return False
        return True
    except OSError:
        return False


def fallback_tmpdirs() -> list[Path]:
    """Directories to search for a daemon started under a different TMPDIR.

    A daemon launched from a shell with a private TMPDIR writes its sockets
    there; clients launched from elsewhere must still find it.
    """
    dirs = [Path("/tmp"), TMPDIR]
    if tmp := os.environ.get("MYX_FALLBACK_TMP"):
        dirs.append(Path(tmp))
    if env_tmp := os.environ.get("TMPDIR"):
        dirs.append(Path(env_tmp))
    return dirs


def read_state_file(path: Path) -> dict[str, Any] | None:
    """Parsed state object, or None when missing, unsafe, or not an object."""
    if not is_trusted(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def read_lock(lock_path: Path | None = None) -> int | None:
    """Pid of a running daemon, or None when the lockfile is absent or dead."""
    path = lock_path or LOCK_PATH
    if not is_trusted(path):
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def write_lock(pid: int, lock_path: Path | None = None) -> None:
    path = lock_path or LOCK_PATH
    if path.is_symlink():
        raise PermissionError("refusing to write symlinked lockfile")
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, str(pid).encode("utf-8"))
    finally:
        os.close(fd)


def is_alive(cmd_sock: Path | None = None, lock_path: Path | None = None) -> bool:
    """Daemon reachability that never waits for a reply.

    A successful connect is proof enough: the command server may be busy with a
    slow search and still be alive (AUDIT BUG-022). Pass `lock_path` to require
    a live pid as well.
    """
    if lock_path is not None and not read_lock(lock_path):
        return False
    path = cmd_sock or CMD_SOCK
    if not path.exists() or not is_trusted(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(LIVENESS_TIMEOUT_S)
            sock.connect(str(path))
            return True
    except OSError:
        return False


def send_command(
    cmd: str, *, sock_path: Path | None = None, **payload: Any
) -> dict[str, Any]:
    """Send one JSON command line and return the daemon's reply object."""
    path = sock_path or CMD_SOCK
    message = {"cmd": cmd, **payload}
    reply_timeout = SLOW_REPLY_TIMEOUT_S if cmd in SLOW_COMMANDS else REPLY_TIMEOUT_S
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(str(path))
            sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
            sock.settimeout(reply_timeout)
            chunks: list[bytes] = []
            total = 0
            while total < MAX_MESSAGE:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        raise IpcError(f"daemon not reachable at {path}: {exc}") from exc
    lines = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
    return json.loads(lines[0] if lines else "{}") or {}
