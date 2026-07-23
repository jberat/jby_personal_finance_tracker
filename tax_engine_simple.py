"""
tax_engine_simple.py — pure-function 2026 personal tax estimator for the
Tax Estimator walkthrough wizard (Tools → Tax Estimator).

2026 TAX YEAR; VERIFY RATES BEFORE RELYING ON RESULTS; THIS IS NOT TAX ADVICE.
Estimates only — no AMT, no capital-loss netting, no phase-outs beyond what's
noted inline, state math is an approximation. A CPA (or at least real tax
software) is ground truth.

Federal constants: brackets/standard deduction/SS wage base/additional
Medicare cross-checked against published 2026 IRS figures (IRS Rev. Proc.
2025-32 post-OBBBA inflation adjustments; ssa.gov wage base; IRS Topic 751).
MFJ/MFS/HOH brackets and the capital-gains thresholds are the standard
published 2026 figures.

Everything here is pure functions on plain dicts — no Flask, no DB, no I/O.
Entry point: estimate(inputs) -> result dict.
"""

TAX_YEAR = 2026  # tax year every constant below belongs to


# ─── STATE RULES (plainly editable) ──────────────────────────────────────────
# Each entry is one of:
#   {"type": "none", "note": ...}                     — no state income tax
#   {"type": "flat", "rate", "base", "approximation_note"}  — flat-rate state
# "base" says what the flat rate is applied to in THIS estimator (an
# approximation flag, not a statement of state law):
#   "federal_taxable" — federal taxable income (AGI − deduction)
#   "federal_agi"     — federal AGI
# Any state code not present here → "unsupported" (graduated brackets etc.);
# federal still computes. Rates are the most recent figures confidently known
# as of mid-2026 — several states have scheduled cuts, see each note.
STATE_RULES = {
    # ── No state income tax ─────────────────────────────────────────────
    "AK": {"type": "none", "note": "Alaska has no state income tax."},
    "FL": {"type": "none", "note": "Florida has no state income tax."},
    "NV": {"type": "none", "note": "Nevada has no state income tax."},
    "SD": {"type": "none", "note": "South Dakota has no state income tax."},
    "TN": {"type": "none", "note": "Tennessee has no state income tax."},
    "TX": {"type": "none", "note": "Texas has no state income tax."},
    "WA": {"type": "none", "note": "Washington has no state income tax on "
           "wages. (WA does levy a 7% excise tax on large long-term capital "
           "gains — not modeled here.)"},
    "WY": {"type": "none", "note": "Wyoming has no state income tax."},
    "NH": {"type": "none", "note": "New Hampshire taxes neither wages nor — "
           "since its Interest & Dividends tax was repealed effective 2025 — "
           "interest/dividend income."},

    # ── Flat-rate states ────────────────────────────────────────────────
    "CO": {"type": "flat", "rate": 0.0440, "base": "federal_taxable",
           "approximation_note": "Colorado: 4.40% of federal taxable income "
           "(CO actually starts from federal taxable income, so this is "
           "close). CO-specific add-backs/subtractions (e.g. 529, TABOR "
           "refund-year rate reductions) not modeled."},
    "IL": {"type": "flat", "rate": 0.0495, "base": "federal_agi",
           "approximation_note": "Illinois: 4.95% of federal AGI. IL's real "
           "base is federal AGI with state modifications and a per-person "
           "exemption allowance — not modeled, so this slightly overstates."},
    "IN": {"type": "flat", "rate": 0.0295, "base": "federal_agi",
           "approximation_note": "Indiana: 2.95% (2026 rate, verified) "
           "of federal AGI. Indiana COUNTY "
           "income taxes (often 1–3%) are NOT included; state exemptions "
           "not modeled."},
    "MI": {"type": "flat", "rate": 0.0425, "base": "federal_agi",
           "approximation_note": "Michigan: 4.25% of federal AGI. Personal "
           "exemptions (~$5,800/person) and city income taxes (e.g. "
           "Detroit) not modeled."},
    "PA": {"type": "flat", "rate": 0.0307, "base": "federal_agi",
           "approximation_note": "Pennsylvania: 3.07% of federal AGI as a "
           "proxy. PA actually taxes gross income classes with NO standard "
           "deduction and doesn't allow federal above-the-line deductions; "
           "local wage taxes (often ~1%+) not included."},
    "UT": {"type": "flat", "rate": 0.0450, "base": "federal_agi",
           "approximation_note": "Utah: 4.50% (2025 rate — verify) of "
           "federal AGI. Utah applies its rate to state taxable income and "
           "then grants a taxpayer credit that partially substitutes for "
           "the standard deduction — not modeled, so this overstates for "
           "most filers."},
    "AZ": {"type": "flat", "rate": 0.0250, "base": "federal_taxable",
           "approximation_note": "Arizona: 2.5% of federal taxable income. "
           "AZ starts from federal AGI and allows a standard deduction "
           "matching the federal one, so federal taxable is a close proxy."},
    "ID": {"type": "flat", "rate": 0.0530, "base": "federal_taxable",
           "approximation_note": "Idaho: 5.30% (2026 rate, verified) "
           "of federal taxable income "
           "(Idaho conforms to federal taxable income)."},
    "KY": {"type": "flat", "rate": 0.0350, "base": "federal_agi",
           "approximation_note": "Kentucky: 3.50% (2026 rate, verified) "
           "of federal AGI. KY's own small "
           "standard deduction (~$3,300) not modeled."},
    "MS": {"type": "flat", "rate": 0.0400, "base": "federal_agi",
           "approximation_note": "Mississippi: 4.00% (2026 rate, verified) "
           "of federal AGI. MS exemptions "
           "and its own standard deduction not modeled, so this overstates."},
    "NC": {"type": "flat", "rate": 0.0399, "base": "federal_agi",
           "approximation_note": "North Carolina: 3.99% (2026 rate, "
           "verified) of federal AGI. NC's own "
           "standard deduction (~$12,750 single / $25,500 MFJ) not modeled, "
           "so this overstates."},
    "GA": {"type": "flat", "rate": 0.0519, "base": "federal_taxable",
           "approximation_note": "Georgia: 5.19% (2025 rate; further "
           "scheduled cuts — verify) of federal taxable income as a proxy. "
           "GA starts from federal AGI with its own standard deduction "
           "similar in size to the federal one."},
}

