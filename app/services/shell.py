"""顶栏与页面共用的班次上下文。"""

from flask import g

from app.db import get_db
from app.routes.auth import current_user
from app.services import shifts


def load_shell():
    """每个请求都刷新顶栏：先落窗口过期，再取本班视图。"""
    cached = getattr(g, "_shell", None)
    if cached is not None:
        return cached
    db = get_db()
    user = current_user()
    if not user:
        g._shell = {"user": "", "view": None, "line": "未登录",
                    "status": None, "handover": None, "prev": None,
                    "shift_text": shifts.SHIFT_TEXT}
        return g._shell
    shifts.expire_if_due(db)
    shift = shifts.ensure_current_shift(db, user)
    db.commit()
    view = shifts.shift_view(db, shift["id"])
    prev = shifts.last_transferred(db)
    g._shell = {
        "user": user,
        "view": view,
        "line": shifts.status_line(view),
        "status": shift["status"],
        "status_text": shifts.SHIFT_TEXT.get(shift["status"], shift["status"]),
        "handover": view["handover"],
        "handover_text": shifts.HANDOVER_TEXT.get(
            view["handover"]["status"], view["handover"]["status"]
        ) if view["handover"] else "—",
        "release_note": shifts.release_note(shift),
        "prev": prev,
        "shift_text": shifts.SHIFT_TEXT,
    }
    return g._shell


def shell_context() -> dict:
    return {"shell": load_shell()}
