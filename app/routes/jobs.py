from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.db import get_db
from app.routes.auth import current_user, login_required
from app.services import shifts
from app.services.mock_catalog import CATALOGS
from app.services.runner import run_job
from app.services.shell import load_shell, shell_context

bp = Blueprint("jobs", __name__)


def _flash_error(message: str):
    flash(message, "error")


def _locked_shift(db, job) -> bool:
    """题名与追加在待接手 / 已移交时锁住。"""
    if job["shift_id"] is None:
        return False
    row = db.execute("SELECT status FROM shifts WHERE id = ?",
                     (job["shift_id"],)).fetchone()
    return row is not None and row["status"] in (
        shifts.SHIFT_PENDING, shifts.SHIFT_TRANSFERRED, shifts.SHIFT_LEDGER
    )


@bp.app_context_processor
def inject_shell():
    return shell_context()


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    shifts.expire_if_due(db)
    shell = load_shell()
    jobs = db.execute("SELECT * FROM crawl_jobs ORDER BY id DESC").fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    shift_rows = [shifts.shift_view(db, s["id"]) for s in db.execute(
        "SELECT * FROM shifts ORDER BY id DESC"
    ).fetchall()]
    handovers = db.execute(
        "SELECT * FROM handovers ORDER BY id DESC LIMIT 20"
    ).fetchall()
    today = shifts.today_key()
    day_mode = shifts.registered_mode(db, today)
    events = shifts.recent_events(db)
    view = shell["view"]
    handover_codes = shifts.handover_codes(db, view["handover"]["id"]) \
        if view["handover"] and view["handover"]["status"] == shifts.HANDOVER_PENDING else []
    shift_labels = {s["id"]: shifts.shift_label(s) for s in db.execute(
        "SELECT * FROM shifts"
    ).fetchall()}
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        seeds=sorted(CATALOGS.keys()),
        shell=shell,
        shift_rows=shift_rows,
        handovers=handovers,
        handover_codes=handover_codes,
        shift_labels=shift_labels,
        mode_labels=shifts.MODE_LABELS,
        today=today,
        day_mode=day_mode,
        day_mode_label=shifts.MODE_LABELS.get(day_mode, "—") if day_mode else "—",
        events=events,
        shift_text=shifts.SHIFT_TEXT,
        handover_text=shifts.HANDOVER_TEXT,
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        _flash_error("请填写任务名与种子路径")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        _flash_error("种子路径必须是已注册的 mock 目录")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    shift = shifts.ensure_current_shift(db, current_user())
    if shift["status"] != shifts.SHIFT_ACTIVE:
        _flash_error("本班待接手 / 已移交，题名与追加已锁，不能建档")
        db.commit()
        return redirect(url_for("jobs.list_jobs"))
    cur = db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, shift_id, created_by)"
        " VALUES (?, ?, 'draft', ?, ?)",
        (name, seed, shift["id"], current_user()),
    )
    job_id = cur.lastrowid
    db.execute("UPDATE crawl_jobs SET code = 'CJ-' || printf('%04d', id) WHERE id = ?",
               (job_id,))
    db.commit()
    flash(f"任务已创建，编码 {_code_of(db, job_id)} 落入{shifts.shift_label(shift)}", "ok")
    return redirect(url_for("jobs.list_jobs"))


def _code_of(db, job_id: int) -> str:
    row = db.execute("SELECT code FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    return row["code"] if row else "—"


@bp.post("/jobs/<int:job_id>/rename")
@login_required
def rename_job(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        _flash_error("任务不存在")
        return redirect(url_for("jobs.list_jobs"))
    if _locked_shift(db, job):
        _flash_error("题名已锁：该编码处于待接手 / 已移交状态")
        return redirect(url_for("jobs.detail", job_id=job_id))
    name = request.form.get("name", "").strip()
    if not name:
        _flash_error("任务名不能为空")
        return redirect(url_for("jobs.detail", job_id=job_id))
    db.execute("UPDATE crawl_jobs SET name = ? WHERE id = ?", (name, job_id))
    db.commit()
    flash("题名已更新", "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
        flash(
            f"运行完成：写入 {summary['inserted']} 条，去重跳过 {summary['skipped_dup']} 条",
            "ok",
        )
    except ValueError as e:
        _flash_error(str(e))
    except Exception as e:
        _flash_error(f"运行失败: {e}")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    shifts.expire_if_due(db)
    shell = load_shell()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        _flash_error("任务不存在")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    holder = shifts.find_shift(db, job["shift_id"]) if job["shift_id"] else None
    locked = _locked_shift(db, job)
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        shell=shell,
        holder=holder,
        holder_label=shifts.shift_label(holder),
        locked=locked,
        shift_text=shifts.SHIFT_TEXT,
    )


# ---------------------------------------------------------------- 交班 / 接手

@bp.post("/shift/handover")
@login_required
def handover():
    db = get_db()
    shell = load_shell()
    view = shell["view"]
    shift = view["shift"]
    # 采集未停稳时入口不可见；这里兜底拒绝绕过页面提交。
    if view["running"]:
        _flash_error("采集未停稳，交班入口不可见，不能交班")
        return redirect(url_for("jobs.list_jobs"))
    try:
        result = shifts.hand_over(
            db, shift["id"],
            successor=request.form.get("successor", ""),
            mode=request.form.get("mode", ""),
            window_start=request.form.get("window_start", ""),
            window_end=request.form.get("window_end", ""),
            actor=current_user(),
        )
        db.commit()
    except shifts.HandoverError as e:
        db.commit()
        _flash_error(str(e))
        return redirect(url_for("jobs.list_jobs"))
    flash(
        f"交班成功：{len(view['held'])} 个编码待移交，接班人"
        f" {request.form.get('successor')}；本班顶栏改为待接手",
        "ok",
    )
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/handovers/<int:handover_id>/accept")
@login_required
def accept_handover(handover_id: int):
    db = get_db()
    try:
        result = shifts.accept(
            db, handover_id,
            mode=request.form.get("mode", ""),
            actor=current_user(),
        )
        db.commit()
    except shifts.HandoverError as e:
        db.commit()
        _flash_error(str(e))
        return redirect(url_for("jobs.list_jobs"))
    if result.stuck:
        _flash_error(
            "整班原子移交未交清：未交出去的编码留在本班并标未交清 "
            + "、".join(result.stuck)
        )
        return redirect(url_for("jobs.list_jobs"))
    flash(
        f"接手成功：{len(result.moved)} 个编码已进入"
        f"{shifts.shift_label(shifts.find_shift(db, result.to_shift_id))}",
        "ok",
    )
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/shift/expire")
@login_required
def expire_now():
    db = get_db()
    released = shifts.expire_if_due(db)
    db.commit()
    if released:
        flash(f"窗口已过：{len(released)} 班自动解封，交接单作废", "ok")
    else:
        flash("当前没有到期待解封的交接单", "error")
    return redirect(url_for("jobs.list_jobs"))
