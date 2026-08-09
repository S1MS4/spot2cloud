"""``python -m spot2cloud`` -- the no-install entry point."""

import sys

from spot2cloud.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
