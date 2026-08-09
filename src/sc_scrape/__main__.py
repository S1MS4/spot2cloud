"""``python -m sc_scrape`` -- the no-install entry point."""

import sys

from sc_scrape.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
