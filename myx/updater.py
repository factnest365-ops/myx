"""Self-updater and package-manager detector for MYX (brew, uv, pipx, pip, git)."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

DIST_NAME = "myx-player"
REPO_SLUG = "factnest365-ops/myx"
GIT_INSTALL_URL = f"git+https://github.com/{REPO_SLUG}.git"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
GH_LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
MAX_API_BYTES = 256 * 1024

_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_semver(raw: str | None) -> tuple[int, int, int] | None:
    """Parse a 'vX.Y.Z' or 'X.Y.Z' version string into a comparable 3-tuple."""
    if not raw:
        return None
    m = _SEMVER_RE.search(str(raw).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _read_direct_url_metadata() -> dict[str, Any]:
    """Read PEP 610 direct_url.json for 'myx-player' (or legacy 'myx') if installed via editable or VCS URL."""
    for pkg_name in (DIST_NAME, "myx"):
        try:
            dist = importlib.metadata.distribution(pkg_name)
            raw = dist.read_text("direct_url.json")
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _is_safe_git_url(url: str) -> bool:
    return url.startswith("https://github.com/") and all(
        ch not in url for ch in (" ", "\t", "\n", "\r", ";", "|", "&", "`", "$")
    )


def _is_brew_managed_path(path_str: str) -> bool:
    p = path_str.replace("\\", "/")
    return any(
        marker in p
        for marker in (
            "/Cellar/",
            "/opt/homebrew/",
            "/usr/local/Cellar/",
            "/linuxbrew/.linuxbrew/",
        )
    )


def detect_install_method(
    executable: str | None = None,
    module_file: str | None = None,
    direct_url: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect how MYX was installed and build the safe argv command list to upgrade it.

    Returns a dict with:
      - method: 'brew' | 'uv' | 'pipx' | 'git-editable' | 'pip-vcs' | 'pip'
      - executable: resolved Python executable
      - root: repo or environment path
      - commands: list[list[str]] to upgrade MYX
    """
    exe_raw = executable or sys.executable
    try:
        exe_resolved = str(Path(exe_raw).resolve())
    except OSError:
        exe_resolved = exe_raw
    # Keep both raw symlink path and resolved target because venvs symlink python -> Homebrew python
    exe_norm = exe_raw.replace("\\", "/")
    exe_res_norm = exe_resolved.replace("\\", "/")

    mod_raw = module_file or __file__
    try:
        pkg_root = Path(mod_raw).resolve().parent.parent
    except OSError:
        pkg_root = Path(mod_raw).parent.parent

    durl = direct_url if direct_url is not None else _read_direct_url_metadata()

    # 1. Homebrew formula install (/Cellar/myx/... or /opt/homebrew/Cellar/myx/...)
    pkg_norm = str(pkg_root).replace("\\", "/")
    if "/Cellar/myx/" in exe_norm or "/Cellar/myx/" in exe_res_norm or "/Cellar/myx/" in pkg_norm:
        brew = shutil.which("brew") or "brew"
        return {
            "method": "brew",
            "executable": exe_raw,
            "root": str(pkg_root),
            "commands": [
                [brew, "update"],
                [brew, "upgrade", "myx"],
            ],
        }

    # 2. uv tool install (~/.local/share/uv/tools/myx-player/... or legacy myx)
    uv_tool_dir = os.environ.get("UV_TOOL_DIR", "").replace("\\", "/")
    for tool_pkg in (DIST_NAME, "myx"):
        if (
            f"/uv/tools/{tool_pkg}/" in exe_norm
            or f"/uv/tools/{tool_pkg}/" in pkg_norm
            or (uv_tool_dir and exe_norm.startswith(uv_tool_dir.rstrip("/") + f"/{tool_pkg}/"))
        ):
            uv = shutil.which("uv") or "uv"
            return {
                "method": "uv",
                "executable": exe_raw,
                "root": str(pkg_root),
                "commands": [[uv, "tool", "upgrade", tool_pkg]],
            }

    # 3. pipx install (~/.local/pipx/venvs/myx-player/... or legacy myx)
    pipx_home = os.environ.get("PIPX_HOME", "").replace("\\", "/")
    for tool_pkg in (DIST_NAME, "myx"):
        if (
            f"/pipx/venvs/{tool_pkg}/" in exe_norm
            or f"/pipx/venvs/{tool_pkg}/" in pkg_norm
            or (pipx_home and exe_norm.startswith(pipx_home.rstrip("/") + f"/venvs/{tool_pkg}/"))
        ):
            pipx = shutil.which("pipx") or "pipx"
            return {
                "method": "pipx",
                "executable": exe_raw,
                "root": str(pkg_root),
                "commands": [[pipx, "upgrade", tool_pkg]],
            }

    # 4. Editable / local git checkout
    is_editable = bool(durl.get("dir_info", {}).get("editable"))
    has_git_dir = (pkg_root / ".git").exists()
    if is_editable or has_git_dir:
        cmds: list[list[str]] = []
        git = shutil.which("git")
        if has_git_dir and git:
            cmds.append([git, "-C", str(pkg_root), "pull", "--ff-only"])
        uv = shutil.which("uv")
        if uv and (pkg_root / "uv.lock").exists() and "/.venv/" in exe_norm:
            cmds.append([uv, "sync", "--directory", str(pkg_root)])
        else:
            cmds.append([exe_raw, "-m", "pip", "install", "--upgrade", "-e", str(pkg_root)])
        return {
            "method": "git-editable",
            "executable": exe_raw,
            "root": str(pkg_root),
            "commands": cmds,
        }

    # 5. Direct VCS pip install (e.g. pip install git+https://github.com/factnest365-ops/myx.git)
    vcs_info = durl.get("vcs_info")
    url_str = str(durl.get("url") or "")
    if isinstance(vcs_info, dict) and _is_safe_git_url(url_str):
        vcs = str(vcs_info.get("vcs") or "git")
        if vcs == "git":
            return {
                "method": "pip-vcs",
                "executable": exe_raw,
                "root": str(pkg_root),
                "commands": [[exe_raw, "-m", "pip", "install", "--upgrade", f"git+{url_str}"]],
            }

    # 6. Standard pip / venv install (from PyPI `myx-player`, never colliding with unrelated `myx`)
    return {
        "method": "pip",
        "executable": exe_raw,
        "root": str(pkg_root),
        "commands": [[exe_raw, "-m", "pip", "install", "--upgrade", DIST_NAME]],
    }


