"""Run the active SpaceTimePy v2 test suite with ``python -m tests``."""

from __future__ import annotations

import unittest
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    suite = unittest.defaultTestLoader.discover(
        str(project_root / "tests"),
        top_level_dir=str(project_root),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
