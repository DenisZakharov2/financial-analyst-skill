#!/usr/bin/env python3
"""Точка входа скилла: python scripts/analyze.py --data-dir ./data --out ./reports"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finanalyst.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
