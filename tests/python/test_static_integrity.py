"""Static asset integrity: every reference in HTML/JS must resolve to a real file."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "swararia" / "web" / "static"
PAGES = ["ingest", "qc", "converter", "organizer", "multitrack", "video-split", "player"]


def _exists_url(url: str) -> bool:
    assert url.startswith("/static/"), url
    return (STATIC / url[len("/static/"):]).is_file()


def test_every_page_has_html_and_entry_script():
    for page in PAGES:
        assert (STATIC / "pages" / page / "index.html").is_file(), page
        assert (STATIC / "pages" / page / "index.js").is_file(), page


def test_html_static_references_resolve():
    missing = []
    for html in sorted(STATIC.rglob("*.html")):
        for url in re.findall(r"""(?:href|src)=["'](/static/[^"'#?]+)""", html.read_text(encoding="utf-8")):
            if not _exists_url(url):
                missing.append(f"{html.relative_to(STATIC)} -> {url}")
    assert not missing, missing


def test_js_relative_imports_resolve():
    missing = []
    pattern = re.compile(r"""(?:from\s+|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
    for js in sorted(STATIC.rglob("*.js")):
        for rel in pattern.findall(js.read_text(encoding="utf-8")):
            if not (js.parent / rel).resolve().is_file():
                missing.append(f"{js.relative_to(STATIC)} -> {rel}")
    assert not missing, missing


def test_js_absolute_static_strings_resolve():
    missing = []
    for js in sorted(STATIC.rglob("*.js")):
        for url in re.findall(r"""['"`](/static/[A-Za-z0-9_\-./]+\.[a-z0-9]+)['"`]""", js.read_text(encoding="utf-8")):
            if not _exists_url(url):
                missing.append(f"{js.relative_to(STATIC)} -> {url}")
    assert not missing, missing


def test_spa_router_config_points_at_real_files():
    router = (STATIC / "modules" / "spa-router.js").read_text(encoding="utf-8")
    config = router[router.index("const ROUTE_CONFIG = {"):]
    config = config[: config.index("\n};")]
    scripts = set(re.findall(r"script:\s*'([^']+)'", config))
    css = set(re.findall(r"'(css/[^']+\.css)'", router))
    assert scripts and css
    missing = [f for f in scripts | css if not (STATIC / f).is_file()]
    assert not missing, missing
    # every route in the router has a matching served page
    assert {Path(s).parent.name for s in scripts} <= set(PAGES)
