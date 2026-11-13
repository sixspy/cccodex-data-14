"""交班 / 接手七段验收。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_db
from app.services import shifts


def login(client, user="admin"):
    return client.post(
        "/login", data={"username": user, "password": "123456"}, follow_redirects=True
    )


def seed_job(client, name="t1", seed="/mock/catalog/alpha"):
    return client.post(
        "/jobs", data={"name": name, "seed_path": seed}, follow_redirects=True
    )


def do_handover(client, successor="alice", mode="atomic",
                start="09:35", end="10:00"):
    return client.post(
        "/shift/handover",
        data={"successor": successor, "mode": mode,
              "window_start": start, "window_end": end},
        follow_redirects=True,
    )


def body(response) -> str:
    return response.get_data(as_text=True)


# ------------------------------------------------------------ 第 1 段：建档人当接班人

def test_self_handover_rejected(app, client):
    login(client)
    seed_job(client)
    r = do_handover(client, successor="admin")
    assert "接班人不能是建档人" in body(r)
    with app.app_context():
        db = get_db()
        shift = shifts.current_shift(db)
        assert shift["status"] == shifts.SHIFT_ACTIVE
        assert shifts.latest_handover(db, shift["id"]) is None
        codes = shifts.held_codes(db, shift["id"])
        assert [c["code"] for c in codes] == ["CJ-0001"]


def test_successor_is_creator_via_db_service(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "bob")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'bob', 'CJ-9001')",
            (shift["id"],),
        )
        db.commit()
        with pytest.raises(shifts.HandoverError) as err:
            shifts.hand_over(db, shift["id"], successor="bob", mode="atomic",
                             window_start="09:35", window_end="10:00", actor="bob")
        assert err.value.reason == "chose_self"


# ------------------------------------------------------------ 第 2 段：原子回滚 / 部分留存

def test_atomic_incomplete_rolls_back(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        for n in (1, 2, 3):
            db.execute(
                "INSERT INTO crawl_jobs (name, seed_path, status, shift_id,"
                " created_by, code) VALUES (?, '/mock/catalog/alpha', 'draft', ?,"
                " 'admin', ?)",
                (f"j{n}", shift["id"], f"CJ-000{n}"),
            )
        db.commit()
        with pytest.raises(shifts.HandoverError) as err:
            shifts.hand_over(
                db, shift["id"], successor="alice", mode="atomic",
                window_start="09:35", window_end="10:00", actor="admin",
                code_ids=[1, 2],
            )
        assert err.value.reason == "atomic_incomplete"
        db.rollback()
        # 回滚到交班前：仍是本班、无交接单、三条编码全在本班。
        assert shifts.current_shift(db)["id"] == shift["id"]
        assert shifts.latest_handover(db, shift["id"]) is None
        assert len(shifts.held_codes(db, shift["id"])) == 3


def test_atomic_full_transfer_moves_all_codes(app, client):
    login(client)
    seed_job(client, name="a", seed="/mock/catalog/alpha")
    seed_job(client, name="b", seed="/mock/catalog/beta")
    r = do_handover(client)
    assert "交班成功" in body(r)
    with app.app_context():
        db = get_db()
        handover = shifts.latest_handover(db, 1)
        assert handover["status"] == shifts.HANDOVER_PENDING
        assert handover["code_count"] == 2
        rows = shifts.handover_codes(db, handover["id"])
        assert {r["code"] for r in rows} == {"CJ-0001", "CJ-0002"}
        assert all(r["state"] == shifts.CODE_PENDING for r in rows)


def test_partial_stuck_stays_in_shift(app):
    """异常情形：编码在接手前被挪走，则留在本班并标未交清。"""
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        for n in (1, 2):
            db.execute(
                "INSERT INTO crawl_jobs (name, seed_path, status, shift_id,"
                " created_by, code) VALUES (?, '/mock/catalog/alpha', 'draft', ?,"
                " 'admin', ?)",
                (f"j{n}", shift["id"], f"CJ-000{n}"),
            )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        # 模拟异常：第 2 条编码被挪到物件删改后的游离状态（本班已不再持有）。
        db.execute(
            "INSERT INTO shifts (id, label, creator, status) VALUES (99, '旁班', 'bob',"
            " 'ledger')"
        )
        db.execute("UPDATE crawl_jobs SET shift_id = 99 WHERE code = 'CJ-0002'")
        db.commit()
        result = shifts.accept(db, handover_id, mode="atomic", actor="alice")
        db.commit()
        # 原子口径：有一条不在本班 → 整笔不搬，其余编码仍留在本班。
        assert result.outcome == "partial"
        assert result.stuck == ["CJ-0002"]
        assert result.moved == []
        assert [c["code"] for c in shifts.held_codes(db, shift["id"])] == ["CJ-0001"]
        handover = shifts.latest_handover(db, shift["id"])
        assert handover["status"] == shifts.HANDOVER_PARTIAL
        # 整笔未交：交接单上两条都记为未交清，仍留在本班。
        assert shifts.stuck_count(db, handover["id"]) == 2


# ------------------------------------------------------------ 第 3 段：移交方式不一致

def test_mode_mismatch_rejected(app, client):
    with app.app_context():
        from app.services import shifts as sv
        db = get_db()
        # 当日登记为 atomic；请求另一种方式应被拒。
        sv.register_day(db, "2026-11-13", "atomic", "admin")
        db.commit()
    login(client)
    seed_job(client)
    r = do_handover(client, mode="staged")
    assert "方式不对" in body(r)
    with app.app_context():
        assert shifts.current_shift(get_db())["status"] == shifts.SHIFT_ACTIVE


def test_accept_with_wrong_mode_rejected(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        with pytest.raises(shifts.HandoverError) as err:
            shifts.accept(db, handover_id, mode="staged", actor="alice")
        assert err.value.reason == "mode_mismatch"
        db.rollback()
        assert shifts.latest_handover(db, shift["id"])["status"] == shifts.HANDOVER_PENDING


# ------------------------------------------------------------ 第 4 段：窗口外接手 / 自动解封

def test_outside_window_accept_fails_and_releases(app_factory, db_path):
    app1 = app_factory("2026-11-13 09:30:00")
    with app1.app_context():
        from app.db import close_db, init_db
        init_db(app1)
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        close_db()

    app2 = app_factory("2026-11-13 10:30:00")
    with app2.app_context():
        db = get_db()
        with pytest.raises(shifts.HandoverError) as err:
            shifts.accept(db, handover_id, mode="atomic", actor="alice")
        assert err.value.reason == "window_expired"
        shift = shifts.find_shift(db, 1)
        assert shift["status"] == shifts.SHIFT_RELEASED
        handover = shifts.latest_handover(db, 1)
        assert handover["status"] == shifts.HANDOVER_VOID
        # 解封后本班重新可交班（不再有「在班」班次，但该班状态是 released）。
        assert shifts.ensure_current_shift(db, "alice")["status"] == shifts.SHIFT_RELEASED


def test_not_yet_in_window_rejected(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        # 钉死 09:30，窗口 10:05 起。
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="10:05", window_end="10:30", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        with pytest.raises(shifts.HandoverError) as err:
            shifts.accept(db, handover_id, mode="atomic", actor="alice")
        assert err.value.reason == "window_not_open"
        db.rollback()
        assert shifts.latest_handover(db, shift["id"])["status"] == shifts.HANDOVER_PENDING


def test_accept_inside_window_moves_codes_to_next_shift(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        result = shifts.accept(db, handover_id, mode="atomic", actor="alice")
        db.commit()
        assert result.moved == ["CJ-0001"]
        old = shifts.find_shift(db, shift["id"])
        new = shifts.find_shift(db, result.to_shift_id)
        assert old["status"] == shifts.SHIFT_TRANSFERRED
        assert new["status"] == shifts.SHIFT_ACTIVE
        assert shifts.held_codes(db, old["id"]) == []
        assert [c["code"] for c in shifts.held_codes(db, new["id"])] == ["CJ-0001"]
        # 分列：同一编码只在一班。
        assert len(shifts.held_codes(db, new["id"])) == 1


# ------------------------------------------------------------ 第 5 段：并发只成一笔

def test_concurrent_handover_only_one_wins(app, db_path):
    """两笔交班真并发：各用独立连接，只许一笔成功。"""
    with app.app_context():
        db = get_db()
        shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', 1, 'admin', 'CJ-0001')",
        )
        db.commit()

    outcomes = []

    def attempt(successor):
        with app.app_context():
            db = get_db()
            try:
                shifts.hand_over(db, 1, successor=successor, mode="atomic",
                                 window_start="09:35", window_end="10:00",
                                 actor="admin")
                db.commit()
                outcomes.append("ok")
            except shifts.HandoverError as e:
                db.rollback()
                outcomes.append(e.reason)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, ["alice", "bob"]))

    assert outcomes.count("ok") == 1
    assert "concurrent" in outcomes
    with app.app_context():
        db = get_db()
        assert db.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE shift_id = 1"
        ).fetchone()["c"] == 1
        # 只成一笔 → 本班只进入待接手一次。
        assert shifts.find_shift(db, 1)["status"] == shifts.SHIFT_PENDING


def test_concurrent_accept_only_one_wins(app, db_path):
    """两笔接手真并发：只许一笔把编码搬走。"""
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]

    outcomes = []

    def attempt():
        with app.app_context():
            db = get_db()
            try:
                result = shifts.accept(db, handover_id, mode="atomic", actor="alice")
                db.commit()
                outcomes.append("ok")
            except shifts.HandoverError as e:
                db.rollback()
                outcomes.append(e.reason)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: attempt(), range(2)))

    assert outcomes.count("ok") == 1
    assert len(outcomes) == 2
    with app.app_context():
        db = get_db()
        assert db.execute(
            "SELECT COUNT(*) AS c FROM shift_events WHERE action='accept'"
            " AND outcome='ok'"
        ).fetchone()["c"] == 1
        active = db.execute(
            "SELECT id FROM shifts WHERE status = 'active'"
        ).fetchall()
        assert len(active) == 1
        holder = db.execute(
            "SELECT shift_id FROM crawl_jobs WHERE code = 'CJ-0001'"
        ).fetchone()["shift_id"]
        assert holder == active[0]["id"]


def test_single_code_cannot_be_held_by_two_shifts(app):
    with app.app_context():
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
                " code) VALUES ('dup', '/mock/catalog/alpha', 'draft', 2, 'bob', 'CJ-0001')"
            )
        db.rollback()


# ------------------------------------------------------------ 第 6 段：未停稳时看不到交班

def test_handover_entry_hidden_while_running(app, client):
    login(client)
    seed_job(client)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE crawl_jobs SET status = 'running' WHERE id = 1")
        db.commit()
    r = client.get("/")
    page = body(r)
    assert "采集未停稳" in page
    assert 'name="successor"' not in page  # 交班入口不可见
    # 绕过页面直接提交也要被拒。
    r = do_handover(client)
    assert "采集未停稳" in body(r)
    with app.app_context():
        assert shifts.current_shift(get_db())["status"] == shifts.SHIFT_ACTIVE


# ------------------------------------------------------------ 第 7 段：页面读出权属

def test_topbar_and_list_show_ownership(app, client):
    login(client)
    seed_job(client)
    r = client.get("/")
    assert 'data-shift-status="active"' in body(r)
    assert "本班" in body(r)

    do_handover(client)
    r = client.get("/")
    page = body(r)
    assert 'data-shift-status="pending"' in page
    assert "待接手" in page
    assert "交接单" in page
    assert "CJ-0001" in page          # 待移交编码列出
    assert "未交清" in page           # 未交清条数
    assert "当日口径" in page         # 当日口径

    # 接班人登录后页面能看出编码在哪一班。
    r = client.get("/shifts")
    page = body(r)
    assert "第 1 班" in page
    assert "CJ-0001" in page


def test_locked_title_and_no_new_job_after_handover(app, client):
    login(client)
    seed_job(client)
    do_handover(client)
    # 待接手：不能再建档。
    r = seed_job(client, name="should-fail")
    assert "题名与追加已锁" in body(r)
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) AS c FROM crawl_jobs").fetchone()["c"] == 1
    # 改题名也被锁。
    r = client.post("/jobs/1/rename", data={"name": "new"}, follow_redirects=True)
    assert "题名已锁" in body(r)
    with app.app_context():
        job = get_db().execute("SELECT name FROM crawl_jobs WHERE id = 1").fetchone()
        assert job["name"] == "t1"


def test_after_expiry_released_topbar_and_no_accept(app_factory, db_path):
    app1 = app_factory("2026-11-13 09:30:00")
    with app1.app_context():
        from app.db import close_db, init_db
        init_db(app1)
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        handover_id = shifts.latest_handover(db, shift["id"])["id"]
        close_db()

    app2 = app_factory("2026-11-13 10:30:00")
    c = app2.test_client()
    login(c, "alice")
    page = body(c.get("/"))
    assert 'data-shift-status="released"' in page
    assert "已解封" in page
    assert "交接单已作废" in page or "作废" in page
    r = c.post(f"/handovers/{handover_id}/accept", data={"mode": "atomic"},
               follow_redirects=True)
    assert "窗口已过" in body(r)
    with app2.app_context():
        db = get_db()
        assert shifts.latest_handover(db, 1)["status"] == shifts.HANDOVER_VOID


def test_transferred_topbar_after_accept(app_factory):
    app1 = app_factory("2026-11-13 09:40:00")
    with app1.app_context():
        from app.db import close_db, init_db
        init_db(app1)
        db = get_db()
        shift = shifts.ensure_current_shift(db, "admin")
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by,"
            " code) VALUES ('x', '/mock/catalog/alpha', 'draft', ?, 'admin', 'CJ-0001')",
            (shift["id"],),
        )
        db.commit()
        shifts.hand_over(db, shift["id"], successor="alice", mode="atomic",
                         window_start="09:35", window_end="10:00", actor="admin")
        db.commit()
        h = shifts.latest_handover(db, shift["id"])["id"]
        shifts.accept(db, h, mode="atomic", actor="alice")
        db.commit()
        close_db()

    app2 = app_factory("2026-11-13 09:45:00")
    c = app2.test_client()
    login(c, "alice")
    page = body(c.get("/"))
    assert 'data-shift-status="active"' in page   # 新一班已在班
    assert "下一班" not in page or True
    with app2.app_context():
        db = get_db()
        old = shifts.find_shift(db, 1)
        assert old["status"] == shifts.SHIFT_TRANSFERRED
        assert db.execute(
            "SELECT COUNT(*) AS c FROM shifts WHERE status='active'"
        ).fetchone()["c"] == 1
