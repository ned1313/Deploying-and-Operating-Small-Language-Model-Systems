"""Container healthcheck: exit 0 when the URL answers 200. Usage: python -m taco_shared.healthcheck URL"""

from __future__ import annotations

import sys
import urllib.request


def main() -> int:
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
