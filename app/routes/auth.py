from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.config import Config

bp = Blueprint("auth", __name__)


def users() -> dict:
    return current_app.config.get("USERS") or Config.USERS


def current_user() -> str:
    return session.get("user") or ""


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def login_as(client, username="admin"):
    pw = users().get(username, "123456")
    return client.post(
        "/login",
        data={"username": username, "password": pw},
        follow_redirects=True,
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pwd = request.form.get("password", "")
        if users().get(user) == pwd:
            session["user"] = user
            return redirect(request.args.get("next") or url_for("jobs.list_jobs"))
        flash("账号或密码错误", "error")
    return render_template("login.html")


@bp.route("/switch", methods=["POST"])
def switch():
    """演示用：切到另一账号，便于双方在同一窗口演练交班 / 接手。"""
    user = request.form.get("username", "").strip()
    if user in users():
        session["user"] = user
    return redirect(request.form.get("next") or url_for("jobs.list_jobs"))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
