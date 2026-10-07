import io
import json

from myx import tui
from myx.library import Library


def test_malformed_library_does_not_crash_launch(monkeypatch, tmp_path):
    """A corrupt library file must not stop `myx` from opening For You.

    `load_library` runs unguarded at startup, so anything `from_dict` lets
    through becomes a crash before the first frame is ever drawn. A hand-edited
    or half-written library is a user-reachable input, not a theoretical one.
    """
    bad = tmp_path / "library.json"
    bad.write_text(json.dumps({"version": 1, "recent_queries": 3}))
    monkeypatch.setenv("MYX_HOME", str(tmp_path))

    renderer = tui.Renderer(out=io.StringIO())
    renderer.load_library()
    assert renderer.home.library.is_empty()


def test_library_load_never_raises_on_malformed_input(tmp_path):
    """`load` promises to fall back to an empty library on any problem."""
    bad = tmp_path / "library.json"
    bad.write_text(json.dumps({"version": 1, "recent_queries": 3}))
    assert Library.load(bad).is_empty()
