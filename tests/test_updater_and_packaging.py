from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import myx
import myx.cli as cli
import myx.mcp as mcp
import myx.updater as updater

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_bump_script():
    spec = importlib.util.spec_from_file_location(
        "bump_release", REPO_ROOT / "scripts" / "bump_release.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_synchronized_across_all_packaging_files():
    bump = _load_bump_script()
    errors = bump.verify_packaging(REPO_ROOT)
    assert errors == [], f"Packaging drift detected: {errors}"
    versions = bump.read_versions(REPO_ROOT)
    assert versions["myx/__init__.py"] == myx.__version__
    assert versions["pyproject.toml"] == myx.__version__
    assert versions["uv.lock"] == myx.__version__
    assert versions["Formula/myx.rb"] == myx.__version__
    assert mcp.SERVER_VERSION == myx.__version__


def test_bump_release_roundtrip_in_tmp(tmp_path):
    bump = _load_bump_script()
    (tmp_path / "myx").mkdir()
    (tmp_path / "Formula").mkdir()
    for rel in ("myx/__init__.py", "pyproject.toml", "uv.lock", "Formula/myx.rb"):
        (tmp_path / rel).write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")

    fake_sha = "a" * 64
    bump.bump_all("1.4.2", root=tmp_path, sha256=fake_sha)
    errs = bump.verify_packaging(tmp_path, require_nonzero_formula_sha=True)
    assert errs == []
    versions = bump.read_versions(tmp_path)
    assert all(v == "1.4.2" for v in versions.values())
    assert fake_sha in (tmp_path / "Formula" / "myx.rb").read_text(encoding="utf-8")


def test_detect_install_method_brew(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    pkg_mod = tmp_path / "site" / "myx" / "updater.py"
    pkg_mod.parent.mkdir(parents=True)
    pkg_mod.write_text("", encoding="utf-8")

    info = updater.detect_install_method(
        executable="/opt/homebrew/Cellar/myx/0.3.0/libexec/bin/python",
        module_file=str(pkg_mod),
        direct_url={},
    )
    assert info["method"] == "brew"
    assert ["/opt/homebrew/bin/brew", "upgrade", "myx"] in info["commands"]


def test_detect_install_method_uv_and_pipx(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    pkg_mod = tmp_path / "site" / "myx" / "updater.py"
    pkg_mod.parent.mkdir(parents=True)
    pkg_mod.write_text("", encoding="utf-8")

    uv_info = updater.detect_install_method(
        executable="/Users/alice/.local/share/uv/tools/myx/bin/python",
        module_file=str(pkg_mod),
        direct_url={},
    )
    assert uv_info["method"] == "uv"
    assert uv_info["commands"] == [["/usr/local/bin/uv", "tool", "upgrade", "myx"]]

    pipx_info = updater.detect_install_method(
        executable="/Users/alice/.local/pipx/venvs/myx/bin/python",
        module_file=str(pkg_mod),
        direct_url={},
    )
    assert pipx_info["method"] == "pipx"
    assert pipx_info["commands"] == [["/usr/local/bin/pipx", "upgrade", "myx"]]


def test_detect_install_method_git_editable_and_pip_vcs_safety(monkeypatch, tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    (repo / "uv.lock").write_text("", encoding="utf-8")
    mod = repo / "myx" / "updater.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("", encoding="utf-8")

    monkeypatch.setattr(updater.shutil, "which", lambda name: f"/usr/bin/{name}")
    editable = updater.detect_install_method(
        executable=str(repo / ".venv" / "bin" / "python"),
        module_file=str(mod),
        direct_url={"dir_info": {"editable": True}},
    )
    assert editable["method"] == "git-editable"
    assert editable["commands"][0][:3] == ["/usr/bin/git", "-C", str(repo)]
    assert editable["commands"][1] == ["/usr/bin/uv", "sync", "--directory", str(repo)]

    # Non-git site-packages with safe GitHub VCS direct_url
    site_mod = tmp_path / "venv" / "lib" / "site-packages" / "myx" / "updater.py"
    site_mod.parent.mkdir(parents=True)
    site_mod.write_text("", encoding="utf-8")
    vcs = updater.detect_install_method(
        executable="/tmp/venv/bin/python",
        module_file=str(site_mod),
        direct_url={"url": "https://github.com/factnest365-ops/myx.git", "vcs_info": {"vcs": "git"}},
    )
    assert vcs["method"] == "pip-vcs"
    assert vcs["commands"] == [["/tmp/venv/bin/python", "-m", "pip", "install", "--upgrade", "git+https://github.com/factnest365-ops/myx.git"]]

    # Untrusted / non-GitHub VCS URL falls back safely to standard PyPI pip upgrade (`myx-player`)
    unsafe = updater.detect_install_method(
        executable="/tmp/venv/bin/python",
        module_file=str(site_mod),
        direct_url={"url": "https://evil.example/repo.git;rm -rf /", "vcs_info": {"vcs": "git"}},
    )
    assert unsafe["method"] == "pip"
    assert unsafe["commands"] == [["/tmp/venv/bin/python", "-m", "pip", "install", "--upgrade", "myx-player"]]


def test_build_dep_update_commands_brew_and_uv(monkeypatch):
    def fake_which(name):
        mapping = {
            "brew": "/opt/homebrew/bin/brew",
            "mpv": "/opt/homebrew/bin/mpv",
            "ffmpeg": "/opt/homebrew/bin/ffmpeg",
            "yt-dlp": "/Users/alice/.local/share/uv/tools/yt-dlp/bin/yt-dlp",
            "uv": "/usr/local/bin/uv",
        }
        return mapping.get(name)

    monkeypatch.setattr(updater.shutil, "which", fake_which)
    steps = updater.build_dep_update_commands()
    assert steps[0] == {
        "target": "mpv, ffmpeg",
        "manager": "brew",
        "command": ["/opt/homebrew/bin/brew", "upgrade", "mpv", "ffmpeg"],
    }
    assert steps[1] == {
        "target": "yt-dlp",
        "manager": "uv",
        "command": ["/usr/local/bin/uv", "tool", "upgrade", "yt-dlp"],
    }


def test_check_latest_version_parses_pypi(monkeypatch):
    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps(
        {
            "info": {
                "version": "9.9.9",
                "project_urls": {"Repository": "https://github.com/factnest365-ops/myx"},
            }
        }
    ).encode("utf-8")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=4.0: FakeResp(payload))
    res = updater.check_latest_version()
    assert res["latest"] == "9.9.9"
    assert res["source"] == "pypi"
    assert res["update_available"] is True

    # Spoofed/unrelated PyPI package pointing to another repo must be rejected
    spoofed = json.dumps(
        {
            "info": {
                "version": "99.0.0",
                "home_page": "https://gitlab.myxrobotics.com/api",
            }
        }
    ).encode("utf-8")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=4.0: FakeResp(spoofed))
    res_spoof = updater.check_latest_version()
    assert res_spoof["latest"] is None


def test_cli_version_and_update_commands(monkeypatch, capsys):
    monkeypatch.setattr(
        updater,
        "detect_install_method",
        lambda *a, **kw: {
            "method": "uv",
            "executable": "/usr/bin/python3",
            "root": "/tmp/myx",
            "commands": [["uv", "tool", "upgrade", "myx"]],
        },
    )
    monkeypatch.setattr(
        updater,
        "build_dep_update_commands",
        lambda: [{"target": "yt-dlp", "manager": "brew", "command": ["brew", "upgrade", "yt-dlp"]}],
    )
    monkeypatch.setattr(
        updater,
        "check_latest_version",
        lambda timeout=4.0: {
            "current": myx.__version__,
            "latest": myx.__version__,
            "source": "pypi",
            "update_available": False,
        },
    )

    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert myx.__version__ in out and "(uv)" in out

    # Check mode should NOT execute subprocess.run
    executed = []
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda cmd, **kw: executed.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    rc_check = cli.main(["update", "--check", "--json"])
    assert rc_check == 0
    assert executed == []
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "check"
    assert "uv tool upgrade myx" in data["commands"]
    assert "brew upgrade yt-dlp" in data["commands"]

    # Apply mode executes both commands
    rc_apply = cli.main(["update", "--json"])
    assert rc_apply == 0
    assert executed == [["uv", "tool", "upgrade", "myx"], ["brew", "upgrade", "yt-dlp"]]
    apply_data = json.loads(capsys.readouterr().out)
    assert apply_data["ok"] is True

    # MCP myx_update tool defaults to check_only=True
    executed.clear()
    mcp_resp = mcp.handle_tool("myx_update", {})
    assert mcp_resp["isError"] is False
    assert executed == []
    mcp_payload = json.loads(mcp_resp["content"][0]["text"])
    assert mcp_payload["mode"] == "check"
