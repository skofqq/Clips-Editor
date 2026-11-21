"""Entry point: ``python -m app.main [video files...]``."""

from __future__ import annotations

import sys


def main() -> int:
    if "--self-test" in sys.argv:
        from app.self_test import run_self_test

        index = sys.argv.index("--self-test")
        return run_self_test(sys.argv[index + 1] if len(sys.argv) > index + 1 else "")
    from app.bootstrap import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