# Friendly message for states not in STATE_RULES (graduated brackets, etc.)
UNSUPPORTED_STATE_MESSAGE = (
    "This state has graduated brackets (or rules we haven't modeled yet), so "
    "the estimator doesn't compute state tax for it. Your FEDERAL estimate "
    "below is still valid — check your state's tax tables separately.")


# ─── Federal constants — 2026 tax year ───────────────────────────────────────
# Ordinary-income brackets: list of (upper_bound_of_bracket, rate), walked in
# order; None = no cap (top bracket). Rev. Proc. 2025-32.
FED_BRACKETS = {
    "single": [  # single filers
        (12_400,  0.10), (50_400,  0.12), (105_700, 0.22), (201_775, 0.24),
        (256_225, 0.32), (640_600, 0.35), (None, 0.37),
    ],
    "mfj": [     # married filing jointly (and qualifying surviving spouse)
        (24_800,  0.10), (100_800, 0.12), (211_400, 0.22), (403_550, 0.24),
        (512_450, 0.32), (768_700, 0.35), (None, 0.37),
    ],
    "mfs": [     # married filing separately (35% bracket caps lower)
        (12_400,  0.10), (50_400,  0.12), (105_700, 0.22), (201_775, 0.24),
        (256_225, 0.32), (384_350, 0.35), (None, 0.37),
    ],
    "hoh": [     # head of household
        (17_700,  0.10), (67_450,  0.12), (105_700, 0.22), (201_775, 0.24),
        (256_225, 0.32), (640_600, 0.35), (None, 0.37),
    ],
}

# Standard deduction by filing status, 2026 (Rev. Proc. 2025-32).
STD_DEDUCTION = {
    "single": 16_100,   # single
    "mfj":    32_200,   # married filing jointly
    "mfs":    16_100,   # married filing separately
    "hoh":    24_150,   # head of household
}

# Long-term capital gains / qualified dividends stacking thresholds, 2026:
# (top of the 0% band, top of the 15% band) of TAXABLE income; 20% above.
# Standard published 2026 figures.
LTCG_THRESHOLDS = {
    "single": (49_450, 545_500),
    "mfj":    (98_900, 613_700),
    "mfs":    (49_450, 306_850),
    "hoh":    (66_200, 579_600),
}

SS_WAGE_BASE = 184_500        # 2026 Social Security wage base (ssa.gov)

