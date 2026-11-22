#!/usr/bin/env python3
"""PyInstaller entry script for the desktop app.

Importing ``swararia.desktop.app`` runs its ``-m yt_dlp`` subprocess router at
import time (needed by the frozen executable), then ``main()`` starts the UI.
"""
import sys

from swararia.desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
