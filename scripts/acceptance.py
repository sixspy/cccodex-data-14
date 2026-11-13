"""执行业务验收：七段口径逐条打点，输出可读报告。

运行：
    python -m scripts.acceptance

每段使用独立临时库，时刻用 app.config['FIXED_NOW'] 钉死。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import close_db, get_db, init_db  # noqa: E402
from app.services import shifts  # noqa: E402
from tests.conftest import make_app  # noqa: E402

RESULTS = []


def check(section: str, name: str, passed: bool, detail: str = ""):
    RESULTS.append((section, name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {section} · {name}" + (f" — {detail}" if detail else ""))


def fresh(now="2026-11-13 09:35:00"):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = make_app(path, now)
    with app.app_context():
        init_db(app)
    return app, path


def login(client, user="admin"):
    client.post("/login", data={"username": user, "password": "123456"},
                follow_redirects=True)


def add_job(db, shift_id, code, name="j", creator="admin"):
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by, code)"
        " VALUES (?, '/mock/catalog/alpha', 'draft', ?, ?, ?)",
        (name, shift_id, creator, code),
    )


def text(resp):
    return resp.get_data(as_text=True)


def section_1():
    app, _ = fresh()
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        add_job(db, s["id"], "CJ-0001")
        db.commit()
        try:
            shifts.hand_over(db, s["id"], successor="admin", mode="atomic",
                             window_start="09:30", window_end="10:10",
                             actor="admin")
            rejected = False
        except shifts.HandoverError as e:
            rejected = e.reason == "chose_self"
        db.rollback()
        check("第1段 建档人当接班人", "交班被拒且本班不变",
              rejected and shifts.current_shift(db)["status"] == "active",
              "reason=chose_self")
        # 编码级建档人也不能接班
        s2 = shifts.ensure_current_shift(db, "bob")
        add_job(db, s2["id"], "CJ-0002", creator="carol")
        db.commit()
        try:
            shifts.hand_over(db, s2["id"], successor="carol", mode="atomic",
                             window_start="09:30", window_end="10:10",
                             actor="bob")
            rejected = False
        except shifts.HandoverError as e:
            rejected = e.reason == "chose_self"
        db.rollback()
        check("第1段 建档人当接班人", "编码建档人同样被拒", rejected)


def section_2():
    app, _ = fresh()
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        for n in (1, 2, 3):
            add_job(db, s["id"], f"CJ-000{n}", name=f"j{n}")
        db.commit()
        try:
            shifts.hand_over(db, s["id"], successor="alice", mode="atomic",
                             window_start="09:30", window_end="10:10",
                             actor="admin", code_ids=[1, 2])
            ok = False
        except shifts.HandoverError as e:
            ok = e.reason == "atomic_incomplete"
        db.rollback()
        held = [c["code"] for c in shifts.held_codes(db, s["id"])]
        check("第2段 原子回滚", "缺一条整笔失败并回滚",
              ok and held == ["CJ-0001", "CJ-0002", "CJ-0003"]
              and shifts.latest_handover(db, s["id"]) is None)


def section_2b():
    app, _ = fresh()
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        add_job(db, s["id"], "CJ-0001")
        add_job(db, s["id"], "CJ-0002")
        db.commit()
        shifts.hand_over(db, s["id"], successor="alice", mode="atomic",
                         window_start="09:30", window_end="10:10", actor="admin")
        db.commit()
        h = shifts.latest_handover(db, s["id"])
        db.execute("INSERT INTO shifts (id, label, creator, status)"
                   " VALUES (99, '旁班', 'bob', 'ledger')")
        db.execute("UPDATE crawl_jobs SET shift_id = 99 WHERE code = 'CJ-0002'")
        db.commit()
        r = shifts.accept(db, h["id"], mode="atomic", actor="alice")
        db.commit()
        h2 = shifts.latest_handover(db, s["id"])
        check("第2段 部分留存", "有一条不在本班则整笔不搬、留本班并标未交清",
              r.outcome == "partial" and r.stuck == ["CJ-0002"]
              and r.moved == []
              and [c["code"] for c in shifts.held_codes(db, s["id"])] == ["CJ-0001"]
              and h2["status"] == "partial"
              and shifts.stuck_count(db, h2["id"]) == 2)


def section_3():
    app, _ = fresh()
    client = app.test_client()
    login(client)
    client.post("/jobs", data={"name": "t", "seed_path": "/mock/catalog/alpha"},
                follow_redirects=True)
    # 当日已登记口径为「整班原子移交」；交班却提交另一种方式。
    with app.app_context():
        db = get_db()
        shifts.register_day(db, shifts.today_key(), "atomic", "admin")
        db.commit()
    r = client.post("/shift/handover",
                    data={"successor": "alice", "mode": "staged",
                          "window_start": "09:30", "window_end": "10:10"},
                    follow_redirects=True)
    with app.app_context():
        db = get_db()
        unchanged = shifts.current_shift(db)["status"] == "active"
        events = db.execute(
            "SELECT COUNT(*) AS c FROM shift_events WHERE detail='mode_mismatch'"
        ).fetchone()["c"]
    check("第3段 方式不对", "移交方式与当日登记不一致被拒",
          "方式不对" in text(r) and unchanged and events == 1,
          f"events={events}")


def section_4():
    app, _ = fresh("2026-11-13 09:35:00")
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        add_job(db, s["id"], "CJ-0001")
        db.commit()
        shifts.hand_over(db, s["id"], successor="alice", mode="atomic",
                         window_start="09:30", window_end="10:10", actor="admin")
        db.commit()
        h = shifts.latest_handover(db, s["id"])
        close_db()

    app2 = make_app(tempfile.mkstemp(suffix=".db")[1]) if False else None
    # 换时刻：同一个库要用同一路径，这里直接改配置。
    app.config["FIXED_NOW"] = "2026-11-13 10:30:00"
    with app.app_context():
        db = get_db()
        try:
            shifts.accept(db, h["id"], mode="atomic", actor="alice")
            rejected = None
        except shifts.HandoverError as e:
            rejected = e.reason
        db.commit()
        shift = shifts.find_shift(db, s["id"])
        handover = shifts.latest_handover(db, s["id"])
        check("第4段 窗口外", "窗口外接手失败且本班已解封、交接单作废",
              rejected == "window_expired" and shift["status"] == "released"
              and handover["status"] == "void")


def section_5():
    app, _ = fresh()
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        add_job(db, s["id"], "CJ-0001")
        db.commit()
        # 第一笔成功
        shifts.hand_over(db, s["id"], successor="alice", mode="atomic",
                         window_start="09:30", window_end="10:10", actor="admin")
        db.commit()
        # 第二笔并发交班
        try:
            shifts.hand_over(db, s["id"], successor="bob", mode="atomic",
                             window_start="09:30", window_end="10:10",
                             actor="admin")
            second = None
        except shifts.HandoverError as e:
            second = e.reason
        db.rollback()
        count = db.execute("SELECT COUNT(*) AS c FROM handovers").fetchone()["c"]
        check("第5段 并发交班", "只成一笔，另一笔争用失败",
              second == "concurrent" and count == 1,
              f"second={second} handovers={count}")


def section_5b():
    app, _ = fresh()
    with app.app_context():
        db = get_db()
        s = shifts.ensure_current_shift(db, "admin")
        add_job(db, s["id"], "CJ-0001")
        db.commit()
        shifts.hand_over(db, s["id"], successor="alice", mode="atomic",
                         window_start="09:30", window_end="10:10", actor="admin")
        db.commit()
        h = shifts.latest_handover(db, s["id"])
        ok = shifts.accept(db, h["id"], mode="atomic", actor="alice")
        db.commit()
        try:
            shifts.accept(db, h["id"], mode="atomic", actor="alice")
            second = None
        except shifts.HandoverError as e:
            second = e.reason
        db.rollback()
        oks = db.execute(
            "SELECT COUNT(*) AS c FROM shift_events WHERE action='accept'"
            " AND outcome='ok'"
        ).fetchone()["c"]
        check("第5段 并发接手", "只成一笔，重放争用失败",
              ok.ok and second in ("not_pending", "concurrent") and oks == 1,
              f"second={second} ok_events={oks}")


def section_6():
    app, _ = fresh()
    client = app.test_client()
    login(client)
    client.post("/jobs", data={"name": "t", "seed_path": "/mock/catalog/alpha"},
                follow_redirects=True)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE crawl_jobs SET status='running' WHERE id=1")
        db.commit()
    page = text(client.get("/"))
    r = client.post("/shift/handover",
                    data={"successor": "alice", "mode": "atomic",
                          "window_start": "09:30", "window_end": "10:10"},
                    follow_redirects=True)
    with app.app_context():
        active = shifts.current_shift(get_db())["status"] == "active"
    check("第6段 未停稳", "页面上看不到交班入口，且提交被拒",
          'name="successor"' not in page and "采集未停稳" in page
          and "采集未停稳" in text(r) and active)


def section_7():
    app, _ = fresh()
    client = app.test_client()
    login(client)
    client.post("/jobs", data={"name": "t", "seed_path": "/mock/catalog/alpha"},
                follow_redirects=True)
    before = text(client.get("/"))
    client.post("/shift/handover",
                data={"successor": "alice", "mode": "atomic",
                      "window_start": "09:30", "window_end": "10:10"},
                follow_redirects=True)
    after = text(client.get("/"))
    ledger = text(client.get("/shifts"))
    check("第7段 页面读权属", "顶栏与交接单能读出编码在哪一班",
          'data-shift-status="active"' in before
          and 'data-shift-status="pending"' in after
          and "待接手" in after and "CJ-0001" in after
          and "未交清" in after and "当日口径" in after
          and "CJ-0001" in ledger)


def main():
    for fn in (section_1, section_2, section_2b, section_3, section_4,
               section_5, section_5b, section_6, section_7):
        fn()
    failed = [r for r in RESULTS if not r[2]]
    print("\n汇总：", f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
