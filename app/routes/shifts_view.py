from flask import Blueprint, redirect, render_template, url_for

from app.db import get_db
from app.routes.auth import current_user, login_required
from app.services import shifts

bp = Blueprint("ledger", __name__)


def _rows(db) -> list:
    out = []
    for shift in db.execute("SELECT * FROM shifts ORDER BY id").fetchall():
        view = shifts.shift_view(db, shift["id"])
        handover = view["handover"]
        out.append({
            **view,
            "status_text": shifts.SHIFT_TEXT.get(shift["status"], shift["status"]),
            "handover_text": shifts.HANDOVER_TEXT.get(
                handover["status"], handover["status"]
            ) if handover else "—",
            "codes": shifts.held_codes(db, shift["id"]),
            "reason_text": shifts.REASON_LABELS.get(handover["reason"])
            if handover and handover["reason"] else None,
        })
    return out


@bp.post("/shifts/expire")
@login_required
def expire():
    db = get_db()
    shifts.expire_if_due(db)
    db.commit()
    return redirect(url_for("jobs.list_jobs"))


@bp.get("/shifts")
@login_required
def ledger():
    db = get_db()
    shifts.expire_if_due(db)
    db.commit()
    return render_template(
        "shifts.html",
        rows=_rows(db),
        user=current_user(),
        today=shifts.today_key(),
        mode_labels=shifts.MODE_LABELS,
    )