def build_dep_update_commands() -> list[dict[str, Any]]:
    """Detect how system media tools (yt-dlp, mpv, ffmpeg) are installed and return safe upgrade steps."""
    steps: list[dict[str, Any]] = []
    brew = shutil.which("brew")
    brew_pkgs: list[str] = []

    for tool in ("yt-dlp", "mpv", "ffmpeg"):
        bin_path = shutil.which(tool)
        if not bin_path:
            continue
        try:
            resolved = str(Path(bin_path).resolve())
        except OSError:
            resolved = bin_path

        if brew and (_is_brew_managed_path(bin_path) or _is_brew_managed_path(resolved)):
            brew_pkgs.append(tool)
            continue

        if tool == "yt-dlp":
            norm_res = resolved.replace("\\", "/")
            uv = shutil.which("uv")
            pipx = shutil.which("pipx")
            if uv and "/uv/tools/yt-dlp/" in norm_res:
                steps.append({"target": "yt-dlp", "manager": "uv", "command": [uv, "tool", "upgrade", "yt-dlp"]})
            elif pipx and "/pipx/venvs/yt-dlp/" in norm_res:
                steps.append({"target": "yt-dlp", "manager": "pipx", "command": [pipx, "upgrade", "yt-dlp"]})
            elif Path(bin_path).parent == Path(sys.executable).parent:
                steps.append(
                    {
                        "target": "yt-dlp",
                        "manager": "pip",
                        "command": [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                    }
                )
            elif os.access(resolved, os.W_OK):
                steps.append({"target": "yt-dlp", "manager": "self", "command": [bin_path, "-U"]})

    if brew and brew_pkgs:
        steps.insert(
            0,
            {
                "target": ", ".join(brew_pkgs),
                "manager": "brew",
                "command": [brew, "upgrade", *brew_pkgs],
            },
        )
    return steps


def check_latest_version(timeout: float = 4.0) -> dict[str, Any]:
    """Query PyPI (and fall back to GitHub Releases) for the newest published MYX version."""
    current = __version__
    cur_tuple = parse_semver(current)
    headers = {"User-Agent": f"myx/{current}", "Accept": "application/json"}

    # Try PyPI first
    for source_name, url in (("pypi", PYPI_JSON_URL), ("github", GH_LATEST_RELEASE_URL)):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                raw = resp.read(MAX_API_BYTES + 1)
                if len(raw) > MAX_API_BYTES:
                    continue
                data = json.loads(raw.decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue

        latest_raw: str | None = None
        if source_name == "pypi" and isinstance(data, dict):
            info = data.get("info")
            if isinstance(info, dict):
                # Verify PyPI metadata points to our GitHub repo (defense-in-depth against name squatting)
                urls_blob = json.dumps(info.get("project_urls") or {}) + " " + str(info.get("home_page") or "")
                if info.get("project_urls") or info.get("home_page"):
                    if REPO_SLUG.lower() not in urls_blob.lower():
                        continue
                v = info.get("version")
                if isinstance(v, str) and v.strip():
                    latest_raw = v.strip()
        elif source_name == "github" and isinstance(data, dict):
            tag = data.get("tag_name")
            if isinstance(tag, str) and tag.strip():
                latest_raw = tag.strip().lstrip("v")

        if latest_raw:
            lat_tuple = parse_semver(latest_raw)
            update_available = bool(cur_tuple and lat_tuple and lat_tuple > cur_tuple)
            return {
                "current": current,
                "latest": latest_raw,
                "source": source_name,
                "update_available": update_available,
            }

    return {
        "current": current,
        "latest": None,
        "source": None,
        "update_available": None,
    }


def _format_cmd(cmd: list[str]) -> str:
    return " ".join(cmd)


def build_update_plan(
    *,
    only_myx: bool = False,
    only_deps: bool = False,
    from_git: bool = False,
) -> dict[str, Any]:
    """Assemble the complete, ordered upgrade plan for MYX and media dependencies."""
    install_info = detect_install_method()
    method = install_info["method"]
    steps: list[dict[str, Any]] = []

    if not only_deps:
        if from_git:
            if method == "uv":
                uv = shutil.which("uv") or "uv"
                cmds = [[uv, "tool", "install", "--force", GIT_INSTALL_URL]]
            elif method == "pipx":
                pipx = shutil.which("pipx") or "pipx"
                cmds = [[pipx, "install", "--force", GIT_INSTALL_URL]]
            elif method == "brew":
                brew = shutil.which("brew") or "brew"
                cmds = [[brew, "upgrade", "--fetch-HEAD", "myx"]]
            else:
                cmds = [[install_info["executable"], "-m", "pip", "install", "--upgrade", GIT_INSTALL_URL]]
        else:
            cmds = install_info["commands"]

        for cmd in cmds:
            steps.append({"target": "myx", "manager": method, "command": cmd})

    if not only_myx:
        # Avoid duplicate `brew upgrade` if `brew upgrade myx` already updates formula deps,
        # though explicitly upgrading yt-dlp/mpv/ffmpeg ensures formula dependents also move forward.
        steps.extend(build_dep_update_commands())

    return {
        "current_version": __version__,
        "install": install_info,
        "steps": steps,
    }


def run_update(
    *,
    check_only: bool = False,
    only_myx: bool = False,
    only_deps: bool = False,
    from_git: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    """Check or execute updates for MYX (brew/uv/pipx/pip/git) and media tools (yt-dlp/mpv/ffmpeg)."""
    plan = build_update_plan(only_myx=only_myx, only_deps=only_deps, from_git=from_git)
    version_check = check_latest_version()
    method = plan["install"]["method"]

    if not quiet:
        print(f"myx update — current v{__version__} (installed via {method})\n")
        if version_check["latest"]:
            state = (
                f"update available -> v{version_check['latest']} ({version_check['source']})"
                if version_check["update_available"]
                else f"up to date (latest v{version_check['latest']} on {version_check['source']})"
            )
            print(f"  release status : {state}")
        else:
            print("  release status : remote version unavailable (offline or unreleased on PyPI)")
        print(f"  install root   : {plan['install']['root']}\n")

    if check_only:
        if not quiet:
            if not plan["steps"]:
                print("  No update commands detected.")
            else:
                print("  Planned commands (--check):")
                for step in plan["steps"]:
                    print(f"    [{step['manager']}] {_format_cmd(step['command'])}")
        return {
            "ok": True,
            "mode": "check",
            "version": version_check,
            "method": method,
            "commands": [_format_cmd(s["command"]) for s in plan["steps"]],
            "results": [],
        }

    results: list[dict[str, Any]] = []
    all_ok = True
    for step in plan["steps"]:
        cmd = step["command"]
        label = f"[{step['manager']}] {_format_cmd(cmd)}"
        if not quiet:
            print(f"  -> {label}")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=quiet,
                text=True,
                timeout=300,
                check=False,
            )
            rc = proc.returncode
            err_tail = (proc.stderr or "").strip()[-400:] if quiet else ""
        except FileNotFoundError as exc:
            rc = 127
            err_tail = str(exc)
            if not quiet:
                print(f"     ! command not found: {cmd[0]}")
        except subprocess.TimeoutExpired:
            rc = 124
            err_tail = "command timed out after 300s"
            if not quiet:
                print("     ! command timed out after 300s")

        # Helpful hint if PyPI doesn't have `myx-player` yet during a standard pip/uv/pipx upgrade
        if rc != 0 and step["target"] == "myx" and not from_git and method in ("pip", "uv", "pipx"):
            if not quiet:
                print(
                    f"     hint: if '{DIST_NAME}' is not yet published on PyPI, run `myx update --git` "
                    f"to upgrade directly from {GIT_INSTALL_URL}"
                )

        ok = rc == 0
        if not ok:
            all_ok = False
        results.append(
            {
                "target": step["target"],
                "manager": step["manager"],
                "command": _format_cmd(cmd),
                "returncode": rc,
                "ok": ok,
                **({"error": err_tail} if err_tail else {}),
            }
        )

    if not quiet:
        print(f"\n{'Update complete.' if all_ok else 'Update finished with warnings/errors.'}")

    return {
        "ok": all_ok,
        "mode": "apply",
        "version": version_check,
        "method": method,
        "commands": [_format_cmd(s["command"]) for s in plan["steps"]],
        "results": results,
    }
