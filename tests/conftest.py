# -*- coding: utf-8 -*-
"""让各测试直接 import 平铺模块（proxy 根入 sys.path）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
