"""隔离模式（python3 -I）下的测试入口。

用法：python3 -I run_tests.py
等价于 python3 -m unittest discover -s tests -v，但显式补齐仓库根与 tests
目录，使 -I 模式下也能导入 overbooking 包、inspect_samples 与测试模块。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
