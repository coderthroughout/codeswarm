"""Enable ``python -m codeswarm``."""
from __future__ import annotations

import sys

from codeswarm.cli import main

if __name__ == "__main__":
    sys.exit(main())
