"""
html_snapshot.py — zero-behavior-change proof harness for refactors.

Renders every page against a frozen copy of the live DB and saves normalized
HTML. After a refactor phase, re-run in diff mode: identical output proves
nothing changed (numbers included — they're embedded in the HTML).

Usage (from the app folder):
    python3 tests/html_snapshot.py baseline   # snapshot current code
    python3 tests/html_snapshot.py diff       # compare current code vs baseline

The DB copy is made once at `baseline` and reused by `diff`, so data can't
drift between the two runs. Work dir: /tmp/pft_snapshots/
"""
import sys
import os
import re
import shutil
import difflib
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = "/tmp/pft_snapshots"
DB_COPY = os.path.join(WORK, "frozen.db")
BASELINE_DIR = os.path.join(WORK, "baseline")
CURRENT_DIR = os.path.join(WORK, "current")

# Every meaningful GET page. Add new pages here as they ship.
PAGES = [
    ("dashboard",            "/"),
    ("review",               "/review"),
    ("expenses_overview",    "/expenses/overview"),
    ("expenses_trx",         "/expenses/transactions"),
    ("expenses_vendor",      "/expenses/by-vendor"),
    ("income_overview",      "/income/overview"),
    ("income_trx",           "/income/transactions"),
    ("import",               "/tools/import"),
    ("import_csv",           "/import"),
    ("import_manual",        "/import/manual"),
    ("investments_overview", "/investments/overview"),
    ("investments_trx",      "/investments/transactions"),
    ("trash",                "/trash"),
    ("receipts_home",        "/tools/receipts"),
    ("receipts_review",      "/receipts/review"),
    ("receipts_orphans",     "/receipts/orphans"),
    ("receipts_trash",       "/receipts/trash"),
    ("cleanup",              "/tools/cleanup"),
    ("tools_home",           "/tools"),
    ("actuals_budget",       "/tools/actuals-vs-budget"),
    ("settings_overview",    "/settings/overview"),
    ("settings_cat",         "/settings/categorization"),
    ("settings_assumptions", "/settings/assumptions"),
    ("settings_budget",      "/settings/assumptions/budget-values"),
    ("settings_fx",          "/settings/assumptions/exchange-rates"),
    ("settings_shortcuts",   "/settings/shortcuts"),
    ("settings_toolsmenu",   "/settings/tools-menu"),
    ("settings_security",    "/settings/security"),
]

# Normalizations for legitimate nondeterminism (NOT data):
_NORMALIZERS = [
    (re.compile(r"\?v=\d+"), "?v=X"),                      # css/js cache-bust mtimes
]


def _normalize(html: str) -> str:
    for rx, sub in _NORMALIZERS:
        html = rx.sub(sub, html)
    # collapse trailing whitespace per line (editor/format noise only)
    return "\n".join(line.rstrip() for line in html.splitlines())


def _load_app(db_path):
    spec = importlib.util.spec_from_file_location(
        "appmod", os.path.join(ROOT, "app.py"))
    appmod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(appmod)
    # Point at the frozen copy — works whether DB_PATH lives on appmod or
    # (post-refactor) on a config module it re-exports.
    appmod.DB_PATH = db_path
    if hasattr(appmod, "config") and hasattr(appmod.config, "DB_PATH"):
        appmod.config.DB_PATH = db_path
    app = appmod.app
    app.config["TESTING"] = True
    return app


def render_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    app = _load_app(DB_COPY)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    failures = []
    for name, url in PAGES:
        r = c.get(url, follow_redirects=True)
        if r.status_code != 200:
            failures.append((name, url, r.status_code))
            continue
        with open(os.path.join(out_dir, name + ".html"), "w") as f:
            f.write(_normalize(r.data.decode("utf-8", "replace")))
    return failures


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "baseline":
        os.makedirs(WORK, exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "finance.db"), DB_COPY)
        shutil.rmtree(BASELINE_DIR, ignore_errors=True)
        failures = render_all(BASELINE_DIR)
        n = len(os.listdir(BASELINE_DIR))
        print(f"baseline: {n} pages snapshotted → {BASELINE_DIR}")
        for f in failures:
            print("  RENDER FAIL:", f)
        sys.exit(1 if failures else 0)

    elif mode == "diff":
        if not os.path.isdir(BASELINE_DIR):
            print("No baseline — run `baseline` first.")
            sys.exit(2)
        shutil.rmtree(CURRENT_DIR, ignore_errors=True)
        failures = render_all(CURRENT_DIR)
        changed = []
        for name in sorted(os.listdir(BASELINE_DIR)):
            b = open(os.path.join(BASELINE_DIR, name)).read()
            cur_path = os.path.join(CURRENT_DIR, name)
            if not os.path.exists(cur_path):
                changed.append((name, "MISSING IN CURRENT"))
                continue
            cu = open(cur_path).read()
            if b != cu:
                diff = list(difflib.unified_diff(
                    b.splitlines(), cu.splitlines(), lineterm=""))[:40]
                changed.append((name, "\n".join(diff)))
        if failures:
            print("RENDER FAILURES:")
            for f in failures:
                print(" ", f)
        if changed:
            print(f"CHANGED PAGES: {len(changed)}")
            for name, d in changed:
                print(f"\n════ {name} ════")
                print(d[:3000])
            sys.exit(1)
        if not failures:
            print(f"IDENTICAL — all {len(os.listdir(BASELINE_DIR))} pages "
                  f"byte-equal to baseline. Zero behavior change proven.")
            sys.exit(0)
        sys.exit(1)

    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
