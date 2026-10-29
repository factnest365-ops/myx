"""Transport tests for myx.ipc - the only owner of the wire.

Hermetic: real AF_UNIX sockets, but all inside a short /tmp directory and
driven by a stub server in a thread. No network, no mpv, no yt-dlp.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from myx import ipc


# AF_UNIX sun_path is capped near 104 bytes, so avoid pytest's long tmp_path.
@pytest.fixture()
def short_dir():
    path = Path(tempfile.mkdtemp(prefix="myx-ipc-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class StubDaemon:
    """One-thread Unix socket server that records requests and replies."""

    def __init__(self, sock_path: Path, reply: dict | None = None, silent: bool = False):
        self.sock_path = sock_path
        self.reply = {"ok": True} if reply is None else reply
        self.silent = silent
        self.requests: list[dict] = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(8)
        self._srv.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> StubDaemon:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._srv.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                if self.silent:
                    time.sleep(0.3)  # accept, then hold the line without answering
                    continue
                line = conn.makefile("rwb").readline()
                if not line:
                    continue
                self.requests.append(json.loads(line))
                if self.reply:
                    conn.sendall((json.dumps(self.reply) + "\n").encode())


def test_send_command_round_trip(short_dir):
    sock = short_dir / "cmd.sock"
    with StubDaemon(sock, {"ok": True, "n": 7}) as stub:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.01)
        reply = ipc.send_command("vol", sock_path=sock, value=5)
    assert reply == {"ok": True, "n": 7}
    assert stub.requests == [{"cmd": "vol", "value": 5}]


def test_send_command_raises_oserror_when_socket_absent(short_dir):
    with pytest.raises(ipc.IpcError) as exc:
        ipc.send_command("toggle", sock_path=short_dir / "missing.sock")
    assert isinstance(exc.value, OSError)


def test_send_command_gives_up_on_a_silent_peer(short_dir, monkeypatch):
    monkeypatch.setattr(ipc, "REPLY_TIMEOUT_S", 0.05)
    sock = short_dir / "silent.sock"
    with StubDaemon(sock, silent=True):
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.01)
        with pytest.raises(ipc.IpcError):
            ipc.send_command("toggle", sock_path=sock)


def test_read_state_file_accepts_only_objects(short_dir):
    missing = short_dir / "missing.json"
    assert ipc.read_state_file(missing) is None

    bad = short_dir / "bad.json"
    bad.write_text("{not json")
    assert ipc.read_state_file(bad) is None

    listed = short_dir / "list.json"
    listed.write_text("[1, 2]")
    assert ipc.read_state_file(listed) is None

    good = short_dir / "good.json"
    good.write_text(json.dumps({"playing": True}))
    assert ipc.read_state_file(good) == {"playing": True}


def test_read_state_file_refuses_symlink(short_dir):
    victim = short_dir / "victim.json"
    victim.write_text(json.dumps({"playing": True}))
    link = short_dir / "link.json"
    link.symlink_to(victim)
    assert ipc.read_state_file(link) is None


def test_read_lock_requires_a_live_pid(short_dir):
    lock = short_dir / "myx.lock"

    assert ipc.read_lock(lock_path=lock) is None  # absent

    lock.write_text(str(os.getpid()))
    assert ipc.read_lock(lock_path=lock) == os.getpid()

    lock.write_text("999999999")  # no such process
    assert ipc.read_lock(lock_path=lock) is None

    lock.write_text("not-a-pid")
    assert ipc.read_lock(lock_path=lock) is None


def test_read_lock_refuses_symlink(short_dir):
    real = short_dir / "real.lock"
    real.write_text(str(os.getpid()))
    link = short_dir / "link.lock"
    link.symlink_to(real)
    assert ipc.read_lock(lock_path=link) is None


def test_write_lock_is_private_and_round_trips(short_dir):
    lock = short_dir / "myx.lock"
    ipc.write_lock(os.getpid(), lock_path=lock)
    assert lock.read_text() == str(os.getpid())
    assert (lock.stat().st_mode & 0o077) == 0
    assert ipc.read_lock(lock_path=lock) == os.getpid()


def test_write_lock_refuses_symlink(short_dir):
    victim = short_dir / "victim.lock"
    victim.write_text("untouched")
    link = short_dir / "link.lock"
    link.symlink_to(victim)
    with pytest.raises(PermissionError):
        ipc.write_lock(os.getpid(), lock_path=link)
    assert victim.read_text() == "untouched"


def test_is_alive_needs_a_listening_socket(short_dir):
    sock = short_dir / "cmd.sock"
    assert ipc.is_alive(cmd_sock=sock) is False

    sock.write_text("")  # a plain file is not a server
    assert ipc.is_alive(cmd_sock=sock) is False

    sock.unlink()
    with StubDaemon(sock):
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.01)
        assert ipc.is_alive(cmd_sock=sock) is True


def test_is_alive_checks_the_pid_when_a_lock_is_given(short_dir):
    sock = short_dir / "cmd.sock"
    lock = short_dir / "myx.lock"
    with StubDaemon(sock):
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.01)
        lock.write_text("999999999")
        assert ipc.is_alive(cmd_sock=sock, lock_path=lock) is False
        lock.write_text(str(os.getpid()))
        assert ipc.is_alive(cmd_sock=sock, lock_path=lock) is True


def test_is_alive_does_not_wait_for_a_reply(short_dir, monkeypatch):
    monkeypatch.setattr(ipc, "LIVENESS_TIMEOUT_S", 0.05)
    sock = short_dir / "cmd.sock"
    with StubDaemon(sock):
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.01)
        started = time.monotonic()
        assert ipc.is_alive(cmd_sock=sock) is True
        assert time.monotonic() - started < 1.0


def test_is_trusted_rejects_symlinks(short_dir):
    real = short_dir / "real"
    real.write_text("x")
    link = short_dir / "link"
    link.symlink_to(real)
    assert ipc.is_trusted(real) is True
    assert ipc.is_trusted(link) is False
    assert ipc.is_trusted(short_dir / "absent") is True


def test_fallback_tmpdirs_honour_the_env(short_dir, monkeypatch):
    monkeypatch.setenv("MYX_FALLBACK_TMP", str(short_dir / "extra"))
    monkeypatch.setenv("TMPDIR", str(short_dir / "envtmp"))
    dirs = ipc.fallback_tmpdirs()
    assert Path("/tmp") in dirs
    assert ipc.TMPDIR in dirs
    assert short_dir / "extra" in dirs
    assert short_dir / "envtmp" in dirs


def test_paths_are_overridable_by_env(tmp_path, monkeypatch):
    """A fresh interpreter is the only honest way to test import-time env reads."""
    import subprocess
    import sys

    env = {
        **os.environ,
        "MYX_CMD_SOCK": str(tmp_path / "c.sock"),
        "MYX_MPV_SOCK": str(tmp_path / "m.sock"),
        "MYX_STATE": str(tmp_path / "s.json"),
        "MYX_LOCK": str(tmp_path / "l.lock"),
    }
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from myx import ipc;"
            "print(ipc.CMD_SOCK.name, ipc.MPV_SOCK.name, ipc.STATE_PATH.name, ipc.LOCK_PATH.name)",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["c.sock", "m.sock", "s.json", "l.lock"]
