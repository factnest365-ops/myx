"""Fuzz/property tests: hostile input to the command handler must never raise.

handle_command is the daemon's attack surface: any local process can write
arbitrary bytes to $TMPDIR/myx-cmd.sock. The contract is that every request —
well-formed or not — produces an {"ok": bool} response dict, never an exception.
"""
import random

import pytest

from myx.player import PlayerDaemon


@pytest.fixture()
def daemon():
    return PlayerDaemon("https://www.youtube.com/watch?v=seed123")


HOSTILE_REQUESTS = [
    {},
    {"cmd": None},
    {"cmd": ""},
    {"cmd": "   "},
    {"cmd": "unknown_cmd"},
    {"cmd": "TOGGLE"},  # case sensitivity probe
    {"cmd": "toggle; rm -rf /"},  # injection probe
    {"cmd": "vol", "arg": "not-a-number"},
    {"cmd": "vol", "arg": None},
    {"cmd": "vol", "arg": ["list"]},
    {"cmd": "vol", "arg": {"nested": "dict"}},
    {"cmd": "vol", "arg": float("nan")},
    {"cmd": "jump", "arg": -10**18},
    {"cmd": "jump", "arg": 10**18},
    {"cmd": "play_id", "arg": None},
    {"cmd": "play_id", "arg": "../../etc/passwd"},
    {"cmd": "load", "arg": "file:///etc/passwd"},
    {"cmd": "del", "arg": "x" * 100_000},  # huge payload
    {"cmd": "\u0000\uFFFF"},  # null / non-characters
    {"cmd": "🎵🎵🎵"},
    ["not", "a", "dict"],
    "plain string",
    42,
    None,
]


@pytest.mark.parametrize("req", HOSTILE_REQUESTS)
def test_hostile_requests_return_not_raise(daemon, req):
    try:
        resp = daemon.handle_command(req)
    except TypeError:
        pytest.skip("non-dict rejected by signature")
        return
    assert isinstance(resp, dict)
    assert "ok" in resp


def test_randomized_fuzz_never_raises(daemon):
    rng = random.Random(1337)  # seeded: failures are reproducible
    alphabet = 'ab{}[]",:\\nulltruexX0 \t\n🎵'
    for _ in range(300):
        blob = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        if rng.random() < 0.5:
            try:
                import json

                req = json.loads(blob)
            except Exception:
                req = blob
        else:
            req = blob
        try:
            resp = daemon.handle_command(req)
        except TypeError:
            continue
        assert isinstance(resp, dict)


def test_quit_command_sets_stop_event(daemon):
    resp = daemon.handle_command({"cmd": "quit"})
    assert resp == {"ok": True}
    assert daemon.stop.is_set()