# Self-employment tax (Schedule SE):
SE_NET_EARNINGS_FACTOR = 0.9235  # net SE profit × 92.35% = net earnings from SE
SE_SS_RATE = 0.124               # 12.4% Social Security portion (both halves)
SE_MEDICARE_RATE = 0.029         # 2.9% Medicare portion (both halves, no cap)

# Additional Medicare tax, 0.9% on wages + SE earnings over the threshold
# (thresholds are statutory, NOT inflation-indexed — IRS Topic 560).
ADDL_MEDICARE_RATE = 0.009
ADDL_MEDICARE_THRESHOLD = {
    "single": 200_000, "mfj": 250_000, "mfs": 125_000, "hoh": 200_000,
}

# Net Investment Income Tax, 3.8% on the lesser of net investment income or
# MAGI over the threshold (statutory, NOT inflation-indexed — IRC §1411).
NIIT_RATE = 0.038
NIIT_THRESHOLD = {
    "single": 200_000, "mfj": 250_000, "mfs": 125_000, "hoh": 200_000,
}

FILING_STATUSES = ("single", "mfj", "mfs", "hoh")

# Standing caveat surfaced in every result — the UI must show it.
PAYROLL_PRETAX_NOTE = (
    "401(k) deferrals and payroll health premiums are usually ALREADY "
    "excluded from W-2 Box 1 wages. Do not enter them again as deductions — "
    "that double-counts. The IRA/HSA fields here are for contributions made "
    "OUTSIDE payroll only.")


# ─── Bracket math helpers ────────────────────────────────────────────────────

def _bracket_tax(brackets, taxable):
    """Walk (upper, rate) brackets over `taxable`; returns tax."""
    taxable = max(0.0, taxable)
    tax, lower = 0.0, 0.0
    for upper, rate in brackets:
        if upper is None or taxable <= upper:
            tax += (taxable - lower) * rate
            break
        tax += (upper - lower) * rate
        lower = upper
    return tax


def _bracket_rate(brackets, taxable):
    """Marginal rate at `taxable` (bottom rate at/below zero)."""
    taxable = max(0.0, taxable)
    for upper, rate in brackets:
        if upper is None or taxable <= upper:
            return rate
    return brackets[-1][1]


# ── Effective-rate basis labels ──────────────────────────────────────────────
# The engine result CARRIES these strings so the UI renders exactly what the
# math did — the label can never drift from the divisor. Both bases are shown
# ("Effective on gross" and "Effective on AGI").
EFFECTIVE_GROSS_BASIS = "total tax ÷ gross income"
EFFECTIVE_AGI_BASIS = "total tax ÷ AGI"


def bracket_breakdown(filing_status, ordinary_taxable):
    """Per-bracket breakout of ORDINARY taxable income for the results UI.

    Covers every filing status and includes a running-total
    column. Pure/additive — walks the same
    FED_BRACKETS[filing_status] as _bracket_tax(), so the rows always sum
    to the engine's federal ordinary tax.

    Feed it ORDINARY taxable income only: preferential income (long-term
    gains + qualified dividends) sits OUTSIDE these brackets — it stacks on
    top of ordinary income and is taxed at its own 0/15/20% rates.

    Returns {"filing_status", "ordinary_taxable", "rows", "total_tax",
             "current_rate", "next_rate", "headroom"}.
    rows: {lower, upper (None = top bracket), rate, income_in, tax_in,
           cumulative_tax, is_current}.
    headroom = additional ordinary taxable income before the next bracket
    starts (None in the top bracket)."""
    if filing_status not in FED_BRACKETS:
        raise ValueError(f"filing_status must be one of {FILING_STATUSES}")
    taxable = max(0.0, float(ordinary_taxable or 0.0))
    brackets = FED_BRACKETS[filing_status]
    rows, lower, cum = [], 0.0, 0.0
    current_rate, next_rate, headroom = None, None, None
    for i, (upper, rate) in enumerate(brackets):
        span_top = upper if upper is not None else max(taxable, lower)
        income_in = max(0.0, min(taxable, span_top) - lower)
        cum += income_in * rate
        is_current = (taxable > lower) and (upper is None or taxable <= upper)
        rows.append({"lower": round(lower, 2), "upper": upper, "rate": rate,
                     "income_in": round(income_in, 2),
                     "tax_in": round(income_in * rate, 2),
                     "cumulative_tax": round(cum, 2),
                     "is_current": is_current})
        if is_current:
            current_rate = rate
            if upper is not None:
                headroom = round(upper - taxable, 2)
                next_rate = brackets[i + 1][1]
        lower = upper if upper is not None else lower
    return {"filing_status": filing_status,
            "ordinary_taxable": round(taxable, 2),
            "rows": rows,
            "total_tax": round(cum, 2),
            "current_rate": current_rate,
            "next_rate": next_rate,
            "headroom": headroom}


