import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
    DATABASE = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "crawl_lab.db"),
    )
    # 演示账号：admin 建档，alice / bob 可当接班人。
    USERS = {
        "admin": "123456",
        "alice": "123456",
        "bob": "123456",
    }
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    # 测试可钉死后台时刻，如 "2026-11-13 09:30:00"。
    FIXED_NOW = os.environ.get("FIXED_NOW")
