"""Enforce AGENTS.md.

The bible is a spec, so it is tested like one. Every structural claim in
AGENTS.md lives in the CONTRACT block; this module parses it and fails when
code and bible disagree. Editing either side alone breaks the build on purpose.

Rules for editing this file:
- Fix the code, or fix the bible. Never widen a check to make it pass.
- `grandfathered_functions` may only shrink. When one fits under the limit,
  delete its entry - a stale entry fails the build too.

No network, no mpv, no daemon. Pure static analysis plus two imports.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIBLE = ROOT / "AGENTS.md"
PKG = ROOT / "myx"

_BLOCK_RE = re.compile(
    r"<!-- BEGIN CONTRACT -->\s*```json\s*(?P<body>.*?)\s*```\s*<!-- END CONTRACT -->",
    re.DOTALL,
)


def contract() -> dict:
    """The CONTRACT block from AGENTS.md, parsed."""
    text = BIBLE.read_text(encoding="utf-8")
    match = _BLOCK_RE.search(text)
    assert match, "AGENTS.md must keep the BEGIN/END CONTRACT markers with a json fence"
    return json.loads(match.group("body"))


def pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def module_files() -> dict[str, Path]:
    """Every importable module in the package, excluding __init__.py."""
    return {p.stem: p for p in sorted(PKG.glob("*.py")) if p.stem != "__init__"}


def imports_of(path: Path, known: set[str]) -> set[str]:
    """Sibling `myx` modules this file imports, including lazy in-function imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                top = node.module.split(".")[0]
                if top in known:
                    found.add(top)
            else:
                for alias in node.names:
                    if alias.name in known:
                        found.add(alias.name)
    return found


def function_lengths(path: Path) -> dict[str, int]:
    """Longest definition per function name in one file, in lines."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lengths: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            size = node.end_lineno - node.lineno + 1
            lengths[node.name] = max(lengths.get(node.name, 0), size)
    return lengths


def test_contract_block_is_complete() -> None:
    spec = contract()
    required = {
        "python",
        "deps",
        "dev_deps",
        "console_script",
        "module_count",
        "mcp_tool_count",
        "max_function_lines",
        "modules",
        "grandfathered_functions",
    }
    assert set(spec) == required, f"CONTRACT keys drifted: {sorted(set(spec) ^ required)}"


def test_declared_modules_match_code() -> None:
    spec = contract()
    declared = set(spec["modules"])
    actual = set(module_files())
    assert declared == actual, f"only in bible: {sorted(declared - actual)}; only in code: {sorted(actual - declared)}"
    assert len(actual) == spec["module_count"], f"bible says {spec['module_count']} modules, code has {len(actual)}"


def test_import_graph_matches_contract() -> None:
    spec = contract()
    modules = module_files()
    known = set(modules)
    actual = {name: sorted(imports_of(path, known)) for name, path in modules.items()}
    assert actual == spec["modules"], (
        "import graph drifted from the bible.\n"
        f"only in bible: { {k: v for k, v in spec['modules'].items() if actual.get(k) != v} }\n"
        f"actual:        { {k: v for k, v in actual.items() if spec['modules'].get(k) != v} }"
    )


def test_no_import_cycles() -> None:
    """The DAG is only a DAG if nothing loops back."""
    graph = contract()["modules"]
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str) -> None:
        assert node not in visiting, f"import cycle through {node}"
        if node in done:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            walk(dep)
        visiting.discard(node)
        done.add(node)

    for module in graph:
        walk(module)


def test_tui_boundary() -> None:
    """tui renders. It does not fetch and does not decide playback."""
    source = (PKG / "tui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"urllib", "http", "requests", "yt_dlp", "httpx"}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            assert top not in forbidden, f"tui.py must never fetch, found import {name}"
    assert "sources" not in imports_of(PKG / "tui.py", set(module_files())), (
        "tui.py must not import sources; fetch goes through the daemon"
    )


def test_mcp_tool_count() -> None:
    from myx.mcp import TOOLS

    spec = contract()
    names = [tool["name"] for tool in TOOLS]
    assert len(names) == len(set(names)), "duplicate MCP tool name"
    assert len(TOOLS) == spec["mcp_tool_count"], (
        f"bible says {spec['mcp_tool_count']} MCP tools, code exposes {len(TOOLS)}"
    )


def test_dependencies_match_contract() -> None:
    spec = contract()
    project = pyproject()["project"]
    assert project["dependencies"] == spec["deps"], "runtime deps drifted from the bible"
    assert project["optional-dependencies"]["dev"] == spec["dev_deps"], "dev deps drifted from the bible"
    assert project["requires-python"] == spec["python"], "python floor drifted from the bible"


def test_console_script_matches_contract() -> None:
    spec = contract()
    scripts = pyproject()["project"]["scripts"]
    assert spec["console_script"] in scripts, f"console script {spec['console_script']} missing from pyproject"
    assert scripts[spec["console_script"]] == "myx.cli:main"


def test_function_size_law() -> None:
    """No new god functions, and the grandfathered ones may only shrink."""
    spec = contract()
    limit = spec["max_function_lines"]
    allowed = spec["grandfathered_functions"]

    offenders: dict[str, int] = {}
    for name, path in module_files().items():
        for func, size in function_lengths(path).items():
            if size > limit:
                offenders[f"{name}.{func}"] = size

    grew = {k: (allowed.get(k), v) for k, v in offenders.items() if allowed.get(k) is None}
    assert not grew, f"new functions over {limit} lines (extract, or add to the bible with a reason): {grew}"

    expanded = {k: (allowed[k], v) for k, v in offenders.items() if v > allowed[k]}
    assert not expanded, f"grandfathered functions grew (bible, actual): {expanded}"

    stale = sorted(set(allowed) - set(offenders))
    assert not stale, f"these now fit under {limit} lines - delete them from the bible and from this list: {stale}"


def test_hygiene() -> None:
    for name, path in module_files().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, f"{name}.py:{node.lineno} bare except"
            if isinstance(node, ast.ImportFrom):
                assert all(alias.name != "*" for alias in node.names), f"{name}.py:{node.lineno} star import"


def test_debt_register_is_honest() -> None:
    """Every grandfathered function must actually exist, or the register lies."""
    modules = module_files()
    names = set()
    for path in modules.values():
        names |= set(function_lengths(path))
    for key in contract()["grandfathered_functions"]:
        module, _, func = key.partition(".")
        assert module in modules, f"debt refers to unknown module {module}"
        assert func in function_lengths(modules[module]), f"debt refers to unknown function {key}"


@pytest.mark.parametrize("marker", ["BEGIN CONTRACT", "END CONTRACT"])
def test_bible_keeps_machine_markers(marker: str) -> None:
    assert marker in BIBLE.read_text(encoding="utf-8")
