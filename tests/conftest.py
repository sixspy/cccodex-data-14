import pytest

from app import create_app
from app.db import close_db, init_db


def make_app(path, now="2026-11-13 09:35:00"):
    class TestConfig:
        SECRET_KEY = "test"
        DATABASE = path
        USERS = {"admin": "123456", "alice": "123456", "bob": "123456"}
        ADMIN_USER = "admin"
        ADMIN_PASSWORD = "123456"
        TESTING = True
        FIXED_NOW = now

    return create_app(TestConfig)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def app(db_path):
    application = make_app(db_path)
    with application.app_context():
        init_db(application)
    yield application
    with application.app_context():
        close_db()

@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def now_holder():
    """测试内可改钉死时刻：配合 app.config['FIXED_NOW'] 使用。"""
    return {"now": "2026-11-13 09:35:00"}


@pytest.fixture()
def app_factory(db_path, now_holder):
    """同一数据库、可换时刻的工厂。"""
    def build(now=None):
        if now:
            now_holder["now"] = now
        return make_app(db_path, now or now_holder["now"])
    return build
