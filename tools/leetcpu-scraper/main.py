#!/usr/bin/env python3
"""入口脚本：python main.py [--mode full] [--outdir ./output]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leetcpu_scraper.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
