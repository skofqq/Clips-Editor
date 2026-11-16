"""Entry point: ``python -m app.main [video files...]``."""

from __future__ import annotations

import sys


def main() -> int:
    from app.bootstrap import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
