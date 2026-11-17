"""Architecture guard: enforce one-way layer dependencies between packages.

    config  <-  core, remote  <-  server  <-  desktop

* ``swararia.core`` must not import ``server``, ``desktop`` or ``remote``.
* ``swararia.remote`` must not import ``server`` or ``desktop``.
* ``swararia.server`` must not import ``desktop``.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "swararia"

FORBIDDEN = {
    "core": {"swararia.server", "swararia.desktop", "swararia.remote"},
    "remote": {"swararia.server", "swararia.desktop"},
    "server": {"swararia.desktop"},
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            # `from swararia import server` style
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _violations(layer: str) -> list[str]:
    bad: list[str] = []
    for path in sorted((PKG / layer).rglob("*.py")):
        for imported in _imports(path):
            for banned in FORBIDDEN[layer]:
                if imported == banned or imported.startswith(banned + "."):
                    bad.append(f"{path.relative_to(PKG.parent)} imports {imported}")
    return bad


def test_core_does_not_depend_on_upper_layers():
    assert _violations("core") == []


def test_remote_does_not_depend_on_server_or_desktop():
    assert _violations("remote") == []


def test_server_does_not_depend_on_desktop():
    assert _violations("server") == []


def test_every_python_file_compiles_and_has_a_package_init():
    for directory in [PKG, *[p for p in PKG.rglob("*") if p.is_dir() and p.name != "__pycache__" and "static" not in p.parts]]:
        if any(directory.glob("*.py")):
            assert (directory / "__init__.py").is_file(), f"missing __init__.py in {directory}"
