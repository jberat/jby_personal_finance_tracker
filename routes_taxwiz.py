"""
routes_taxwiz.py — Tax Estimator walkthrough wizard (Tools → Tax Estimator).

Entirely stateless: no DB reads or writes, no session state. The wizard page
keeps all state in client-side JS; the calc endpoint is a pure JSON→JSON
wrapper around tax_engine_simple.estimate().

register(app) binds views under their function names (no blueprints), same
convention as the other routes_* modules. Accepts the optional `helpers`
dict the app passes to every module; only login_required is used.
"""
from flask import request, render_template, jsonify

from tax_engine_simple import (estimate, STATE_RULES, TAX_YEAR,
                               UNSUPPORTED_STATE_MESSAGE)


# All US states + DC for the dropdown, grouped by support level client-side.
US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"),
    ("AR", "Arkansas"), ("CA", "California"), ("CO", "Colorado"),
    ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"),
    ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"),
    ("MN", "Minnesota"), ("MS", "Mississippi"), ("MO", "Missouri"),
    ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"),
    ("NY", "New York"), ("NC", "North Carolina"), ("ND", "North Dakota"),
    ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
    ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"),
    ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
    ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
]


def _state_groups():
    """Group the dropdown by support tier, driven entirely by the engine's
    STATE_RULES (single source of truth — no hand-maintained second list).
    Returns [(group_label, [(code, name, supported), ...]), ...]."""
    none_g, flat_g, unsupported_g = [], [], []
    for code, name in US_STATES:
        rule = STATE_RULES.get(code)
        if rule is None:
            unsupported_g.append((code, name, False))
        elif rule["type"] == "none":
            none_g.append((code, name, True))
        else:
            flat_g.append((code, name, True))
    return [("No income tax", none_g),
            ("Flat rate — supported", flat_g),
            ("Not yet supported (federal only)", unsupported_g)]


def _state_info():
    """Per-state support tier for client-side JS ({code: 'none'|'flat'});
    absent code = unsupported. Serialized straight from STATE_RULES."""
    return {code: rule["type"] for code, rule in STATE_RULES.items()}


def register(app, helpers=None):
    login_required = (helpers or {}).get("login_required", lambda f: f)

    def tools_tax_estimator():
        return render_template("tools/tax_estimator.html",
                               state_groups=_state_groups(),
                               state_info=_state_info(),
                               unsupported_state_message=UNSUPPORTED_STATE_MESSAGE,
                               tax_year=TAX_YEAR)

    def api_tax_estimator_calc():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        result = estimate(payload)
        return jsonify(result), (200 if result.get("ok") else 400)

    tools_tax_estimator = login_required(tools_tax_estimator)
    api_tax_estimator_calc = login_required(api_tax_estimator_calc)

    app.add_url_rule("/tools/tax-estimator", "tools_tax_estimator",
                     tools_tax_estimator, methods=["GET"])
    app.add_url_rule("/api/tax-estimator/calc", "api_tax_estimator_calc",
                     api_tax_estimator_calc, methods=["POST"])
