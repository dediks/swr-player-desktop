"""Central filesystem locations shared across the application.

Kept free of any ``swararia`` imports so every layer can depend on it.

* ``REPO_ROOT`` - the source checkout root (``.vendor/``, ``library/``).  In a
  PyInstaller bundle ``__file__`` resolves inside ``sys._MEIPASS``, so the same
  expression yields the bundle root.
* ``PACKAGE_DIR`` - the ``swararia`` package directory.
* ``STATIC_DIR`` - the web UI assets served under ``/static/``.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = PACKAGE_DIR.parent
STATIC_DIR: Path = PACKAGE_DIR / "web" / "static"
