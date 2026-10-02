"""全局配置。

DATABASE_PATH 与 TODAY 都允许通过环境变量覆盖，
TODAY 覆盖主要用于测试"批次过期"这种依赖当前日期的逻辑。
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "allergen.db"
DATABASE_PATH = Path(os.environ.get("ALLERGEN_DB", DEFAULT_DB))


def today() -> date:
    override = os.environ.get("ALLERGEN_TODAY")
    if override:
        return date.fromisoformat(override)
    return datetime.now().date()
