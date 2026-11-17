"""Guard: every HTTP route literal known before the refactor must still exist.

Scans the server sources (flat ``web_ui.py`` or ``swararia/server/**``) for
string constants that look like routes and compares them to a golden list.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "route_table.txt"
ROUTE_RE = re.compile(r"^/(api/[A-Za-z0-9_\-/]+|static/|qc|converter|organizer|multitrack|splitter|video-split|videosplit|player)(\.html)?$|^/$|^/index\.html$")


def _server_sources() -> list[Path]:
    pkg = ROOT / "swararia" / "server"
    if pkg.is_dir():
        return sorted(pkg.rglob("*.py"))
    return [ROOT / "web_ui.py"]


def _collect_routes() -> set[str]:
    found: set[str] = set()
    for src in _server_sources():
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ROUTE_RE.match(node.value):
                    found.add(node.value)
    return found


def test_route_table_unchanged():
    expected = {line.strip() for line in GOLDEN.read_text().splitlines() if line.strip()}
    missing = expected - _collect_routes()
    assert not missing, f"routes lost during refactor: {sorted(missing)}"
