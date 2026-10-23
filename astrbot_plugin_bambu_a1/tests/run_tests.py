"""跑测试的入口，避免依赖 pytest。

用法：
    cd astrbot_plugin_bambu_a1
    python tests/run_tests.py
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for path in (_ROOT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=_HERE, pattern="test_*.py", top_level_dir=_ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
