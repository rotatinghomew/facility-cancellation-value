"""命令行入口：在隔离模式（python3 -I）下也能定位到 overbooking 包。

用法：
    python3 -I run_report.py --samples samples
    python3 -I run_report.py --samples samples --day 2026-09-15
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overbooking.report import main  # noqa: E402

if __name__ == "__main__":
    main()
