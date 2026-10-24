"""Stdlib-only process gate: the parent assigns its kill-on-close job before 'go'."""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    if sys.stdin.buffer.readline() != b"go\n":
        return 1
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())