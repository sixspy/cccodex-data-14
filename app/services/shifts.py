"""交班 / 接手：跨班权属转移。

口径（已定，不再更改）：**整班原子移交**。
交班必须把本班全部未移交编码放进同一笔交接单；缺任何一条，整笔失败并回滚到
交班前，本班仍是「本班」。未交出去的编码只在交接单被作废或异常时才留在原班，
并在交接单标 `stuck`（未交清）。

权属只落一班：
- `crawl_jobs.code` 全表唯一 → 同一编码不可能同时挂在两班；
- 只有接手动作会改写 `crawl_jobs.shift_id`，且只在窗口内发生；
- `ux_shifts_single_active` 让「在班」永远只有一班；
- `ux_handovers_one_pending` 让并发交班只成一笔。

任何拒绝 / 争用都写 `shift_events`。
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.db import db_transaction
from app.services.clock import now, stamp

MODE_ATOMIC = "atomic"
MODE_STAGED = "staged"
# 本班口径已定：**整班原子移交**；staged 仅作为「对不上」的已知方式名识别。
KNOWN_MODES = (MODE_ATOMIC, MODE_STAGED)
TRANSFER_MODES = (MODE_ATOMIC,)
MODE_LABELS = {MODE_ATOMIC: "整班原子移交", MODE_STAGED: "部分移交"}

SHIFT_ACTIVE = "active"
SHIFT_PENDING = "pending"
SHIFT_TRANSFERRED = "transferred"
SHIFT_RELEASED = "released"
SHIFT_LEDGER = "ledger"

HANDOVER_PENDING = "pending"
HANDOVER_ACCEPTED = "accepted"
HANDOVER_VOID = "void"
HANDOVER_PARTIAL = "partial"

CODE_PENDING = "pending"
CODE_MOVED = "moved"
CODE_STUCK = "stuck"

SHIFT_TEXT = {
    SHIFT_ACTIVE: "本班",
    SHIFT_PENDING: "待接手",
    SHIFT_TRANSFERRED: "已移交",
    SHIFT_RELEASED: "已解封",
    SHIFT_LEDGER: "下一班账册",
}

HANDOVER_TEXT = {
    HANDOVER_PENDING: "待接手",
    HANDOVER_ACCEPTED: "已接手",
    HANDOVER_VOID: "已作废",
    HANDOVER_PARTIAL: "未交清",
}

REASON_LABELS = {
    "no_successor": "请填写接班人",
    "mode_mismatch": "方式不对：移交方式与当日登记不一致",
    "chose_self": "接班人不能是建档人",
    "not_pending": "本班当前不在待接手状态",
    "not_successor": "当前用户不是本单接班人",
    "window_not_open": "尚未进入交接窗口",
    "window_expired": "交接窗口已过，本班已自动解封",
    "crawl_running": "采集未停稳，不能交班",
    "concurrent": "争用失败：另一笔交班或接手已先成功",
    "atomic_incomplete": "整班原子移交：有编码未纳入交接单，整笔回滚",
    "bad_window": "交接窗口起止不合法",
    "bad_mode": "移交方式不合法",
    "no_codes": "本班没有可移交的编码",
    "not_found": "交接单不存在",
}


class HandoverError(Exception):
    def __init__(self, reason: str, message: str | None = None,
                 outcome: str | None = None):
        self.reason = reason
        self.outcome = outcome or (
            "conflict" if reason == "concurrent" else "rejected"
        )
        super().__init__(message or REASON_LABELS.get(reason, reason))


@dataclass
class HandoverResult:
    handover_id: int | None = None
    shift_id: int | None = None
    to_shift_id: int | None = None
    moved: list = field(default_factory=list)
    stuck: list = field(default_factory=list)
    outcome: str = "ok"
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


# ---------------------------------------------------------------- 读取侧

def shift_label(shift) -> str:
    """「第 N 班 · 首班 / 接手班」。"""
    if shift is None:
        return "—"
    return f"第 {shift['id']} 班 · {shift['label']}"


def find_shift(db, shift_id: int):
    return db.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()


def current_shift(db):
    return db.execute(
        "SELECT * FROM shifts WHERE status = ? ORDER BY id DESC LIMIT 1",
        (SHIFT_ACTIVE,),
    ).fetchone()


def latest_handover(db, shift_id: int):
    return db.execute(
        "SELECT * FROM handovers WHERE shift_id = ? ORDER BY id DESC LIMIT 1",
        (shift_id,),
    ).fetchone()


def incoming_handover(db, shift_id: int):
    """下一班账册：指向本班的交接单（接手前的预看）。"""
    return db.execute(
        "SELECT * FROM handovers WHERE to_shift_id = ? AND status = ?"
        " ORDER BY id DESC LIMIT 1",
        (shift_id, HANDOVER_PENDING),
    ).fetchone()


def last_transferred(db):
    """最近一笔已接手的班次，供顶栏显示「已移交 → 下一班」。"""
    return db.execute(
        "SELECT h.*, s.label AS from_label, s.status AS from_status,"
        " n.label AS to_label FROM handovers h"
        " JOIN shifts s ON s.id = h.shift_id"
        " LEFT JOIN shifts n ON n.id = h.to_shift_id"
        " WHERE h.status = ? ORDER BY h.id DESC LIMIT 1",
        (HANDOVER_ACCEPTED,),
    ).fetchone()


def held_codes(db, shift_id: int) -> list:
    return db.execute(
        "SELECT * FROM crawl_jobs WHERE shift_id = ? ORDER BY code", (shift_id,)
    ).fetchall()


def handover_codes(db, handover_id: int) -> list:
    return db.execute(
        "SELECT hc.code, hc.state, cj.name AS title FROM handover_codes hc"
        " JOIN crawl_jobs cj ON cj.id = hc.job_id"
        " WHERE hc.handover_id = ? ORDER BY hc.code",
        (handover_id,),
    ).fetchall()


def stuck_count(db, handover_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) AS c FROM handover_codes"
        " WHERE handover_id = ? AND state = ?",
        (handover_id, CODE_STUCK),
    ).fetchone()["c"]


def shift_view(db, shift_id: int) -> dict:
    shift = find_shift(db, shift_id)
    if shift is None:
        return {}
    handover = latest_handover(db, shift_id)
    held = held_codes(db, shift_id)
    counts_stuck = handover is not None and handover["status"] in (
        HANDOVER_PENDING, HANDOVER_PARTIAL
    )
    return {
        "shift": shift,
        "label": shift_label(shift),
        "handover": handover,
        "held": held,
        "held_count": len(held),
        "stuck": stuck_count(db, handover["id"]) if counts_stuck else 0,
        "running": any(j["status"] == "running" for j in held),
        "mode_label": MODE_LABELS.get(handover["mode"]) if handover else None,
    }


def status_line(view: dict | None = None) -> str:
    """顶栏一行口径：本班 / 待接手 / 已移交 / 已解封。"""
    if not view:
        return "无在班班次"
    shift, label = view["shift"], view["label"]
    status = shift["status"]
    if status == SHIFT_ACTIVE:
        return f"{label} · 本班"
    if status == SHIFT_PENDING:
        return f"{label} · 待接手（题名与追加已锁）"
    if status == SHIFT_TRANSFERRED:
        return f"{label} · 已移交（编码已入下一班）"
    if status == SHIFT_LEDGER:
        return f"{label} · 下一班账册"
    return f"{label} · 已解封（本班可继续操作）"


def release_note(shift) -> str | None:
    """已解封班次在页面上的提示：交接单作废、不可再点接手。"""
    if shift is None or shift["status"] != SHIFT_RELEASED:
        return None
    return "交接窗口已过，交接单作废，本班已解封，不能再点接手"


# ---------------------------------------------------------------- 班次与口径

def ensure_current_shift(db, actor: str):
    """取当前控制台班次：本班优先，其次待接手，其次已解封；都没有才新开一班。"""
    for status in (SHIFT_ACTIVE, SHIFT_PENDING, SHIFT_RELEASED):
        row = db.execute(
            "SELECT * FROM shifts WHERE status = ? ORDER BY id DESC LIMIT 1",
            (status,),
        ).fetchone()
        if row is not None:
            return row
    db.execute(
        "INSERT INTO shifts (label, creator, status, created_at) VALUES (?, ?, ?, ?)",
        ("首班", actor, SHIFT_ACTIVE, stamp()),
    )
    return current_shift(db)

def registered_mode(db, day: str) -> str | None:
    row = db.execute(
        "SELECT mode FROM day_registrations WHERE day = ?", (day,)
    ).fetchone()
    return row["mode"] if row else None


def register_day(db, day: str, mode: str, actor: str) -> None:
    db.execute(
        "INSERT INTO day_registrations (day, mode, registered_by, created_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET mode = excluded.mode,"
        " registered_by = excluded.registered_by",
        (day, mode, actor, stamp()),
    )


def today_key() -> str:
    return now().strftime("%Y-%m-%d")


def parse_clock(text: str) -> datetime:
    """`HH:MM` 按后台时刻的当天取钟点；完整日期时间原样解析。"""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(microsecond=0)
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = (int(x) for x in text.split(":"))
        if hour > 23 or minute > 59:
            raise HandoverError("bad_window")
        return now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    raise HandoverError("bad_window")


# ---------------------------------------------------------------- 交班

def hand_over(db, shift_id: int, *, successor: str, mode: str, window_start: str,
              window_end: str, actor: str,
              code_ids: list | None = None) -> HandoverResult:
    successor = (successor or "").strip()
    mode = (mode or "").strip()
    if not successor:
        raise HandoverError("no_successor")

    start = parse_clock(window_start)
    end = parse_clock(window_end)
    if end <= start:
        end = _next_day(end, start)

    day = today_key()
    try:
        with db_transaction(immediate=True):
            registered = registered_mode(db, day)
            if registered is not None and registered != mode:
                raise HandoverError("mode_mismatch")
            if mode not in TRANSFER_MODES:
                raise HandoverError("bad_mode")
            register_day(db, day, mode, actor)
            return _hand_over_tx(
                db, shift_id, successor=successor, mode=mode, actor=actor,
                start=start, end=end, code_ids=code_ids, day=day,
            )
    except HandoverError as e:
        # 拒绝记录在回滚之后单独落库，保证「写争用失败 / 方式不对」可追溯。
        _audit_after(db, shift_id, None, "handover", e.outcome, actor, e.reason)
        raise
    except sqlite3.IntegrityError:
        _audit_after(db, shift_id, None, "handover", "conflict", actor, "concurrent")
        raise HandoverError("concurrent")


def _is_creator_of_shift(db, shift, person: str) -> bool:
    """建档人：班次建档人或本班任一编码的建档人。"""
    if shift["creator"] == person:
        return True
    return db.execute(
        "SELECT 1 FROM crawl_jobs WHERE shift_id = ? AND created_by = ? LIMIT 1",
        (shift["id"], person),
    ).fetchone() is not None


def _hand_over_tx(db, shift_id, *, successor, mode, actor, start, end, code_ids, day):
    shift = find_shift(db, shift_id)
    if shift is None:
        raise HandoverError("not_found")
    if _is_creator_of_shift(db, shift, successor):
        raise HandoverError("chose_self")
    # 仅本班或已解封可交班；待接手 / 已移交视为争用。
    if shift["status"] not in (SHIFT_ACTIVE, SHIFT_RELEASED):
        raise HandoverError("concurrent")
    held = held_codes(db, shift_id)
    if any(j["status"] == "running" for j in held):
        raise HandoverError("crawl_running")
    if not held:
        raise HandoverError("no_codes")

    # 整班原子移交：未点名默认全量；点名但漏掉任何一条 → 整笔失败并回滚。
    if code_ids is not None:
        named = set(dict.fromkeys(code_ids))
        missing = [j["code"] for j in held if j["id"] not in named]
        if missing:
            raise HandoverError("atomic_incomplete")
    selected = held

    # 抢本班 → 待接手：并发交班只有一笔能改到状态。
    changed = db.execute(
        "UPDATE shifts SET status = ?, successor = ?, mode = ?,"
        " window_start = ?, window_end = ? WHERE id = ? AND status = ?",
        (SHIFT_PENDING, successor, mode, stamp(start), stamp(end),
         shift_id, shift["status"]),
    ).rowcount
    if changed != 1:
        raise HandoverError("concurrent")

    cur = db.execute(
        "INSERT INTO handovers (shift_id, successor, mode, registered_on,"
        " window_start, window_end, status, code_count, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (shift_id, successor, mode, day, stamp(start), stamp(end),
         HANDOVER_PENDING, len(selected), stamp()),
    )
    handover_id = cur.lastrowid
    for job in selected:
        db.execute(
            "INSERT INTO handover_codes (handover_id, job_id, code, state)"
            " VALUES (?, ?, ?, ?)",
            (handover_id, job["id"], job["code"], CODE_PENDING),
        )
    db.execute("UPDATE shifts SET handover_id = ? WHERE id = ?", (handover_id, shift_id))
    _audit(db, shift_id, handover_id, "handover", "ok", actor,
           f"atomic n={len(selected)}")
    return HandoverResult(handover_id=handover_id, shift_id=shift_id)


# ---------------------------------------------------------------- 接手

def accept(db, handover_id: int, *, mode: str, actor: str) -> HandoverResult:
    # 窗口已过：先把解封 / 作废落库，再报窗口外失败。
    _expire_if_due(db, handover_id)
    mode = (mode or "").strip()
    try:
        with db_transaction(immediate=True):
            return _accept_tx(db, handover_id, mode=mode, actor=actor)
    except HandoverError as e:
        _audit_after(db, None, handover_id, "accept", e.outcome, actor, e.reason)
        raise
    except sqlite3.IntegrityError:
        _audit_after(db, None, handover_id, "accept", "conflict", actor, "concurrent")
        raise HandoverError("concurrent")


def _accept_tx(db, handover_id, *, mode, actor):
    handover = db.execute(
        "SELECT * FROM handovers WHERE id = ?", (handover_id,)
    ).fetchone()
    if handover is None:
        raise HandoverError("not_found")

    shift = find_shift(db, handover["shift_id"])
    if handover["status"] != HANDOVER_PENDING or shift["status"] != SHIFT_PENDING:
        reason = "window_expired" if handover["status"] == HANDOVER_VOID else "not_pending"
        raise HandoverError(reason)

    if actor != handover["successor"]:
        same_as_creator = _is_creator_of_shift(db, shift, handover["successor"])
        raise HandoverError("chose_self" if same_as_creator else "not_successor")

    if mode != handover["mode"]:
        raise HandoverError("mode_mismatch")

    day_mode = registered_mode(db, today_key())
    if day_mode and day_mode != mode:
        raise HandoverError("mode_mismatch")

    if now() < parse_clock(handover["window_start"]):
        raise HandoverError("window_not_open")

    # 抢交接单：并发接手只有一笔成功。
    changed = db.execute(
        "UPDATE handovers SET status = ? WHERE id = ? AND status = ?",
        (HANDOVER_ACCEPTED, handover_id, HANDOVER_PENDING),
    ).rowcount
    if changed != 1:
        raise HandoverError("concurrent")

    missing = _missing_codes(db, handover_id, shift["id"])
    if missing:
        # 整班原子口径：漏了任何一条就整笔不搬，编码全部留本班并标未交清。
        db.execute(
            "UPDATE handovers SET status = ?, reason = ?, closed_at = ? WHERE id = ?",
            (HANDOVER_PARTIAL, "atomic_incomplete", stamp(), handover_id),
        )
        db.execute(
            "UPDATE handover_codes SET state = ? WHERE handover_id = ?",
            (CODE_STUCK, handover_id),
        )
        db.execute(
            "UPDATE shifts SET status = ?, handover_id = NULL, successor = NULL,"
            " mode = NULL, window_start = NULL, window_end = NULL WHERE id = ?",
            (SHIFT_ACTIVE, shift["id"]),
        )
        _audit(db, shift["id"], handover_id, "accept", "partial", actor,
               ",".join(missing))
        return HandoverResult(handover_id=handover_id, shift_id=shift["id"],
                              moved=[], stuck=missing,
                              outcome="partial", reason="atomic_incomplete")

    to_shift_id = _ensure_next_shift(db, handover, actor)
    moved = _transfer_codes(db, handover_id, shift["id"], to_shift_id)

    # 整班交清：旧班让位后再把下一班设为在班，唯一索引兜底「只落一班」。
    db.execute("UPDATE shifts SET status = ? WHERE id = ?",
               (SHIFT_TRANSFERRED, shift["id"]))
    db.execute("UPDATE shifts SET status = ? WHERE id = ?", (SHIFT_ACTIVE, to_shift_id))
    db.execute("UPDATE handovers SET closed_at = ? WHERE id = ?", (stamp(), handover_id))
    _audit(db, shift["id"], handover_id, "accept", "ok", actor, f"to_shift={to_shift_id}")
    return HandoverResult(handover_id=handover_id, shift_id=shift["id"],
                          to_shift_id=to_shift_id, moved=moved)


def _missing_codes(db, handover_id, from_shift_id) -> list:
    """交接单里已不在本班的编码。"""
    missing = []
    for row in handover_codes(db, handover_id):
        job = db.execute(
            "SELECT shift_id FROM crawl_jobs WHERE code = ?", (row["code"],)
        ).fetchone()
        if job is None or job["shift_id"] != from_shift_id:
            missing.append(row["code"])
    return missing


def _transfer_codes(db, handover_id, from_shift_id, to_shift_id) -> list:
    """把交接单上的编码整批改挂下一班（调用前已确认全部仍在本班）。"""
    moved = []
    for row in handover_codes(db, handover_id):
        job = db.execute(
            "SELECT id FROM crawl_jobs WHERE code = ? AND shift_id = ?",
            (row["code"], from_shift_id),
        ).fetchone()
        db.execute("UPDATE crawl_jobs SET shift_id = ? WHERE id = ?",
                   (to_shift_id, job["id"]))
        db.execute(
            "UPDATE handover_codes SET state = ? WHERE handover_id = ? AND code = ?",
            (CODE_MOVED, handover_id, row["code"]),
        )
        moved.append(row["code"])
    return moved


def _ensure_next_shift(db, handover, actor: str) -> int:
    if handover["to_shift_id"]:
        return handover["to_shift_id"]
    next_no = (db.execute("SELECT MAX(id) AS m FROM shifts").fetchone()["m"] or 0) + 1
    cur = db.execute(
        "INSERT INTO shifts (label, creator, status, created_at) VALUES (?, ?, ?, ?)",
        (f"接手班 {next_no}", actor, SHIFT_LEDGER, stamp()),
    )
    db.execute("UPDATE handovers SET to_shift_id = ? WHERE id = ?",
               (cur.lastrowid, handover["id"]))
    return cur.lastrowid


# ---------------------------------------------------------------- 解封

def expire_if_due(db, actor: str = "system") -> list:
    """窗口过后仍未接手：本班自动解封、交接单作废。"""
    out = []
    for shift in db.execute(
        "SELECT * FROM shifts WHERE status = ?", (SHIFT_PENDING,)
    ).fetchall():
        handover = latest_handover(db, shift["id"])
        if handover is None or handover["status"] != HANDOVER_PENDING:
            continue
        if now() > parse_clock(handover["window_end"]) and \
                _expire_one(db, shift, handover, actor):
            out.append(shift["id"])
    return out


def _expire_if_due(db, handover_id: int) -> None:
    handover = db.execute("SELECT * FROM handovers WHERE id = ?",
                          (handover_id,)).fetchone()
    if handover is None or handover["status"] != HANDOVER_PENDING:
        return
    shift = find_shift(db, handover["shift_id"])
    if shift and shift["status"] == SHIFT_PENDING and \
            now() > parse_clock(handover["window_end"]):
        _expire_one(db, shift, handover, "system")


def _expire_one(db, shift, handover, actor: str) -> bool:
    with db_transaction(immediate=True):
        changed = db.execute(
            "UPDATE handovers SET status = ?, reason = ?, closed_at = ?"
            " WHERE id = ? AND status = ?",
            (HANDOVER_VOID, "window_expired", stamp(), handover["id"], HANDOVER_PENDING),
        ).rowcount
        if changed != 1:
            return False
        db.execute("UPDATE handover_codes SET state = ? WHERE handover_id = ?",
                   (CODE_STUCK, handover["id"]))
        db.execute(
            "UPDATE shifts SET status = ?, handover_id = NULL, successor = NULL,"
            " mode = NULL, window_start = NULL, window_end = NULL WHERE id = ?",
            (SHIFT_RELEASED, shift["id"]),
        )
        _audit(db, shift["id"], handover["id"], "accept", "void", actor,
               "window_expired")
    return True


# ---------------------------------------------------------------- 审计

def _audit(db, shift_id, handover_id, action, outcome, actor, detail):
    db.execute(
        "INSERT INTO shift_events (shift_id, handover_id, action, outcome, actor,"
        " detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (shift_id, handover_id, action, outcome, actor, detail, stamp()),
    )


def _audit_after(db, shift_id, handover_id, action, outcome, actor, detail):
    """事务已回滚后补写审计，避免拒绝记录被一起回滚掉。"""
    try:
        with db_transaction(immediate=True):
            _audit(db, shift_id, handover_id, action, outcome, actor, detail)
    except sqlite3.Error:
        pass


def audit(db, shift_id, handover_id, action, outcome, actor, detail) -> None:
    """公开入口：供路由层在拒绝后补记事件。"""
    _audit_after(db, shift_id, handover_id, action, outcome, actor, detail)


def recent_events(db, limit: int = 12):
    return db.execute("SELECT * FROM shift_events ORDER BY id DESC LIMIT ?",
                      (limit,)).fetchall()


def _next_day(moment: datetime, after: datetime) -> datetime:
    while moment <= after:
        moment += timedelta(days=1)
    return moment
