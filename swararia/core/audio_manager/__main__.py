"""``python -m swararia.core.audio_manager`` runs the audio library CLI."""
import sys

from swararia.core.audio_manager.cli import main

if __name__ == "__main__":
    sys.exit(main())
