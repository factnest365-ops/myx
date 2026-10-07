"""Drive tui.run() through a real PTY so the interactive path is observed.

Everything here exists because the mouse-reporting lifecycle is gated on
`out.isatty() and sys.stdin.isatty()`. Asserting on the ENABLE_MOUSE and
DISABLE_MOUSE constants proves nothing about whether `run()` writes them; only
driving the real loop and reading its output bytes does.

The child runs the actual `tui.run()`, quits immediately by sending 'q', and we
inspect what it wrote. `myx` needs no daemon for this: the home screen renders
from an empty state and 'q' exits before any command matters.
"""

import os
import pty

import sys
import time
from pathlib import Path

REPO = Path(__file__).parents[1]


def _run_pty(feed: bytes = b"q", timeout: float = 20.0) -> tuple[int, str, str]:
    """Run `tui.run()` on a PTY, send `feed`, return (rc, stdout, stderr)."""
    code = (
        "import sys\n"
        "from myx import tui\n"
        "tui.Renderer.load_library = lambda self, path=None: None\n"
        "tui.run(state_path=tui.STATE_PATH, out=sys.stdout, show_home=True)\n"
    )
    env = {**os.environ, "MYX_HOME": "/tmp", "TERM": "xterm-256color"}
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(REPO)
            os.execve(sys.executable, [sys.executable, "-c", code], env)
        except Exception:  # pragma: no cover - child failure path
            os._exit(127)

    chunks: list[bytes] = []
    deadline = time.time() + timeout
    # Give run() time to emit its startup sequences before feeding input.
    time.sleep(1.0)
    try:
        os.write(fd, feed)
    except OSError:
        pass
    while time.time() < deadline:
        try:
            ready, _, _ = __import__("select").select([fd], [], [], 0.2)
        except OSError:
            break
        if not ready:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        if b"\x1b[?1006l" in b"".join(chunks):
            # Disable seen: give the loop a moment to finish flushing.
            time.sleep(0.3)
            try:
                more = os.read(fd, 65536)
                if more:
                    chunks.append(more)
            except OSError:
                pass
            break
    try:
        os.close(fd)
    except OSError:
        pass
    _, status = os.waitpid(pid, 0)
    rc = os.waitstatus_to_exitcode(status)
    return rc, b"".join(chunks).decode(errors="ignore"), ""


def test_run_enables_mouse_reporting_on_start_over_a_real_tty():
    """A TTY session must turn mouse reporting on."""
    rc, out, _ = _run_pty()
    assert "\x1b[?1000h" in out, "mouse reporting (1000) not enabled on start"
    assert "\x1b[?1002h" in out, "drag tracking (1002) not enabled on start"
    assert "\x1b[?1006h" in out, "SGR coordinates (1006) not enabled on start"


def test_run_disables_mouse_reporting_on_exit_over_a_real_tty():
    """And must turn it back off, so the user's shell is not left broken."""
    rc, out, _ = _run_pty()
    assert "\x1b[?1006l" in out, "mouse reporting (1006) not disabled on exit"
    assert "\x1b[?1002l" in out, "drag tracking (1002) not disabled on exit"
    assert "\x1b[?1000l" in out, "mouse tracking (1000) not disabled on exit"
    assert rc == 0, f"run() should exit cleanly after 'q', got {rc}"


def test_enable_comes_before_disable_on_a_real_tty():
    """Ordering matters: reporting is on at start, off at exit, never inverted."""
    rc, out, _ = _run_pty()
    on_1000 = out.find("\x1b[?1000h")
    off_1000 = out.find("\x1b[?1000l")
    assert on_1000 >= 0 and off_1000 >= 0
    assert on_1000 < off_1000, "reporting was disabled before it was enabled"


def test_run_exits_the_alt_screen_on_a_real_tty():
    """Terminal state must be restored, not just mouse reporting."""
    rc, out, _ = _run_pty()
    assert "\x1b[?1049h" in out, "alt screen was never entered"
    assert "\x1b[?1049l" in out, "alt screen was never left"
    assert "\x1b[?25h" in out, "cursor was never restored"


def test_run_renders_the_home_screen_on_a_real_tty():
    rc, out, _ = _run_pty()
    import re

    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", out)
    assert "Search" in plain, "For You search bar missing on a real TTY"
    assert "Stations" in plain, "For You stations shelf missing on a real TTY"
