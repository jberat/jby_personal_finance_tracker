"""
Personal Financial Tracker — a local-only personal finance web app.
Flask + SQLite, single user, runs at http://127.0.0.1:5005.
"""
import os
from functools import wraps
from flask import (Flask, g, session, request, redirect, url_for,
                   render_template, jsonify)
import config
from config import (
    APP_PASSWORD, SECRET_KEY, TOOLS_REGISTRY,
    _ALLOWED_ORIGINS, OWNER,
    IMPORTS_CHECKING_DIR, IMPORTS_CC_DIR, IMPORTS_OTHER_DIR,
)
from db import get_db, init_db


app = Flask(__name__)
app.secret_key = SECRET_KEY

# ─── Jinja filters ────────────────────────────────────────────────────────────

@app.template_filter("amt")
def amt(val):
    """Format a signed amount for display.

    50.0   → "$50.00"
    -50.0  → "-$50.00"   (credit / contra-expense)
    None/0 → "$0.00"
    Floating-point near-zero → "$0.00" (no negative-zero artifact)
    """
    if val is None:
        return "$0.00"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "$0.00"
    if abs(v) < 0.005:
        return "$0.00"
    if v < 0:
        return f"-${abs(v):,.2f}"
    return f"${v:,.2f}"

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


# ─── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    """Password-only login. Change the password by writing a new one to the
    gitignored .app_password file next to app.py and restarting — that file
    doubles as the recovery path if forgotten."""
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Wrong password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── CSRF protection ─────────────────────────────────────────────────────────
# Localhost binding does NOT stop cross-site requests: any webpage you visit
# can fire form posts / fetch() at 127.0.0.1:5005 with your session cookie.
# Browsers attach an Origin header to all cross-site (and same-site POST)
# requests, so: mutating request + browser Origin/Referer that isn't ours →
# reject. Requests with neither header (curl, scripts) pass through.

@app.before_request
def _csrf_origin_check():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    origin = request.headers.get("Origin")
    if origin:
        return None if origin.rstrip("/") in _ALLOWED_ORIGINS \
            else ("Cross-origin request blocked.", 403)
    referer = request.headers.get("Referer")
    if referer:
        ok = any(referer == o or referer.startswith(o + "/")
                 for o in _ALLOWED_ORIGINS)
        return None if ok else ("Cross-origin request blocked.", 403)
    return None  # no Origin/Referer → non-browser client → allow

# ─── Context processors ───────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    db = get_db()

    # Review queue badge
    pending = db.execute("""
        SELECT COUNT(*) FROM staging
        WHERE status IN ('pending','duplicate') AND owner=?
    """, (OWNER,)).fetchone()[0]

    endpoint = request.endpoint or ""
    current_portal = OWNER

    # Cache-bust static assets by file mtime — without this, browsers cache
    # style.css / main.js indefinitely and don't pick up changes after a
    # restart. With the version stamp, each edit invalidates the cache.
    try:
        css_v = int(os.path.getmtime(os.path.join(app.root_path, 'static', 'css', 'style.css')))
    except OSError:
        css_v = 0
    try:
        js_v = int(os.path.getmtime(os.path.join(app.root_path, 'static', 'js', 'main.js')))
    except OSError:
        js_v = 0
    try:
        shared_js_v = int(os.path.getmtime(os.path.join(app.root_path, 'static', 'js', 'shared.js')))
    except OSError:
        shared_js_v = 0

    # Tools submenu: registry-driven, filtered to the tools checked in
    # Docs & Settings → Tools Menu, rendered in saved order.
    visible = get_tools_menu_visible(db, current_portal)
    _by_key = {t["key"]: t for t in TOOLS_REGISTRY}
    tools_menu = []
    for k in visible:
        t = _by_key.get(k)
        if t and current_portal in t["portals"]:
            tools_menu.append({
                "title": t["title"],
                "url": t["url"][current_portal],
                "active": _tool_is_active(t, endpoint),
            })
    tools_group_active = (endpoint == "tools_home"
                          or any(_tool_is_active(t, endpoint) for t in TOOLS_REGISTRY))

    return dict(
        pending_count=pending,
        current_endpoint=endpoint,
        current_portal=current_portal,
        tools_menu=tools_menu,
        tools_group_active=tools_group_active,
        css_v=css_v,
        js_v=js_v,
        shared_js_v=shared_js_v,
    )

# ─── Route modules ────────────────────────────────────────────────────────────
# No blueprints — each module's register(app, helpers) binds its views under
# their original endpoint names, so url_for(...) and base.html endpoint
# checks behave consistently.
import routes_lists
import routes_review
import routes_trx
import routes_receipts
import routes_investments
import routes_settings
import routes_tools
import routes_export
import routes_ccrecon
import routes_taxwiz
import routes_payroll
from routes_settings import (get_shortcuts, get_tools_menu_visible,
                             _tool_is_active, get_fx_rate)

_HELPERS = {"login_required": login_required, "amt": amt}
routes_lists.register(app, _HELPERS)
routes_review.register(app, _HELPERS)
routes_trx.register(app, _HELPERS)
routes_receipts.register(app, _HELPERS)
routes_investments.register(app, _HELPERS)
routes_settings.register(app, _HELPERS)
routes_tools.register(app, _HELPERS)
routes_export.register(app, _HELPERS)
routes_ccrecon.register(app, _HELPERS)
routes_taxwiz.register(app, _HELPERS)
routes_payroll.register(app, _HELPERS)


# ─── Version fingerprint / health (so a stale or zombie process is obvious) ──
import sys as _sys, time as _time, hashlib as _hashlib
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STARTED_AT = _time.strftime("%Y-%m-%d %H:%M:%S")


def _code_version():
    """A cheap fingerprint of the running code: mtimes of key source files +
    the git HEAD short hash. If two processes report different versions (or one
    is stale), this shows it immediately — the zombie-process failure mode.
    Both parts degrade gracefully (no git checkout → empty git hash)."""
    try:
        parts = []
        for fn in ("app.py", "routes_review.py", "routes_ccrecon.py",
                   "importers/chase_checking.py", "importers/chase_cc.py"):
            parts.append(str(int(os.path.getmtime(os.path.join(_BASE_DIR, fn)))))
        code = _hashlib.md5("|".join(parts).encode()).hexdigest()[:8]
    except Exception:
        code = "unknown"
    git = ""
    try:
        head = open(os.path.join(_BASE_DIR, ".git", "HEAD")).read().strip()
        if head.startswith("ref:"):
            git = open(os.path.join(_BASE_DIR, ".git", head.split(" ", 1)[1].strip())).read().strip()[:8]
        else:
            git = head[:8]
    except Exception:
        pass
    return {"code": code, "git": git}


@app.route("/health")
def _health():
    return jsonify({"version": _code_version(), "started_at": _STARTED_AT,
                    "python": _sys.version.split()[0], "pid": os.getpid()})


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _v = _code_version()
    print(f"[pft] STARTING — code {_v['code']} · git {_v['git']} · "
          f"python {_sys.version.split()[0]} · pid {os.getpid()} · {_STARTED_AT}",
          flush=True)
    init_db()
    for _d in (IMPORTS_CHECKING_DIR, IMPORTS_CC_DIR, IMPORTS_OTHER_DIR):
        os.makedirs(_d, exist_ok=True)
    # debug=False: the Werkzeug debugger is an arbitrary-code-execution
    # surface on any unhandled exception.
    app.run(host="127.0.0.1", port=5005, debug=False, use_reloader=False)
