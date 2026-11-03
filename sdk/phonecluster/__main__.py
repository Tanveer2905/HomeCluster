"""``python -m phonecluster`` -> the CLI (see docs/SPEC.md section 13)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
