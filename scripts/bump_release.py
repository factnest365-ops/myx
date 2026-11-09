#!/usr/bin/env python3
"""Synchronize MYX release version and Homebrew formula metadata across all packaging files.

Examples:
  python3 scripts/bump_release.py --verify
  python3 scripts/bump_release.py 0.3.1
  python3 scripts/bump_release.py 0.3.1 --tarball dist/myx-0.3.1.tar.gz
  python3 scripts/bump_release.py 0.3.1 --fetch-sha256 --refresh-resources
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "myx" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
FORMULA = ROOT / "Formula" / "myx.rb"

REPO_SLUG = "factnest365-ops/myx"
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

BUILD_RESOURCES = (
    "flit-core",
    "packaging",
    "pathspec",
    "pluggy",
    "trove-classifiers",
    "hatchling",
)


def read_versions(root: Path = ROOT) -> dict[str, str]:
    init_text = (root / "myx" / "__init__.py").read_text(encoding="utf-8")
    m_init = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)

    pyproj_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m_pyproj = re.search(r'^version\s*=\s*"([^"]+)"', pyproj_text, re.MULTILINE)

    lock_text = (root / "uv.lock").read_text(encoding="utf-8")
    m_lock = re.search(
        r'\[\[package\]\]\s*\nname\s*=\s*"myx(?:-player)?"\s*\nversion\s*=\s*"([^"]+)"',
        lock_text,
    )

    formula_text = (root / "Formula" / "myx.rb").read_text(encoding="utf-8")
    m_formula = re.search(r'/refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz"', formula_text)
    if not m_formula:
        m_formula = re.search(r'/releases/download/v(\d+\.\d+\.\d+)/', formula_text)

    return {
        "myx/__init__.py": m_init.group(1) if m_init else "",
        "pyproject.toml": m_pyproj.group(1) if m_pyproj else "",
        "uv.lock": m_lock.group(1) if m_lock else "",
        "Formula/myx.rb": m_formula.group(1) if m_formula else "",
    }


def verify_packaging(root: Path = ROOT, require_nonzero_formula_sha: bool = False) -> list[str]:
    """Return a list of validation errors (empty list means healthy)."""
    errors: list[str] = []
    versions = read_versions(root)
    for rel, ver in versions.items():
        if not ver or not SEMVER_RE.match(ver):
            errors.append(f"{rel}: missing or invalid semver ({ver!r})")

    unique = {v for v in versions.values() if v}
    if len(unique) > 1:
        errors.append(f"version mismatch across files: {versions}")

    formula_text = (root / "Formula" / "myx.rb").read_text(encoding="utf-8")
    if "include Language::Python::Virtualenv" not in formula_text:
        errors.append("Formula/myx.rb: missing `include Language::Python::Virtualenv`")

    shas = re.findall(r'sha256\s+"([^"]+)"', formula_text)
    if not shas:
        errors.append("Formula/myx.rb: no sha256 entries found")
    for idx, sha in enumerate(shas):
        if not HEX64_RE.match(sha):
            errors.append(f"Formula/myx.rb: invalid sha256 format #{idx + 1}: {sha!r}")
        elif idx > 0 and sha == "0" * 64:
            errors.append(f"Formula/myx.rb: resource #{idx} has all-zero placeholder sha256")
        elif idx == 0 and require_nonzero_formula_sha and sha == "0" * 64:
            errors.append("Formula/myx.rb: top-level release tarball sha256 is still all zeros")

    return errors


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_url_sha256(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "myx-release-bumper/1.0"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def refresh_formula_resources(formula_text: str) -> str:
    updated = formula_text
    for pkg in BUILD_RESOURCES:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{pkg}/json",
            headers={"User-Agent": "myx-release-bumper/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sdists = [u for u in data.get("urls", []) if u.get("packagetype") == "sdist"]
        if not sdists:
            continue
        url = sdists[0]["url"]
        sha = sdists[0]["digests"]["sha256"]
        pattern = re.compile(
            rf'(  resource "{re.escape(pkg)}" do\s*\n\s*url ")[^"]+("\s*\n\s*sha256 ")[^"]+(")',
            re.MULTILINE,
        )
        updated = pattern.sub(rf"\g<1>{url}\g<2>{sha}\g<3>", updated, count=1)
    return updated


def bump_all(
    version: str,
    *,
    root: Path = ROOT,
    sha256: str | None = None,
    refresh_resources: bool = False,
) -> None:
    version = version.lstrip("v").strip()
    if not SEMVER_RE.match(version):
        raise ValueError(f"Expected X.Y.Z semver, got {version!r}")
    if sha256 is not None and not HEX64_RE.match(sha256):
        raise ValueError(f"Expected 64-char lowercase hex sha256, got {sha256!r}")

    # 1. myx/__init__.py
    init_p = root / "myx" / "__init__.py"
    init_txt = init_p.read_text(encoding="utf-8")
    init_txt = re.sub(
        r'^__version__\s*=\s*"[^"]+"',
        f'__version__ = "{version}"',
        init_txt,
        count=1,
        flags=re.MULTILINE,
    )
    init_p.write_text(init_txt, encoding="utf-8")

    # 2. pyproject.toml
    pyproj_p = root / "pyproject.toml"
    pyproj_txt = pyproj_p.read_text(encoding="utf-8")
    pyproj_txt = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        pyproj_txt,
        count=1,
        flags=re.MULTILINE,
    )
    pyproj_p.write_text(pyproj_txt, encoding="utf-8")

    # 3. uv.lock
    lock_p = root / "uv.lock"
    lock_txt = lock_p.read_text(encoding="utf-8")
    lock_txt = re.sub(
        r'(\[\[package\]\]\s*\nname\s*=\s*"myx(?:-player)?"\s*\nversion\s*=\s*)"[^"]+"',
        rf'\g<1>"{version}"',
        lock_txt,
        count=1,
    )
    lock_p.write_text(lock_txt, encoding="utf-8")

    # 4. Formula/myx.rb
    formula_p = root / "Formula" / "myx.rb"
    formula_txt = formula_p.read_text(encoding="utf-8")
    tarball_url = f"https://github.com/{REPO_SLUG}/archive/refs/tags/v{version}.tar.gz"
    formula_txt = re.sub(
        r'^(\s*url\s+)"https://github\.com/[^"]+"',
        rf'\1"{tarball_url}"',
        formula_txt,
        count=1,
        flags=re.MULTILINE,
    )
    if sha256 is not None:
        formula_txt = re.sub(
            r'^(\s*url\s+"[^"]+"\s*\n\s*sha256\s+)"[0-9a-fA-Z_]+"',
            rf'\1"{sha256}"',
            formula_txt,
            count=1,
            flags=re.MULTILINE,
        )
    if refresh_resources:
        formula_txt = refresh_formula_resources(formula_txt)
    formula_p.write_text(formula_txt, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or bump MYX version & Homebrew formula")
    parser.add_argument("version", nargs="?", help="New semantic version (e.g. 0.3.1)")
    parser.add_argument("--verify", action="store_true", help="Verify all version & formula metadata")
    parser.add_argument(
        "--require-release-sha",
        action="store_true",
        help="With --verify, fail if Formula/myx.rb top-level sha256 is still all zeros",
    )
    parser.add_argument("--sha256", help="Explicit 64-hex SHA256 for Formula/myx.rb release archive")
    parser.add_argument("--tarball", type=Path, help="Compute SHA256 from local archive file")
    parser.add_argument(
        "--fetch-sha256",
        action="store_true",
        help="Download GitHub tag tarball for <version> and compute its SHA256",
    )
    parser.add_argument(
        "--refresh-resources",
        action="store_true",
        help="Refresh PyPI sdist URLs and SHA256 digests in Formula/myx.rb",
    )
    args = parser.parse_args(argv)

    if args.verify:
        errs = verify_packaging(require_nonzero_formula_sha=args.require_release_sha)
        if errs:
            for e in errs:
                print(f"ERROR: {e}", file=sys.stderr)
            return 1
        versions = read_versions()
        print(f"OK: packaging metadata synchronized at v{versions['pyproject.toml']}")
        return 0

    if not args.version:
        parser.error("provide <version> or --verify")

    ver = args.version.lstrip("v").strip()
    sha = args.sha256
    if args.tarball:
        sha = sha256_file(args.tarball)
    elif args.fetch_sha256:
        url = f"https://github.com/{REPO_SLUG}/archive/refs/tags/v{ver}.tar.gz"
        sha = fetch_url_sha256(url)

    bump_all(ver, sha256=sha, refresh_resources=args.refresh_resources)
    errs = verify_packaging()
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Updated MYX packaging to v{ver}" + (f" (sha256={sha[:12]}...)" if sha else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
