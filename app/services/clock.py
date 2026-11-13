"""后台时刻。测试用 app.config['FIXED_NOW'] 钉死；窗口判断一律走这里。"""

from datetime import datetime

from flask import current_app


def now() -> datetime:
    fixed = current_app.config.get("FIXED_NOW")
    if fixed:
        if isinstance(fixed, datetime):
            return fixed.replace(microsecond=0)
        return datetime.fromisoformat(str(fixed)).replace(microsecond=0)
    return datetime.now().replace(microsecond=0)


def stamp(moment: datetime | None = None) -> str:
    return (moment or now()).strftime("%Y-%m-%d %H:%M:%S")
