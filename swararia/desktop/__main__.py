"""``python -m swararia.desktop`` launches the desktop application."""
import sys

from swararia.desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
