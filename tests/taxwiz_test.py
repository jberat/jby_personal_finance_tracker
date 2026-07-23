"""
taxwiz_test.py — hand-computed assertions for tax_engine_simple (2026).

Uses the engine directly (no HTTP, no DB). Every expected number below was
computed by hand from the 2026 constants; run after any engine change.

Usage (from the app folder):
    python3 tests/taxwiz_test.py
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tax_engine_simple import (estimate, bracket_breakdown,  # noqa: E402
                               EFFECTIVE_GROSS_BASIS, EFFECTIVE_AGI_BASIS)

PASS = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  ✓ {label}")


def approx(a, b, tol=0.02):
    assert abs(a - b) <= tol, f"expected {b}, got {a}"


def main():
    # ── 1. Single, W-2 only, Colorado ────────────────────────────────────
    # w2 100,000 → taxable 100,000 − 16,100 = 83,900
    # fed: 12,400×10% + 38,000×12% + 33,500×22% = 1,240 + 4,560 + 7,370 = 13,170
    # CO: 4.40% × 83,900 = 3,691.60
    r = estimate({"filing_status": "single", "state": "CO", "w2_wages": 100_000})
    assert r["ok"]
    approx(r["income"]["taxable"], 83_900.00)
    approx(r["federal"]["income_tax"], 13_170.00)
    approx(r["federal"]["total"], 13_170.00)          # no SE/addl-Medicare/NIIT
    assert r["state_result"]["supported"] and r["state_result"]["type"] == "flat"
    approx(r["state_result"]["tax"], 3_691.60)
    assert abs(r["rates"]["federal_marginal"] - 0.22) < 1e-9
    approx(r["rates"]["effective"], (13_170.00 + 3_691.60) / 100_000, tol=0.0002)
    ok("single W-2-only CO: fed 13,170.00 / CO 3,691.60 / 22% marginal")

    # ── 2. MFJ with LTCG stacking crossing the 0%→15% threshold ─────────
    # w2 60,000 + LTCG 80,000 → AGI 140,000; taxable 140,000−32,200 = 107,800
    # ordinary slice 27,800: 24,800×10% + 3,000×12% = 2,840
    # cap gains stack 27,800→107,800 vs thresholds (98,900 / 613,700):
    #   0%: 98,900−27,800 = 71,100 · 15%: 107,800−98,900 = 8,900 → 1,335 · 20%: 0
    # federal income tax = 2,840 + 1,335 = 4,175
    r = estimate({"filing_status": "mfj", "state": "TX",
                  "w2_wages": 60_000, "long_term_gains": 80_000})
    assert r["ok"]
    approx(r["income"]["taxable"], 107_800.00)
    approx(r["federal"]["ordinary_tax"], 2_840.00)
    approx(r["federal"]["capgains_at_0"], 71_100.00)
    approx(r["federal"]["capgains_at_15"], 8_900.00)
    approx(r["federal"]["capgains_at_20"], 0.00)
    approx(r["federal"]["capgains_tax"], 1_335.00)
    approx(r["federal"]["income_tax"], 4_175.00)
    ok("MFJ LTCG stacking crosses 0/15 threshold: 71,100 @ 0%, 8,900 @ 15%")

    # ── 3. SE-only single: SE tax + half-SE deduction feed AGI ──────────
    # se 50,000 → net earnings 50,000×0.9235 = 46,175
    # SE tax: 12.4%×46,175 = 5,725.70 + 2.9%×46,175 = 1,339.075 → 7,064.775
    # half deduction 3,532.3875 → AGI 46,467.6125 → taxable 30,367.6125
    # fed: 1,240 + (30,367.6125−12,400)×12% = 3,396.1135
    r = estimate({"filing_status": "single", "state": "TX", "se_income": 50_000})
    assert r["ok"]
    approx(r["federal"]["se_tax"], 7_064.78)
    approx(r["federal"]["half_se_deduction"], 3_532.39)
    approx(r["income"]["agi"], 46_467.61)
    approx(r["income"]["taxable"], 30_367.61)
    approx(r["federal"]["income_tax"], 3_396.11)
    approx(r["federal"]["total"], 3_396.11 + 7_064.78)
    ok("SE-only: SE tax 7,064.78, half-SE deduction 3,532.39 flows to AGI")

    # ── 4. SE + W-2 near the SS wage base (cap respected) ────────────────
    # w2 180,000 leaves 184,500−180,000 = 4,500 of SS room
    # SE SS: 12.4%×4,500 = 558; SE Medicare: 2.9%×46,175 = 1,339.075
    # addl Medicare: 0.9%×(180,000+46,175−200,000) = 0.9%×26,175 = 235.575
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 180_000, "se_income": 50_000})
    assert r["ok"]
    approx(r["federal"]["se_ss"], 558.00)
    approx(r["federal"]["se_medicare"], 1_339.08)
    approx(r["federal"]["se_tax"], 1_897.08)
    approx(r["federal"]["additional_medicare"], 235.58)
    ok("SS wage base cap + additional Medicare 0.9% over 200K")

    # ── 5. No-tax state: TX → state tax exactly 0, still 'supported' ─────
    r = estimate({"filing_status": "single", "state": "TX", "w2_wages": 90_000})
    assert r["ok"]
    assert r["state_result"]["supported"] is True
    assert r["state_result"]["type"] == "none"
    assert r["state_result"]["tax"] == 0.0
    approx(r["totals"]["total_tax"], r["federal"]["total"])
    ok("no-tax state: TX state tax = 0, total = federal only")

    # ── 6. Unsupported state: CA → federal still computes ────────────────
    r = estimate({"filing_status": "single", "state": "CA", "w2_wages": 90_000})
    assert r["ok"]
    assert r["state_result"]["supported"] is False
    assert r["state_result"]["tax"] is None
    assert "state" in r["state_result"]["message"].lower()
    assert r["federal"]["total"] > 0
    assert r["payments"]["remaining_state"] is None
    ok("unsupported state (CA): friendly message, federal unaffected")

    # ── 7. Payments producing a refund + quarterly = 0 ───────────────────
    # Case 1 again (fed 13,170 / CO 3,691.60) with 16,000 fed + 4,000 state
    # withheld → refund 2,830 + 308.40 = 3,138.40
    r = estimate({"filing_status": "single", "state": "CO",
                  "w2_wages": 100_000,
                  "federal_withheld": 16_000, "state_withheld": 4_000})
    assert r["ok"]
    approx(r["payments"]["remaining_federal"], -2_830.00)
    approx(r["payments"]["remaining_state"], -308.40)
    approx(r["payments"]["remaining_total"], -3_138.40)
    approx(r["payments"]["refund"], 3_138.40)
    assert r["payments"]["quarterly_suggestion"] == 0.0
    ok("payments case: projected refund 3,138.40, no quarterlies suggested")

    # ── 8. Owing → simple quarterly split of the remainder ───────────────
    r = estimate({"filing_status": "single", "state": "CO",
                  "w2_wages": 100_000, "federal_withheld": 5_000})
    owed = 13_170.00 - 5_000 + 3_691.60
    approx(r["payments"]["remaining_total"], owed)
    approx(r["payments"]["quarterly_suggestion"], owed / 4)
    ok("payments case: remainder split into 4 quarterly payments")

    # ── 9. S-corp owner: distributions produce NO SE tax + note present ──
    # w2 80,000 salary + 40,000 distributions → AGI 120,000 (no half-SE
    # deduction); taxable 120,000 − 16,100 = 103,900
    # fed: 12,400×10% + 38,000×12% + 53,500×22% = 1,240+4,560+11,770 = 17,570
    r = estimate({"filing_status": "single", "state": "TX",
                  "se_structure": "s_corp",
                  "w2_wages": 80_000, "scorp_distributions": 40_000})
    assert r["ok"]
    assert r["se_structure"] == "s_corp"
    approx(r["federal"]["se_tax"], 0.00)
    approx(r["federal"]["half_se_deduction"], 0.00)
    approx(r["income"]["agi"], 120_000.00)
    approx(r["income"]["taxable"], 103_900.00)
    approx(r["federal"]["income_tax"], 17_570.00)
    assert any("S-CORP" in n for n in r["notes"]), \
        "results notes must state the S-corp treatment"
    assert not any("SOLE PROPRIETOR" in n for n in r["notes"])
    assert any("S-corp owner distributions" in s["label"]
               for s in r["agi_walk"]), \
        "AGI walk must show the distributions line"
    ok("S-corp owner: 40,000 distributions → NO SE tax; treatment note present")

    # ── 10. Sole-prop default: math unchanged from case 3 + note present ─
    r = estimate({"filing_status": "single", "state": "TX",
                  "se_income": 50_000})
    assert r["se_structure"] == "sole_prop"
    approx(r["federal"]["se_tax"], 7_064.78)         # identical to case 3
    approx(r["federal"]["half_se_deduction"], 3_532.39)
    approx(r["income"]["agi"], 46_467.61)
    assert any("SOLE PROPRIETOR" in n for n in r["notes"]), \
        "results notes must state the sole-prop treatment"
    assert not any("S-CORP" in n for n in r["notes"])
    assert not any("S-corp owner distributions" in s["label"]
                   for s in r["agi_walk"]), \
        "no distributions line for a pure sole-prop run"
    ok("sole-prop (default): SE math unchanged; treatment note present")

    # ── 11. Bracket breakdown — hand-computed single filer ───────────────
    # Same facts as case 1: single, w2 100,000 → ordinary taxable 83,900.
    # 2026 single brackets fill as:
    #   0–12,400 @10%   → 12,400 in, 1,240.00 tax, cum 1,240.00
    #   12,400–50,400 @12% → 38,000 in, 4,560.00 tax, cum 5,800.00
    #   50,400–105,700 @22% → 33,500 in, 7,370.00 tax, cum 13,170.00 ← marginal
    #   24/32/35/37% brackets → 0 in, cum stays 13,170.00
    # headroom to 24%: 105,700 − 83,900 = 21,800
    bb = bracket_breakdown("single", 83_900)
    assert bb["filing_status"] == "single"
    assert len(bb["rows"]) == 7
    expected = [  # (lower, upper, rate, income_in, tax_in, cum_tax, current)
        (0,       12_400,  0.10, 12_400, 1_240.00, 1_240.00, False),
        (12_400,  50_400,  0.12, 38_000, 4_560.00, 5_800.00, False),
        (50_400,  105_700, 0.22, 33_500, 7_370.00, 13_170.00, True),
        (105_700, 201_775, 0.24, 0,      0.00,     13_170.00, False),
        (201_775, 256_225, 0.32, 0,      0.00,     13_170.00, False),
        (256_225, 640_600, 0.35, 0,      0.00,     13_170.00, False),
        (640_600, None,    0.37, 0,      0.00,     13_170.00, False),
    ]
    for row, (lo, up, rate, inc, tax, cum, cur) in zip(bb["rows"], expected):
        assert row["lower"] == lo and row["upper"] == up
        assert abs(row["rate"] - rate) < 1e-9
        approx(row["income_in"], inc)
        approx(row["tax_in"], tax)
        approx(row["cumulative_tax"], cum)
        assert row["is_current"] is cur, (row, cur)
    approx(bb["total_tax"], 13_170.00)
    assert abs(bb["current_rate"] - 0.22) < 1e-9
    assert abs(bb["next_rate"] - 0.24) < 1e-9
    approx(bb["headroom"], 21_800.00)
    # Rows must tie to the engine's federal ordinary tax, and the calc
    # payload must carry the identical breakdown for the UI.
    r = estimate({"filing_status": "single", "state": "CO",
                  "w2_wages": 100_000})
    approx(bb["total_tax"], r["federal"]["ordinary_tax"])
    assert r["bracket_breakdown"] == bb
    ok("bracket breakdown: single 83,900 — per-bracket fill/tax, running "
       "total, 22% marginal, 21,800 headroom, ties to engine ordinary tax")

    # ── 12. Bracket breakdown excludes preferential income ───────────────
    # single, w2 60,000 + LTCG 20,000 → taxable 63,900, preferential 20,000,
    # ordinary 43,900 — the breakdown must walk 43,900, not 63,900.
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 60_000, "long_term_gains": 20_000})
    assert r["ok"]
    approx(r["income"]["ordinary_taxable"], 43_900.00)
    approx(r["bracket_breakdown"]["ordinary_taxable"], 43_900.00)
    approx(r["bracket_breakdown"]["total_tax"], r["federal"]["ordinary_tax"])
    assert r["bracket_breakdown"]["current_rate"] == 0.12
    ok("bracket breakdown: preferential income (LTCG) stays out of the table")

    # ── 13. Effective rate — explicit basis on BOTH bases ────────────────
    # SE case 3 again: gross 50,000; AGI 46,467.61; total tax 10,460.89
    # (fed income 3,396.11 + SE 7,064.78, TX → no state).
    # effective_gross = 10,460.89 / 50,000     = 0.2092
    # effective_agi   = 10,460.89 / 46,467.61  = 0.2251
    r = estimate({"filing_status": "single", "state": "TX",
                  "se_income": 50_000})
    assert r["ok"]
    approx(r["rates"]["effective_gross"], 0.2092, tol=0.0002)
    approx(r["rates"]["effective_agi"], 0.2251, tol=0.0002)
    assert r["rates"]["effective_gross_basis"] == EFFECTIVE_GROSS_BASIS \
        == "total tax ÷ gross income"
    assert r["rates"]["effective_agi_basis"] == EFFECTIVE_AGI_BASIS \
        == "total tax ÷ AGI"
    # Legacy alias keeps its historical gross basis, now labeled.
    assert r["rates"]["effective"] == r["rates"]["effective_gross"]
    assert r["rates"]["effective_basis"] == EFFECTIVE_GROSS_BASIS
    ok("effective rate: gross + AGI bases both computed, basis strings "
       "travel in the payload")

    # ── 14. SE loss: excluded from AGI AND the exclusion is disclosed ────
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 70_000, "se_income": -20_000})
    assert r["ok"]
    approx(r["federal"]["se_tax"], 0.00)
    approx(r["income"]["agi"], 70_000.00)      # loss excluded, not netted
    assert any("Self-employment LOSSES" in n for n in r["notes"]), \
        "SE-loss exclusion must be disclosed in notes"
    ok("SE loss: no SE tax, excluded from AGI, disclosure note present")

    # ── 15. Explicit $0 withholding must NOT fall back to the W-2 field ──
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 100_000,
                  "w2_federal_withholding": 12_000, "federal_withheld": 0})
    assert r["ok"]
    approx(r["payments"]["federal_paid"], 0.00)
    # …but a MISSING/blank dedicated field still falls back:
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 100_000, "w2_federal_withholding": 12_000})
    approx(r["payments"]["federal_paid"], 12_000.00)
    r = estimate({"filing_status": "single", "state": "TX",
                  "w2_wages": 100_000, "w2_federal_withholding": 12_000,
                  "federal_withheld": ""})
    approx(r["payments"]["federal_paid"], 12_000.00)
    ok("withholding: explicit 0 respected; blank still falls back to W-2 Box 2")

    # ── 16. Itemizing below the standard deduction triggers a warning ────
    r = estimate({"filing_status": "mfj", "state": "TX", "w2_wages": 120_000,
                  "use_standard": False, "itemized_total": 30_000})
    assert r["ok"]
    approx(r["income"]["deduction"], 30_000.00)   # choice still honored
    assert any("LESS" in n and "standard deduction" in n for n in r["notes"]), \
        "itemized-below-standard warning must be in notes"
    r = estimate({"filing_status": "mfj", "state": "TX", "w2_wages": 120_000,
                  "use_standard": False, "itemized_total": 40_000})
    assert not any("LESS" in n and "standard deduction" in n for n in r["notes"])
    ok("deductions: itemized < standard warns (and larger itemized doesn't)")

    print(f"\nTAXWIZ TESTS PASSED — {PASS} checks green")


if __name__ == "__main__":
    main()