def _num(inputs, key, default=0.0):
    """Pull a numeric field, tolerating missing/blank/None."""
    v = inputs.get(key, default)
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# ─── Main entry point ────────────────────────────────────────────────────────

def estimate(inputs):
    """Estimate 2026 federal + (approximate) state tax from a plain dict.

    Recognized keys (all optional except filing_status; numbers default 0):
      filing_status  'single' | 'mfj' | 'mfs' | 'hoh'
      state          2-letter code (state math per STATE_RULES; else unsupported)
      w2_wages, w2_federal_withholding, w2_state_withholding
      pretax_401k_already_excluded  (bool, default True — flag only; when
          True the standing note reminds that payroll 401k/health premiums
          are already out of Box 1 and must not be re-entered)
      se_structure   'sole_prop' (default) | 's_corp' — how self-employment
          is structured. Sole proprietor / single-member LLC: se_income gets
          SE tax (92.35% factor) + the half-SE-tax above-the-line deduction.
          S-corp owner: salary belongs in the W-2 fields (FICA already
          withheld) and owner distributions go in scorp_distributions —
          ordinary income with NO SE tax. The applied treatment is stated
          in notes[].
      scorp_distributions (S-corp owner distributions — taxed as ordinary
          income, never SE-taxed; only meaningful with se_structure='s_corp')
      se_income (net 1099/self-employment profit), interest,
      ordinary_dividends (1099-DIV box 1a TOTAL, includes qualified),
      qualified_dividends (box 1b subset), short_term_gains, long_term_gains,
      other_income
      use_standard (bool, default True), itemized_total, traditional_ira, hsa
      credits_total (simple non-refundable credits vs federal income tax)
      federal_withheld, state_withheld (fall back to the w2_* withholding
          fields if not given), estimated_payments_fed, estimated_payments_state

    Returns a dict: federal/state/SE breakdowns, AGI walk, rates, payments
    applied → remaining owed or refund + quarterly split, notes[].
    """
    notes = [
        f"{TAX_YEAR} tax year rates. Verify before relying on results; "
        "this is an estimate, not tax advice.",
        PAYROLL_PRETAX_NOTE,
    ]

    filing_status = str(inputs.get("filing_status", "single")).lower()
    if filing_status not in FILING_STATUSES:
        return {"ok": False,
                "error": f"filing_status must be one of {FILING_STATUSES}"}
    state = str(inputs.get("state", "")).strip().upper()

    se_structure = (str(inputs.get("se_structure", "sole_prop")).strip().lower()
                    or "sole_prop")
    if se_structure not in ("sole_prop", "s_corp"):
        se_structure = "sole_prop"

    # ── Input validation ─────────────────────────────────────────────────
    # Garbage must be a clean error, never a silent $0: "5o000" quietly
    # becoming zero wages would produce a wildly wrong estimate with no
    # hint anything was dropped. Blank/missing still defaults to 0 (the
    # wizard sends "" for untouched fields). Negative amounts are rejected
    # for fields where a negative is meaningless; loss fields (SE income,
    # capital gains, other income) stay signed.
    _NONNEG_KEYS = ("w2_wages", "scorp_distributions", "interest",
                    "ordinary_dividends", "qualified_dividends",
                    "traditional_ira", "hsa", "itemized_total",
                    "credits_total", "federal_withheld", "state_withheld",
                    "w2_federal_withholding", "w2_state_withholding",
                    "estimated_payments_fed", "estimated_payments_state")
    _SIGNED_KEYS = ("se_income", "short_term_gains", "long_term_gains",
                    "other_income")
    bad = []
    for key in _NONNEG_KEYS + _SIGNED_KEYS:
        raw = inputs.get(key)
        if raw is None or raw == "":
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            bad.append(f"{key} must be a number (got {raw!r})")
            continue
        if key in _NONNEG_KEYS and v < 0:
            bad.append(f"{key} can't be negative")
    if bad:
        return {"ok": False, "error": "; ".join(bad)}

    # ── Income items (negatives floored where the math needs it) ─────────
    w2_wages = max(0.0, _num(inputs, "w2_wages"))
    se_income = _num(inputs, "se_income")       # net SE profit; loss → no SE tax
    scorp_distributions = max(0.0, _num(inputs, "scorp_distributions"))
    interest = max(0.0, _num(inputs, "interest"))
    ordinary_div = max(0.0, _num(inputs, "ordinary_dividends"))
    qualified_div = max(0.0, _num(inputs, "qualified_dividends"))
    if qualified_div > ordinary_div:
        # Box 1b is a subset of box 1a; assume the user gave only 1b.
        ordinary_div = qualified_div
        notes.append("Qualified dividends exceeded ordinary (total) "
                     "dividends; treated total dividends as the qualified "
                     "amount.")
    st_gains = _num(inputs, "short_term_gains")
    lt_gains = _num(inputs, "long_term_gains")
    if st_gains < 0 or lt_gains < 0:
        notes.append("Capital LOSSES aren't modeled (no netting or $3,000 "
                     "limit); negative gains were floored at $0.")
        st_gains = max(0.0, st_gains)
        lt_gains = max(0.0, lt_gains)
    other_income = _num(inputs, "other_income")

    # ── Self-employment tax (Schedule SE) ────────────────────────────────
    # Net earnings = SE profit × 92.35%. SS portion respects the wage base,
    # and W-2 wages use up wage-base room first.
    se_net_earnings = max(0.0, se_income) * SE_NET_EARNINGS_FACTOR
    ss_room = max(0.0, SS_WAGE_BASE - w2_wages)
    se_ss = SE_SS_RATE * min(se_net_earnings, ss_room)
    se_medicare = SE_MEDICARE_RATE * se_net_earnings
    se_tax = se_ss + se_medicare
    half_se_deduction = se_tax / 2.0   # above-the-line, feeds back into AGI

    # ── SE treatment notes — say exactly which treatment was applied ─────
    if se_income > 0:
        notes.append(
            "Self-employment treated as SOLE PROPRIETOR / single-member LLC: "
            "SE tax was applied to 92.35% of your net profit, and half the "
            "SE tax was deducted above the line automatically. Both are "
            "1040-level items, not business expenses — your Schedule C net "
            "profit should NOT already have them subtracted.")
    elif se_income < 0:
        notes.append(
            "Self-employment LOSSES aren't modeled: your negative SE amount "
            "was EXCLUDED entirely (no SE tax, and no reduction of your "
            "other income). A real return would net the loss against other "
            "income, so your actual tax is likely LOWER than shown.")
    if se_structure == "s_corp":
        notes.append(
            "S-CORP OWNER treatment: owner distributions "
            f"(${scorp_distributions:,.2f}) were included as ordinary income "
            "with NO self-employment tax — S-corp distributions aren't "
            "subject to SE tax. Your salary (and its FICA withholding) flows "
            "through the W-2 wage fields instead.")

    # ── AGI walk ─────────────────────────────────────────────────────────
    se_income_for_agi = se_income if se_income > 0 else 0.0
    total_income = (w2_wages + se_income_for_agi + scorp_distributions
                    + interest + ordinary_div
                    + st_gains + lt_gains + other_income)
    traditional_ira = max(0.0, _num(inputs, "traditional_ira"))
    hsa = max(0.0, _num(inputs, "hsa"))
    above_the_line = half_se_deduction + traditional_ira + hsa
    agi = total_income - above_the_line

    use_standard = bool(inputs.get("use_standard", True))
    std_deduction = STD_DEDUCTION[filing_status]
    itemized_total = max(0.0, _num(inputs, "itemized_total"))
    deduction = std_deduction if use_standard else itemized_total
    if not use_standard and itemized_total < std_deduction:
        notes.append(
            f"Heads up: your itemized total (${itemized_total:,.2f}) is LESS "
            f"than the {TAX_YEAR} standard deduction for your filing status "
            f"(${std_deduction:,.2f}). The estimate honors your itemized "
            "choice as entered, but taking the standard deduction would "
            "lower this tax.")
    taxable = max(0.0, agi - deduction)

    # ── Federal ordinary tax + LTCG/qualified-dividend stacking ─────────
    # Preferential income (qualified dividends + LTCG) sits ON TOP of
    # ordinary income: ordinary brackets run on taxable − preferential, then
    # the preferential slice is taxed 0/15/20% by where it lands in taxable.
    preferential = min(taxable, qualified_div + lt_gains)
    ordinary_taxable = taxable - preferential
    brackets = FED_BRACKETS[filing_status]
    ordinary_tax = _bracket_tax(brackets, ordinary_taxable)

    thr0, thr15 = LTCG_THRESHOLDS[filing_status]
    cg_at_0 = max(0.0, min(taxable, thr0) - ordinary_taxable)
    cg_at_15 = max(0.0, min(taxable, thr15) - max(ordinary_taxable, thr0))
    cg_at_20 = max(0.0, taxable - max(ordinary_taxable, thr15))
    capgains_tax = cg_at_15 * 0.15 + cg_at_20 * 0.20

    fed_income_tax_before_credits = ordinary_tax + capgains_tax
    credits_total = max(0.0, _num(inputs, "credits_total"))
    fed_income_tax = max(0.0, fed_income_tax_before_credits - credits_total)

    # ── Additional Medicare 0.9% (wages + SE earnings over threshold) ────
    addl_medicare = ADDL_MEDICARE_RATE * max(
        0.0, (w2_wages + se_net_earnings) - ADDL_MEDICARE_THRESHOLD[filing_status])

    # ── NIIT 3.8% (simplified: MAGI ≈ AGI; NII = investment income here) ─
    nii = interest + ordinary_div + st_gains + lt_gains
    niit = NIIT_RATE * max(0.0, min(nii, agi - NIIT_THRESHOLD[filing_status])) \
        if agi > NIIT_THRESHOLD[filing_status] and nii > 0 else 0.0
    if niit > 0:
        notes.append("NIIT (3.8% net investment income tax) applied using "
                     "MAGI ≈ AGI and gross investment income (no investment "
                     "expenses netted) — a simplification.")

    federal_total = fed_income_tax + se_tax + addl_medicare + niit

    # ── State ────────────────────────────────────────────────────────────
    rule = STATE_RULES.get(state)
    if rule is None:
        state_result = {"supported": False, "type": "unsupported",
                        "tax": None, "message": UNSUPPORTED_STATE_MESSAGE,
                        "approximation_note": None}
    elif rule["type"] == "none":
        state_result = {"supported": True, "type": "none", "tax": 0.0,
                        "message": rule["note"],
                        "approximation_note": rule["note"]}
    else:  # flat
        base_amount = taxable if rule["base"] == "federal_taxable" else agi
        state_result = {"supported": True, "type": "flat",
                        "rate": rule["rate"], "base": rule["base"],
                        "tax": rule["rate"] * max(0.0, base_amount),
                        "message": None,
                        "approximation_note": rule["approximation_note"]}
        notes.append(rule["approximation_note"])
    state_tax = state_result["tax"] if state_result["tax"] is not None else 0.0

    # ── Rates ────────────────────────────────────────────────────────────
    # Marginal = federal bracket rate on the next dollar of ORDINARY income
    # (ignores stack-shift effects on preferential income — approximation).
    fed_marginal = _bracket_rate(brackets, ordinary_taxable) \
        if taxable > 0 else 0.0
    gross_income = total_income
    total_tax = federal_total + state_tax
    # Effective rate on BOTH bases ("Effective on gross" + "Effective on
    # AGI"). Each value travels with its basis string — see
    # EFFECTIVE_*_BASIS above.
    effective_gross = (total_tax / gross_income) if gross_income > 0 else 0.0
    effective_agi = (total_tax / agi) if agi > 0 else 0.0

    # ── Payments → remaining owed / refund ───────────────────────────────
    # Fall back to the W-2 withholding fields only when the dedicated field
    # was NOT GIVEN (missing/blank). An explicit 0 means 0 — a user zeroing
    # the payments box must not get their W-2 withholding silently re-applied.
    def _withheld(primary, fallback):
        raw = inputs.get(primary)
        if raw is None or raw == "":
            return _num(inputs, fallback)
        return _num(inputs, primary)
    fed_withheld = _withheld("federal_withheld", "w2_federal_withholding")
    state_withheld = _withheld("state_withheld", "w2_state_withholding")
    est_fed = _num(inputs, "estimated_payments_fed")
    est_state = _num(inputs, "estimated_payments_state")
    fed_paid = max(0.0, fed_withheld) + max(0.0, est_fed)
    state_paid = max(0.0, state_withheld) + max(0.0, est_state)

    remaining_federal = federal_total - fed_paid
    remaining_state = (state_tax - state_paid) if state_result["supported"] \
        else None
    remaining_total = remaining_federal + (remaining_state or 0.0)
    # Simple split of what's still owed across the 4 estimated-payment dates.
    quarterly_suggestion = remaining_total / 4.0 if remaining_total > 0 else 0.0

    r2 = lambda v: (round(v, 2) if isinstance(v, float) else v)
    agi_walk = [
        {"label": "W-2 wages (Box 1)", "amount": w2_wages},
        {"label": "Self-employment net income", "amount": se_income_for_agi},
    ]
    if se_structure == "s_corp" or scorp_distributions > 0:
        agi_walk.append({"label": "S-corp owner distributions (no SE tax)",
                         "amount": scorp_distributions})
    agi_walk += [
        {"label": "Interest", "amount": interest},
        {"label": "Ordinary dividends (incl. qualified)", "amount": ordinary_div},
        {"label": "Short-term capital gains", "amount": st_gains},
        {"label": "Long-term capital gains", "amount": lt_gains},
        {"label": "Other income", "amount": other_income},
        {"label": "Total income", "amount": total_income, "rule": True},
        {"label": "− ½ self-employment tax", "amount": -half_se_deduction},
        {"label": "− Traditional IRA", "amount": -traditional_ira},
        {"label": "− HSA (outside payroll)", "amount": -hsa},
        {"label": "AGI", "amount": agi, "rule": True},
        {"label": ("− Standard deduction" if use_standard
                   else "− Itemized deductions"), "amount": -deduction},
        {"label": "Taxable income", "amount": taxable, "rule": True},
    ]
    for step in agi_walk:
        step["amount"] = r2(step["amount"])

    return {
        "ok": True,
        "tax_year": TAX_YEAR,
        "filing_status": filing_status,
        "state": state,
        "se_structure": se_structure,
        "agi_walk": agi_walk,
        "income": {
            "total_income": r2(total_income), "agi": r2(agi),
            "deduction": r2(deduction), "deduction_type":
                ("standard" if use_standard else "itemized"),
            "taxable": r2(taxable),
            "ordinary_taxable": r2(ordinary_taxable),
            "preferential": r2(preferential),
        },
        "federal": {
            "ordinary_tax": r2(ordinary_tax),
            "capgains_tax": r2(capgains_tax),
            "capgains_at_0": r2(cg_at_0), "capgains_at_15": r2(cg_at_15),
            "capgains_at_20": r2(cg_at_20),
            "income_tax_before_credits": r2(fed_income_tax_before_credits),
            "credits": r2(credits_total),
            "income_tax": r2(fed_income_tax),
            "se_tax": r2(se_tax), "se_ss": r2(se_ss),
            "se_medicare": r2(se_medicare),
            "half_se_deduction": r2(half_se_deduction),
            "additional_medicare": r2(addl_medicare),
            "niit": r2(niit),
            "total": r2(federal_total),
        },
        "state_result": {k: r2(v) if isinstance(v, float) else v
                         for k, v in state_result.items()},
        "rates": {
            "federal_marginal": fed_marginal,
            "effective_gross": round(effective_gross, 4),
            "effective_gross_basis": EFFECTIVE_GROSS_BASIS,
            "effective_agi": round(effective_agi, 4),
            "effective_agi_basis": EFFECTIVE_AGI_BASIS,
            # Legacy alias — same gross basis the field always had.
            "effective": round(effective_gross, 4),
            "effective_basis": EFFECTIVE_GROSS_BASIS,
        },
        # Per-bracket breakout of ordinary income (preferential income
        # stacks on top at 0/15/20% and is NOT in these rows).
        "bracket_breakdown": bracket_breakdown(filing_status,
                                               ordinary_taxable),
        "totals": {"total_tax": r2(total_tax)},
        "payments": {
            "federal_paid": r2(fed_paid), "state_paid": r2(state_paid),
            "remaining_federal": r2(remaining_federal),
            "remaining_state": (r2(remaining_state)
                                if remaining_state is not None else None),
            "remaining_total": r2(remaining_total),
            "refund": r2(-remaining_total) if remaining_total < 0 else 0.0,
            "quarterly_suggestion": r2(quarterly_suggestion),
        },
        "notes": notes,
    }
