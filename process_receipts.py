"""
process_receipts.py — Deterministic receipts pipeline.

Replaces the agent-based dry-run that gave inconsistent answers between runs.
Walks the inbox, extracts (vendor, amount, date) from each file using
format-specific or generic extractors, matches to transactions, writes
results to the receipts table, and (when confidence is HIGH) files the
receipt into the year/L1/L2/MM hierarchy + sets the trx's primary
receipt_path.

Usage from the app folder:

    python3 process_receipts.py                # full process: extract+match+report
    python3 process_receipts.py --dry-run      # extract+match only, no DB writes
    python3 process_receipts.py --no-file      # match + write DB rows but don't move files
    python3 process_receipts.py --report       # print latest receipts table state, no scanning

External dependencies (Mac):
    brew install poppler    # for pdftotext (PDF text extraction)
    Apple Vision OCR (HEIC + image-only PDFs) via bin/apple_ocr.

This script is intentionally conservative: nothing auto-files by default.
Matches are written with status='queued' or 'orphan' and surfaced in the
/tools/receipts review UI for human confirmation.
"""
from __future__ import annotations  # PEP 563: lazy annotations so `X | None`
                                    # union syntax works on Python 3.9+ too.
import sys
import os
import re
import hashlib
import sqlite3
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# ── Paths ────────────────────────────────────────────────────────────────────
# All layout comes from config.py: the DB, the receipts inbox, and the
# single filed tree (receipts/filed/<YYYY>/<L1>/<L2>/<MM>/). to_mac_path()
# is an identity hook in this build (paths are stored as-is).
import config as _config
from config import (RECEIPTS_ROOT, RECEIPTS_INBOX, RECEIPTS_OWNER_FOLDER,
                    _owner_to_receipts_folder, to_mac_path, OWNER)

HERE       = Path(__file__).parent
DB_PATH    = Path(_config.DB_PATH)
INBOX_DIR  = Path(RECEIPTS_INBOX)
FILED_ROOT = Path(RECEIPTS_ROOT)

# Names that must never win the vendor-keyword scan: payment rails (add
# your own name here too — you're the customer on every emailed receipt,
# so your name appearing in the text should never make it the "vendor").
VENDOR_SCAN_BLOCKLIST = {"zelle", "venmo", "paypal"}

DRY_RUN      = "--dry-run"      in sys.argv
NO_FILE      = "--no-file"      in sys.argv
REPORT       = "--report"       in sys.argv
SCRUB        = "--scrub"        in sys.argv  # walk every status='filed' receipt
                                             # and verify the file is at the
                                             # right place with the right name;
                                             # fix or orphan if not.
FILE_PENDING = "--file-pending" in sys.argv  # ONLY process status='linked' rows
                                             # (skips inbox scan). Useful right
                                             # after confirming a batch in the
                                             # portal to get them filed.
REPROCESS    = "--reprocess"    in sys.argv  # re-scan files even if their
                                             # content hash is already in the
                                             # receipts table.
# Review-by-default (2026-06-29): NOTHING auto-files. Every match — HIGH and
# MED alike — routes to 'queued' so it lands in the portal review for an
# explicit confirm before anything is filed. Pass --autofile to opt back into
# the old behavior (auto-file HIGH-confidence matches). --no-autofile is kept
# as a harmless explicit alias for the default.
AUTOFILE     = "--autofile"     in sys.argv  # opt-in: auto-file HIGH matches
NO_AUTOFILE  = "--no-autofile"  in sys.argv  # explicit form of the default
DO_AUTOFILE  = AUTOFILE and not NO_AUTOFILE  # effective auto-file switch

# ── Logging ──────────────────────────────────────────────────────────────────
def log(msg, level="info"):
    prefix = {"info": "  ", "ok": "✓ ", "warn": "⚠ ", "err": "✗ "}.get(level, "  ")
    print(f"{prefix}{msg}")


# ── File-type sniffing ───────────────────────────────────────────────────────
def sniff_mime(path: Path) -> str:
    """Best-effort mime detection from extension. Good enough for now —
    if edge cases show up (renamed files), upgrade to `file --mime-type`."""
    ext = path.suffix.lower()
    return {
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".heic": "image/heic",
        ".heif": "image/heic",
        ".webp": "image/png",   # PIL/tesseract path handles it like any raster
        ".tif":  "image/png",
        ".tiff": "image/png",
    }.get(ext, "application/octet-stream")


def content_hash(path: Path) -> str:
    """sha256 of file bytes — collision-proof dedup key."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── PDF text extraction ──────────────────────────────────────────────────────
def pdftotext(path: Path) -> str:
    """Extract selectable text from a PDF. Empty string if no text layer
    (image-only PDFs fall through to OCR via _ocr_pdf_pages)."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        log("pdftotext not installed — `brew install poppler`", "warn")
    except subprocess.TimeoutExpired:
        log(f"pdftotext timed out on {path.name}", "warn")
    return ""


# ── OCR (Apple Vision via Swift CLI primary, pyobjc fallback, Tesseract last) ─
# Three backends, tried in order:
#   1. Swift CLI at bin/apple_ocr (PREFERRED — same Apple Vision quality, no
#      Python binding pain). Build once: `cd bin && swiftc -O apple_ocr.swift
#      -o apple_ocr`. Universal binary, works on Intel + Apple Silicon.
#   2. pyobjc-framework-Vision (if installed and importable). Currently broken
#      on macOS + Xcode CLT + system Python 3.9 due to a pyobjc-core build
#      issue — recommend skipping in favor of the Swift CLI.
#   3. Tesseract via pytesseract (cross-platform fallback). Lower accuracy but
#      works without macOS-specific tooling. `brew install tesseract` +
#      `pip3 install pytesseract pillow`.

APPLE_OCR_BIN = HERE / "bin" / "apple_ocr"


def _ocr_apple_swift_cli(path: Path) -> str:
    """OCR via the Swift CLI binary (Apple Vision under the hood). Returns
    empty string if binary doesn't exist or fails. Doesn't raise."""
    if not APPLE_OCR_BIN.exists():
        return ""
    try:
        result = subprocess.run(
            [str(APPLE_OCR_BIN), str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        log(f"apple_ocr exit={result.returncode}: {result.stderr.strip()}", "warn")
        return ""
    except Exception as e:
        log(f"apple_ocr CLI failed on {path.name}: {e}", "warn")
        return ""


def _ocr_apple_vision(path: Path) -> str:
    """OCR via Apple Vision (macOS only). Returns empty string if pyobjc
    isn't installed or this isn't a Mac. Doesn't raise."""
    try:
        import Vision
        import Quartz
        from Foundation import NSURL
    except ImportError:
        return ""
    try:
        url = NSURL.fileURLWithPath_(str(path.resolve()))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if not src or Quartz.CGImageSourceGetCount(src) == 0:
            return ""
        cg_image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, None
        )
        success, _err = handler.performRequests_error_([request], None)
        if not success:
            return ""
        results = request.results() or []
        lines = []
        for obs in results:
            top = obs.topCandidates_(1)
            if top and len(top) > 0:
                lines.append(str(top[0].string()))
        return "\n".join(lines).strip()
    except Exception as e:
        log(f"Apple Vision OCR failed on {path.name}: {e}", "warn")
        return ""


def _ocr_tesseract(path: Path) -> str:
    """OCR via Tesseract (cross-platform). Returns empty string if not
    installed. Lower accuracy than Apple Vision but works without pyobjc.

    Always converts the image to RGB first — Tesseract chokes on iPhone
    "Live Photo" MPO format and various non-standard image modes (P, CMYK,
    1-bit, etc.). RGB normalization handles all of those uniformly."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        img = Image.open(path).convert("RGB")
        return pytesseract.image_to_string(img).strip()
    except Exception as e:
        log(f"Tesseract OCR failed on {path.name}: {e}", "warn")
        return ""


def ocr_image(path: Path) -> str:
    """Tiered OCR: Swift CLI → pyobjc → Tesseract. Each returns "" on
    failure, so the pipeline degrades gracefully (orphan, not crash)."""
    text = _ocr_apple_swift_cli(path)
    if text: return text
    text = _ocr_apple_vision(path)
    if text: return text
    return _ocr_tesseract(path)


# ── HEIC → JPG conversion (sips primary, pillow-heif fallback) ──────────────
# sips is macOS-only — on a Mac it's always available. Elsewhere (e.g. a
# Linux box) we fall back to pillow-heif (cross-platform Python lib).
# Either way the output is the same: a JPG sitting next to the HEIC.

def heic_to_jpg(heic_path: Path) -> Path:
    """Convert a HEIC file to JPG. Tries macOS `sips` first, then pillow-heif.
    Returns the JPG path. Raises if neither backend is available or both fail."""
    jpg_path = heic_path.with_suffix(".jpg")
    # Use a temp suffix if a same-named jpg already exists (avoid clobber)
    if jpg_path.exists():
        jpg_path = heic_path.with_name(heic_path.stem + ".converted.jpg")

    # Backend 1: macOS sips
    if shutil.which("sips"):
        try:
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", str(heic_path),
                 "--out", str(jpg_path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and jpg_path.exists():
                return jpg_path
        except Exception:
            pass  # fall through to pillow-heif

    # Backend 2: pillow-heif (cross-platform)
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
        register_heif_opener()
        img = Image.open(heic_path)
        img.convert("RGB").save(jpg_path, "JPEG", quality=85)
        return jpg_path
    except ImportError:
        raise RuntimeError(
            "HEIC conversion needs `sips` (macOS) or "
            "`pip install pillow-heif`. Neither is available."
        )
    except Exception as e:
        raise RuntimeError(f"pillow-heif HEIC conversion failed: {e}")


# ── Image-only PDF → page PNGs → OCR ────────────────────────────────────────
def _ocr_pdf_pages(pdf_path: Path) -> str:
    """Render PDF pages to PNG via pdftoppm, OCR each, concatenate.
    For image-only PDFs (scans) — text-layer PDFs are handled by pdftotext."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = Path(tmpdir) / "page"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", str(pdf_path), str(prefix)],
                capture_output=True, timeout=60, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log(f"pdftoppm failed on {pdf_path.name}: {e}", "warn")
            return ""
        pages = sorted(Path(tmpdir).glob("page-*.png"))
        return "\n".join(ocr_image(p) for p in pages).strip()


def extract_text(path: Path, mime: str) -> tuple[str, str]:
    """Get raw text from any supported file type.
    Returns (text, source) where source is 'pdftotext'|'pdf-ocr'|'image-ocr'|''."""
    if mime == "application/pdf":
        text = pdftotext(path)
        if text:
            return text, "pdftotext"
        # Image-only PDF — fall back to OCR
        return _ocr_pdf_pages(path), "pdf-ocr"
    if mime == "image/heic":
        try:
            jpg = heic_to_jpg(path)
        except Exception as e:
            log(f"HEIC conversion failed for {path.name}: {e}", "warn")
            return "", ""
        try:
            return ocr_image(jpg), "image-ocr"
        finally:
            try: jpg.unlink()
            except Exception: pass
    if mime in ("image/png", "image/jpeg"):
        return ocr_image(path), "image-ocr"
    return "", ""


# ── Generic regex extractor ──────────────────────────────────────────────────
DOLLAR_RE = re.compile(r"\$\s?([0-9]{1,6}(?:,[0-9]{3})*(?:\.[0-9]{2}))")
# Any money-looking number, currency symbol optional. Supports both US
# (1,234.56) and EU-style (1.234,56) formats — see _money_to_float().
AMT_ANY_RE = re.compile(r"(?:[$₺]|TL\s?|TRY\s?)?\s?"
                        r"([0-9]{1,6}(?:[.,][0-9]{3})*[.,][0-9]{2})\b")
# Lines that carry the receipt total (English + other supported locales).
TOTAL_RE = re.compile(r"\b(grand\s+total|total\s+due|amount\s+due|balance\s+due|"
                      r"amount\s+paid|amount\s+charged|charged|order\s+total|"
                      r"payment\s+amount|total|toplam|genel\s+toplam|tutar)\b", re.I)
# Lines that look like totals but aren't (the common misread causes:
# savings lines, tender/change, tips, subtotals, points).
ANTI_TOTAL_RE = re.compile(r"\b(sub[\s-]?total|ara\s+toplam|savings?|discount|"
                           r"tend(?:er)?e?d?|cash|change|tip|gratuity|"
                           r"points?|rewards?|total\s+items?|items?\s+sold|"
                           r"you\s+saved|top\s*kdv|kdv)\b", re.I)
DATE_RES  = [
    # 2026-04-17, 2026/04/17
    (re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"),
     lambda m: _validated_iso(m.group(1), m.group(2), m.group(3))),
    # 04/17/2026, 4/17/26 (and day-first 17/04/2026 when first field > 12)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b"),
     lambda m: _normalize_us_date(m.group(1), m.group(2), m.group(3))),
    # 17.04.2026 (EU dotted, day-first)
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"),
     lambda m: _validated_iso(m.group(3), m.group(2), m.group(1))),
    # April 17, 2026
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.I),
     lambda m: _month_name_to_iso(m.group(1), m.group(2), m.group(3))),
]
MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}


def _validated_iso(yyyy, mm, dd):
    """Build YYYY-MM-DD only if it's a real calendar date, else None.
    (2026-07-03 fix: '16/06/2026' EU-style used to normalize to the
    impossible '2026-16-06', which silently broke julianday() matching.)"""
    try:
        return datetime(int(yyyy), int(mm), int(dd)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _normalize_us_date(a, b, yy):
    yy = int(yy)
    if yy < 100:
        yy = 2000 + yy
    a, b = int(a), int(b)
    # US month-first by default; if the first field can't be a month,
    # treat as day-first (European receipt).
    if a > 12 and b <= 12:
        a, b = b, a
    return _validated_iso(yy, a, b)


def _month_name_to_iso(mon, dd, yyyy):
    m = MONTHS[mon[:3].lower()]
    return _validated_iso(yyyy, m, dd)


def _money_to_float(s: str):
    """Parse '1,234.56' (US) or '1.234,56' (TR/EU) — the RIGHTMOST separator
    is the decimal point."""
    s = s.strip()
    try:
        if "," in s and "." in s:
            if s.rindex(",") > s.rindex("."):        # 1.234,56 → TR/EU
                s = s.replace(".", "").replace(",", ".")
            else:                                     # 1,234.56 → US
                s = s.replace(",", "")
        elif "," in s:                                # 123,45 → decimal comma
            s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
        return float(s)
    except ValueError:
        return None


def detect_currency(text: str) -> str:
    """'TRY' / 'EUR' when a receipt is clearly foreign-denominated, else
    'USD'. EUR gets no matcher yet (no rate configured) — detecting it stops
    EUR amounts from being compared against USD charges as if 1:1
    (2026-07-03)."""
    t = text or ""
    if re.search(r"\$", t):
        return "USD"
    # (2026-07-04: OCR regularly mangles the ₺ glyph, so lean on
    # unambiguous TRY receipt vocabulary instead: TL suffix,
    # tax/receipt/thanks words, tax-office markers, card-payment labels.)
    if re.search(r"₺|\bTRY\b|\bTL\b|\bKDV\b|\bFİŞ\b|\bFIS\b|\bTOPLAM\b"
                 r"|\bTUTARI?\b|TEŞEKKÜR|TESEKKUR|\bVKN\b|\bV\.D\.?\b"
                 r"|KRED[İI] KARTI|\bNAK[İI]T\b", t, re.I):
        return "TRY"
    # TVA (FR) / MwSt (DE) — euro-zone VAT labels that survive OCR when the
    # € glyph doesn't. (IVA deliberately excluded: also Mexican peso VAT.)
    if re.search(r"€|\bEUR\b|\bTVA\b|\bMwSt\b", t, re.I):
        return "EUR"
    return "USD"


def extract_date(text: str, dayfirst: bool = False):
    """First plausible date in the document. dayfirst=True (day-first
    locales) tries DD[./-]MM[.//-]YYYY readings before US month-first —
    some international receipts use day-first dates, so '09/06/2026'
    (9 June) would otherwise land as Sept 6 (2026-07-03 fix). Also accepts
    dash-separated dates ('TARİH: 08-06-2026'), which the old patterns
    missed entirely."""
    from datetime import date as _date
    today_yr = _date.today().year
    patterns = list(DATE_RES)
    if dayfirst:
        patterns = [
            (re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](20\d{2})\b"),
             lambda m: _validated_iso(m.group(3), m.group(2), m.group(1))),
        ] + patterns
    else:
        patterns = patterns + [
            # dash-separated, month-first with day-first fallback
            (re.compile(r"\b(\d{1,2})-(\d{1,2})-(20\d{2})\b"),
             lambda m: _normalize_us_date(m.group(1), m.group(2), m.group(3))),
        ]
    for rx, conv in patterns:
        for m in rx.finditer(text):
            try:
                cand = conv(m)
                if cand and abs(int(cand[:4]) - today_yr) <= 2:
                    return cand
            except Exception:
                pass
    return None


def _paired_column_amount(lines, i):
    """OCR column-splitting fix: label line i has no amount. Collect the run
    of consecutive amount-less label lines starting BEFORE/AT i, then the run
    of amount-only lines that follows, and pair them by position — so in
    'TOPKDV / TOPLAM / *18,00 / *118,00', TOPLAM (2nd label) gets 118.00
    (2nd value), not the tax figure."""
    def is_label(ln):
        return bool((TOTAL_RE.search(ln) or ANTI_TOTAL_RE.search(ln))
                    and not AMT_ANY_RE.search(ln))
    # Walk back to the start of the label run
    start = i
    while start - 1 >= 0 and is_label(lines[start - 1]):
        start -= 1
    labels_end = i
    while labels_end + 1 < len(lines) and is_label(lines[labels_end + 1]):
        labels_end += 1
    my_pos = i - start
    # Values: consecutive lines after the label run that carry amounts
    values = []
    j = labels_end + 1
    while j < len(lines):
        s = lines[j].strip()
        if not s:
            j += 1
            continue
        m = AMT_ANY_RE.search(s)
        if not m or len(s) > 24:   # long line = not a bare value column
            break
        v = _money_to_float(m.group(1))
        if v is not None:
            values.append(v)
        j += 1
        if len(values) > labels_end - start + 1:
            break
    if my_pos < len(values):
        return values[my_pos]
    return values[-1] if values else None


def pick_amount(text: str):
    """Total-line amount heuristic (2026-07-03 — replaces bare max($), which
    grabbed savings/line-item figures instead of the true total).

    Tiers:
      1. Amount on a line whose label matches TOTAL_RE (and not ANTI_TOTAL_RE).
         The LAST such line wins — grand totals print at the bottom.
      2. Amount on the line AFTER a total-label line (Apple Vision often
         splits label and value onto adjacent lines).
      3. Arithmetic cross-check: an amount ≈ subtotal + tax gets promoted.
      4. Fallback: largest $-anchored amount (the old heuristic) at LOW
         confidence so it queues for review but never auto-files.

    Returns (amount, confidence) — (None, 0.0) when nothing parses."""
    if not text:
        return None, 0.0
    lines = text.splitlines()
    tier1, tier2 = [], []
    for i, ln in enumerate(lines):
        if not TOTAL_RE.search(ln) or ANTI_TOTAL_RE.search(ln):
            continue
        m = AMT_ANY_RE.search(ln)
        if m:
            v = _money_to_float(m.group(1))
            if v is not None and v > 0:
                tier1.append(v)
                continue
        # Label with no amount on its own line → OCR likely split columns.
        # (2026-07-03 fix: in 'TOPKDV / TOPLAM / *18,00 / *118,00', naive
        # "next line" pairing gave TOPLAM the TAX figure. Pair the label
        # run with the following value run POSITIONALLY instead.)
        v = _paired_column_amount(lines, i)
        if v is not None and v > 0:
            tier2.append(v)
    if tier1:
        return tier1[-1], 0.75
    if tier2:
        return tier2[-1], 0.65
    # Arithmetic cross-check: some amount == subtotal + tax (±2¢)
    dollar_amts = []
    for m in DOLLAR_RE.finditer(text):
        v = _money_to_float(m.group(1))
        if v is not None:
            dollar_amts.append(v)
    uniq = sorted(set(dollar_amts))
    for total in reversed(uniq):
        for sub in uniq:
            for tax in uniq:
                if sub < total and 0 < tax < sub and abs((sub + tax) - total) < 0.02:
                    return total, 0.6
    # Last resort: old behavior, flagged low-confidence
    if dollar_amts:
        return max(dollar_amts), 0.35
    return None, 0.0


def extract_generic(text: str, filename: str, db=None) -> dict:
    """Extract candidate (vendor, amount, date) from raw text. Confidence
    capped at 0.5 — these need human review unless they hit an exact
    transaction match.

    If `db` is provided, run a vendor-keyword scan as a fallback / override:
    look at all vendor names already in the transactions table, and if any
    of them appears anywhere in the receipt text, prefer that over the
    line-scan heuristic. This solves the common case where the receipt
    has the vendor name buried in body text (e.g., a Marriott PDF where
    the regex picks up 'ADDRESS' from the header, or a Best Buy PDF where
    it picks up '‹ See all orders' from a navigation chrome line)."""
    if not text:
        return {"vendor": None, "amount": None, "date": None,
                "order_id": None, "confidence": 0.0, "currency": "USD"}

    # Total-line heuristic (2026-07-03) — see pick_amount(). Also detect
    # foreign-currency receipts so the matcher can go FX-aware instead of
    # comparing ₺/€ figures against USD charges.
    currency = detect_currency(text)
    amount, amt_conf = pick_amount(text)

    # Date — day-first parsing for TRY receipts (see extract_date).
    date = extract_date(text, dayfirst=(currency == "TRY"))

    # Vendor: first non-empty line that's not just a date/amount/whitespace.
    vendor = None
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > 80:
            continue
        if DOLLAR_RE.search(s):
            continue
        if any(rx.search(s) for rx, _ in DATE_RES):
            continue
        # Skip pure-numeric / pure-symbol noise
        if re.match(r"^[\d\s\W]+$", s):
            continue
        # Skip time-of-day lines — Uber/Lyft email receipts start with
        # "9:24 PM" and it was being taken as the vendor (2026-07-03 fix).
        if re.match(r"^\d{1,2}:\d{2}(\s*[AP]M)?\b", s, re.I):
            continue
        vendor = s
        break

    # ── Vendor-keyword fallback ─────────────────────────────────────────────
    # Scan the full text for any vendor name that's already in the trxs
    # table. If found, override the line-scan vendor — DB matches are way
    # more useful for downstream matching than "ADDRESS" or stray nav text.
    # Sort longer-first so "Home Depot" beats "Depot".
    if db is not None:
        try:
            db_vendors = [r["vendor"] for r in db.execute(
                "SELECT DISTINCT vendor FROM transactions "
                "WHERE status='active' AND vendor IS NOT NULL "
                "  AND LENGTH(vendor) >= 4"
            ).fetchall()]
            db_vendors.sort(key=lambda v: -len(v))
            # (2026-07-03 fix) Never treat the account owner / payment-rail
            # names as the vendor — the owner's own name appears on most
            # email receipts (as the CUSTOMER) and was winning the vendor
            # scan, producing junk matches.
            db_vendors = [v for v in db_vendors
                          if v.lower() not in VENDOR_SCAN_BLOCKLIST]
            for v in db_vendors:
                # Word-boundary match (2026-07-03 fix: bare substring made
                # "Uber" match "Huber", etc.)
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(v.lower()) + r"(?![A-Za-z0-9])",
                             text.lower()):
                    vendor = v
                    break
        except Exception as e:
            log(f"vendor-keyword scan failed: {e}", "warn")

    return {
        "vendor": vendor,
        "amount": amount,
        "date":   date,
        "order_id": None,
        "confidence": (min(amt_conf, 0.75) if (amount and date) else 0.3),
        "currency": currency,
    }


# ── Per-vendor extractors ────────────────────────────────────────────────────
# Each takes (text, filename) and returns dict-or-None. Returns None if the
# file doesn't match this vendor's signature; caller falls through to the
# next extractor.

def extract_gusto(text: str, filename: str):
    """Gusto monthly invoice. Filename pattern '<YYMMDD> Gusto.pdf' is
    deterministic — we can pull date from the filename even if PDF text
    parsing is sketchy.

    2026-07-03 fixes: word-boundary trigger (bare substring fired on any
    restaurant named 'Gusto…'), and NO invented fallback amount — the old
    `amount = 55.00 @ 0.95 confidence` guess could auto-file a wrong match.
    If we can't parse an amount, fall through to the generic extractor."""
    if not re.search(r"\bgusto\b", (text or ""), re.I) \
            and not re.search(r"\bgusto\b", filename, re.I):
        return None
    # Date from filename (YYMMDD prefix)
    m = re.match(r"^(\d{6})\s+Gusto", filename, re.I)
    date = None
    if m:
        yymmdd = m.group(1)
        date = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    # Amount from text — Gusto's standard monthly fee is ~$55; parse, don't guess.
    amount = None
    if text:
        for m in DOLLAR_RE.finditer(text):
            try:
                v = float(m.group(1).replace(",", ""))
                if 40 <= v <= 200:  # plausible Gusto fee range
                    amount = v
                    break
            except ValueError:
                pass
    if amount is None:
        return None  # let the generic extractor (+ human review) handle it
    return {
        "vendor": "Gusto", "amount": amount, "date": date,
        "order_id": None, "confidence": 0.95,
    }


# Order matters: most specific first. Add per-vendor extractors for your
# own recurring receipts here (see docs/customize_with_ai.md).
EXTRACTORS = [
    extract_gusto,
]


# ── Match engine ─────────────────────────────────────────────────────────────
_STOP_TOKENS = {"the", "and", "inc", "llc", "corp", "com", "www", "store",
                "shop", "market", "of", "co", "tst", "sq", "pos", "web"}


def _vendor_token_set(s: str) -> set:
    """Meaningful tokens of a vendor string: words ≥4 chars (minus stop
    words/digits) PLUS adjacent-pair joins — so 'WAL*MART' yields
    {'mart', 'walmart'} and matches the clean vendor 'Walmart'."""
    parts = [t for t in re.split(r"[^a-z0-9]+", (s or "").lower())
             if t and not t.isdigit()]
    toks = {t for t in parts if len(t) >= 4 and t not in _STOP_TOKENS}
    for i in range(len(parts) - 1):
        if parts[i] in _STOP_TOKENS or parts[i + 1] in _STOP_TOKENS:
            continue
        joined = parts[i] + parts[i + 1]
        if len(joined) >= 6:
            toks.add(joined)
    return toks


def _vendor_tokens_match(a: str, b: str) -> bool:
    """True when two vendor strings share a meaningful word. Token-based,
    so OCR junk, '*' prefixes, and store numbers don't block the match."""
    if not a or not b:
        return False
    return bool(_vendor_token_set(a) & _vendor_token_set(b))


def find_matches(db, vendor: str, amount: float, date: str):
    """Return ranked list of candidate transactions for a receipt.

    Confidence levels:
      HIGH    — amount exact + date within ±3 days + vendor fuzzy match
      MEDIUM  — exactly 2 of 3 hit
      LOW     — only 1 hit
      NONE    — no candidates
    """
    if amount is None:
        return []  # nothing to match on

    # Tolerance scales with amount (2026-07-03 fix: a flat ±$5 was far too
    # loose on tiny FX charges and too tight for tip-adjusted restaurant
    # totals). Amount compares against ABS(t.amount) so refunds / credits
    # (stored negative) can match — the old signed compare made a $10
    # refund receipt miss its own -$10 trx by $20.
    tol = max(0.50, round(0.03 * amount, 2))
    # Restaurant tip window (2026-07-03): the bank charge includes the tip,
    # the printed receipt often shows the pre-tip total. Pull candidates up
    # to +30% ABOVE the receipt amount too; scoring below only accepts them
    # as tip-adjusted matches when vendor AND date also agree, capped MEDIUM.
    fetch_tol_hi = max(tol, round(amount * 0.30, 2))

    # Cast a wide net — within tolerance AND within ±30 days of date if known
    if date:
        rows = db.execute("""
            SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
                   t.owner, t.l1_category, t.l2_category, t.receipt_path,
                   ABS(ABS(t.amount) - ?) AS amt_diff,
                   ABS(julianday(t.trx_date) - julianday(?)) AS date_diff
              FROM transactions t
             WHERE (t.status='active'
                    OR (t.status='deleted' AND COALESCE(t.is_split,0)=1))
               AND (ABS(ABS(t.amount) - ?) < ?
                    OR (ABS(t.amount) >= ? AND ABS(t.amount) <= ? + ?))
               AND (julianday(t.trx_date) BETWEEN julianday(?, '-30 day')
                                              AND julianday(?, '+30 day'))
             ORDER BY amt_diff ASC, date_diff ASC
             LIMIT 10
        """, (amount, date, amount, tol, amount, amount, fetch_tol_hi,
              date, date)).fetchall()
        # (2026-07-03 fix) A bad/missing-window date used to return zero
        # candidates even on a perfect amount. Degrade to the no-date query
        # instead of orphaning.
        if not rows:
            date = None
    if not date:
        rows = db.execute("""
            SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
                   t.owner, t.l1_category, t.l2_category, t.receipt_path,
                   ABS(ABS(t.amount) - ?) AS amt_diff, NULL AS date_diff
              FROM transactions t
             WHERE (t.status='active'
                    OR (t.status='deleted' AND COALESCE(t.is_split,0)=1))
               AND ABS(ABS(t.amount) - ?) < ?
             ORDER BY amt_diff ASC
             LIMIT 10
        """, (amount, amount, tol)).fetchall()

    # Score each candidate: amount_match (0/1), date_match (0/1), vendor_match (0/1)
    scored = []
    vendor_lc = (vendor or "").lower()
    for r in rows:
        amt_match  = abs(abs(r["amount"]) - amount) < 0.01
        date_match = (r["date_diff"] is not None and r["date_diff"] <= 3.0)
        vendor_match = False
        if vendor_lc:
            r_v  = (r["vendor"] or "").lower()
            r_rd = (r["raw_description"] or "").lower()
            # (2026-07-03 fix: the old expression's operator precedence made
            # the whole chain conditional on r_v being non-empty — which
            # disabled the raw_description check exactly when vendor was
            # NULL, the case that needs it most.)
            if r_v and (vendor_lc in r_v or r_v in vendor_lc):
                vendor_match = True
            elif vendor_lc in r_rd:
                vendor_match = True
            elif _vendor_tokens_match(vendor_lc, r_v) \
                    or _vendor_tokens_match(vendor_lc, r_rd):
                # Token overlap (2026-07-03): survives OCR noise + processor
                # prefixes — "WAL*MART #2043" ↔ "Walmart", "TST* Blue
                # Sushi Den" ↔ "Blue Sushi", etc.
                vendor_match = True

        # Tip-adjusted amount (2026-07-03): trx amount within (receipt,
        # receipt+30%] — accepted only with vendor AND date agreement,
        # capped at MEDIUM so it always queues for review.
        tip_match = (not amt_match
                     and vendor_match and date_match
                     and amount < abs(r["amount"]) <= round(amount * 1.30, 2))

        hits = sum([amt_match, date_match, vendor_match])
        if   hits == 3:  conf = "HIGH"
        elif tip_match:  conf = "MEDIUM"
        elif hits == 2:  conf = "MEDIUM"
        elif hits == 1:  conf = "LOW"
        else:            conf = "NONE"

        scored.append({
            "trx_id": r["id"], "trx_date": r["trx_date"],
            "trx_vendor": r["vendor"], "trx_amount": r["amount"],
            "trx_owner": r["owner"], "trx_l1": r["l1_category"],
            "trx_l2": r["l2_category"], "trx_has_receipt": bool(r["receipt_path"]),
            "amt_match": amt_match, "date_match": date_match,
            "vendor_match": vendor_match, "tip_match": tip_match,
            "confidence": conf, "score": 2 if (tip_match and hits < 2) else hits,
            "amt_diff": r["amt_diff"], "date_diff": r["date_diff"],
        })
    scored.sort(key=lambda x: (-x["score"], x["amt_diff"]))

    # ── Uniqueness promotion ────────────────────────────────────────────────
    # If the top candidate has amount AND date both exact, AND it's the ONLY
    # candidate that hits both exactly within the search window, then this
    # match is functionally unique — vendor confirmation is redundant.
    # Promote MED → HIGH. Catches the common case of email-forwarded receipt
    # screenshots where OCR pulls "Forwarded message" or "Receipt" as the
    # vendor instead of Apple / Spotify / etc., even though amount + date
    # nail it.
    if scored and scored[0]["amt_match"] and scored[0]["date_match"]:
        unique = sum(1 for s in scored if s["amt_match"] and s["date_match"])
        # (2026-07-03: require the promoted match to be within 1 day, not 3 —
        # a same-amount recurring charge a few days out shouldn't promote.)
        if unique == 1 and scored[0]["confidence"] != "HIGH" \
                and (scored[0]["date_diff"] is not None and scored[0]["date_diff"] <= 1.0):
            scored[0]["confidence"] = "HIGH"
            scored[0]["score"] = 3

    return scored


# FX receipts: the local amount can't tie to the USD charge, so match by
# date proximity + an exchange-rate band instead. Rates live in the portal
# (Docs & Settings → Assumptions → Exchange Rates → app_settings); matcher
# searches ±10% around each. Falls back to a wide static band if unset.
# Matches cap at MEDIUM → always queued for review.
FX_CONFIG = {
    "TRY": {"key": "fx_try_per_usd", "fallback": (35.0, 60.0), "symbol": "₺"},
    "EUR": {"key": "fx_eur_per_usd", "fallback": (0.80, 1.05), "symbol": "€"},
}
TRY_USD_RANGE = FX_CONFIG["TRY"]["fallback"]   # legacy alias
FX_BAND_PCT   = 0.10                           # ±10% around the configured rate


def _fx_rate(db, key):
    """Read a configured per-USD rate from app_settings, or None."""
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return float(row["value"]) if row and row["value"] else None
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None  # table doesn't exist yet / bad value → fallback band


def _fx_try_rate(db):
    return _fx_rate(db, "fx_try_per_usd")   # legacy alias (tests)


def find_matches_fx(db, vendor: str, fx_amount: float, date: str,
                    currency: str = "TRY"):
    """Candidates for a foreign-denominated receipt: trx within ±4 days
    whose USD amount lands inside the rate band. Confidence caps at MEDIUM
    so every FX match goes through the review queue."""
    if not date or not fx_amount or currency not in FX_CONFIG:
        return []
    cfg = FX_CONFIG[currency]
    rate = _fx_rate(db, cfg["key"])
    if rate:
        lo_rate, hi_rate = rate * (1 - FX_BAND_PCT), rate * (1 + FX_BAND_PCT)
    else:
        lo_rate, hi_rate = cfg["fallback"]
    try_amount = fx_amount   # kept name for the body below
    usd_lo = try_amount / hi_rate
    usd_hi = try_amount / lo_rate
    rows = db.execute("""
        SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
               t.owner, t.l1_category, t.l2_category, t.receipt_path,
               ABS(julianday(t.trx_date) - julianday(?)) AS date_diff
          FROM transactions t
         WHERE (t.status='active'
                OR (t.status='deleted' AND COALESCE(t.is_split,0)=1))
           AND ABS(t.amount) BETWEEN ? AND ?
           AND (julianday(t.trx_date) BETWEEN julianday(?, '-4 day')
                                          AND julianday(?, '+4 day'))
         ORDER BY date_diff ASC
         LIMIT 10
    """, (date, usd_lo, usd_hi, date, date)).fetchall()
    scored = []
    for r in rows:
        rate = try_amount / abs(r["amount"]) if r["amount"] else None
        vendor_match = bool(vendor) and (
            _vendor_tokens_match(vendor, r["vendor"] or "")
            or _vendor_tokens_match(vendor, r["raw_description"] or ""))
        # Selectivity gate (2026-07-03 fix): the rate band alone isn't
        # selective — for any ₺X there's often SOME tiny $X/rate trx
        # within a few days (a misread ₺ tax figure can land on a random
        # small USD charge). Keep a candidate only if the vendor agrees,
        # OR it's same/next-day AND a non-trivial amount.
        if not vendor_match and not (r["date_diff"] <= 1.0 and abs(r["amount"]) >= 5.0):
            continue
        scored.append({
            "trx_id": r["id"], "trx_date": r["trx_date"],
            "trx_vendor": r["vendor"], "trx_amount": r["amount"],
            "trx_owner": r["owner"], "trx_l1": r["l1_category"],
            "trx_l2": r["l2_category"], "trx_has_receipt": bool(r["receipt_path"]),
            "amt_match": False, "date_match": r["date_diff"] <= 3.0,
            "vendor_match": vendor_match,
            "confidence": "MEDIUM", "score": 2,
            "amt_diff": None, "date_diff": r["date_diff"],
            "fx_rate": round(rate, 2) if rate else None,
        })
    # Vendor agreement first, then date proximity
    scored.sort(key=lambda x: (not x["vendor_match"], x["date_diff"]))
    return scored


# ── Filing ───────────────────────────────────────────────────────────────────
# The sanitizer, path formula, and collision-move used to live here as
# "keep in sync" mirrors of app.py's copies. One copy now (Refactor
# Phase 4): receipts_engine.py. Local aliases keep every call site in
# this file (and its CLI surface) untouched.
from receipts_engine import (
    filename_vendor,
    filed_path_for_scored as filed_path_for,
    move_with_collision_suffix as file_receipt,
    canonical_dest_for_trx,
)
_filename_vendor = filename_vendor


# ── Main pipeline ────────────────────────────────────────────────────────────
def process_one(db, path: Path):
    """Process a single inbox file end-to-end. Returns the receipts row id."""
    # ── HEIC normalization (always-on) ──────────────────────────────────────
    # iPhone HEIC photos can't be embedded in <embed>/<img> by the browser
    # and most AI-assistant file readers can't see them. Convert to JPG immediately,
    # delete the HEIC, and process the JPG from here on. The JPG becomes
    # the canonical receipt for filing + preview.
    if path.suffix.lower() == ".heic" and not DRY_RUN:
        # (2026-07-03: gated on DRY_RUN — a "dry" run used to convert AND
        # delete the original HEIC. extract_text() handles HEIC via a temp
        # JPG on dry runs, so nothing on disk changes.)
        try:
            jpg = heic_to_jpg(path)
            log(f"{path.name} → {jpg.name} (HEIC converted to JPG, original deleted)", "ok")
            path.unlink()
            path = jpg
        except Exception as e:
            log(f"HEIC conversion failed for {path.name}: {e} — processing as-is", "warn")

    # Check if we've seen this file before (by content hash).
    # In --reprocess mode we re-scan unfiled rows (still skip filed ones —
    # those are done).
    chash = content_hash(path)
    existing = db.execute(
        "SELECT * FROM receipts WHERE content_hash=?", (chash,)
    ).fetchone()
    if existing:
        if not REPROCESS or existing["status"] == "filed":
            # (2026-07-03) Stale-orphan self-heal: an orphan/queued row whose
            # hash is already known used to be skipped forever — transactions
            # imported AFTER the receipt orphaned could never match. Re-run
            # the matcher from the STORED extraction (no OCR needed).
            if existing["status"] in ("orphan", "queued") and existing["extracted_amount"]:
                cands = find_matches(db, existing["extracted_vendor"],
                                     existing["extracted_amount"],
                                     existing["extracted_date"])
                nb = cands[0] if cands else None
                if nb and (existing["status"] == "orphan"
                           or (existing["matched_trx_id"] is None)):
                    if DRY_RUN:
                        log(f"{path.name}: DRY-RUN would re-match orphan → "
                            f"trx #{nb['trx_id']} ({nb['confidence']})", "warn")
                    else:
                        db.execute("""
                            UPDATE receipts SET matched_trx_id=?, match_confidence=?,
                                   match_score=?, status='queued', owner=?,
                                   updated_at=datetime('now')
                             WHERE id=?
                        """, (nb["trx_id"], nb["confidence"], nb["score"],
                              nb["trx_owner"], existing["id"]))
                        db.commit()
                        log(f"{path.name}: re-matched stale {existing['status']} → "
                            f"trx #{nb['trx_id']} ({nb['confidence']}) → queued", "ok")
                    return existing["id"]
            # (2026-07-03) Re-upload of an ALREADY-FILED receipt: don't skip
            # silently — create a marker row so the portal reviewer shows it
            # with a duplicate banner and the user decides (discard / attach as
            # extra). content_hash stays NULL on the marker (the unique index
            # belongs to the real row); one marker per filename max.
            if existing["status"] == "filed" and not DRY_RUN:
                marker = db.execute(
                    """SELECT id FROM receipts WHERE filename=?
                        AND status='suspected_duplicate' LIMIT 1""",
                    (path.name,)).fetchone()
                if not marker:
                    cur = db.execute("""
                        INSERT INTO receipts (filename, file_type, status,
                            matched_trx_id, match_confidence, owner, notes)
                        VALUES (?, ?, 'suspected_duplicate', ?, 'HIGH',
                                (SELECT owner FROM transactions WHERE id=?),
                                ?)
                    """, (path.name, sniff_mime(path), existing["matched_trx_id"],
                          existing["matched_trx_id"],
                          f"EXACT duplicate of already-filed receipt #{existing['id']} "
                          f"({existing['filename']}) — same file contents. "
                          f"Discard, or confirm to attach as an extra copy."))
                    db.commit()
                    log(f"{path.name}: EXACT duplicate of filed receipt "
                        f"#{existing['id']} → flagged for review", "warn")
                    return cur.lastrowid
            log(f"{path.name}: already in DB (id={existing['id']}, status={existing['status']}) — skipping",
                "warn")
            return existing["id"]
        # Reprocess: hold the existing extraction + match in memory. If the
        # new pass yields a usable result, it overwrites. If it yields
        # nothing (e.g. external tools missing), the prior values are KEPT —
        # avoids the regression where a reprocess demoted good matches just
        # because pdftotext had disappeared.
        receipt_id = existing["id"]
        prior = dict(existing)
        log(f"{path.name}: reprocessing (was status={existing['status']})", "info")
        mime = sniff_mime(path)
    else:
        mime  = sniff_mime(path)
        prior = None
        if DRY_RUN:
            # (2026-07-03) TRUE dry-run: no intake row. The old behavior
            # INSERTed + committed here, permanently consuming the file's
            # "first sight" — the next real run hit the hash-skip and never
            # matched, silently accumulating stale orphans.
            receipt_id = None
        else:
            # Insert intake row
            cur = db.execute("""
                INSERT INTO receipts (filename, content_hash, file_type, status)
                VALUES (?, ?, ?, 'inbox')
            """, (path.name, chash, mime))
            receipt_id = cur.lastrowid
            db.commit()

    # Extract text — pdftotext, OCR for HEIC/PNG/JPG, OCR-via-PNG for image PDFs
    text, text_source = extract_text(path, mime)
    if not text and mime != "application/octet-stream":
        log(f"{path.name}: no text extracted ({mime})", "warn")

    # Try each extractor in order
    extracted = None
    for extractor in EXTRACTORS:
        result = extractor(text, path.name)
        if result and result.get("amount") is not None:
            extracted = result
            extracted["extractor"] = extractor.__name__
            break

    if extracted is None:
        # Fall through to generic
        extracted = extract_generic(text, path.name, db=db)
        extracted["extractor"] = "generic"

    # Match — FX-aware (2026-07-03): a lira-denominated amount can never tie
    # to the USD bank charge, so TRY receipts match by date + rate band
    # instead (capped at MEDIUM → always reviewed). Retires the old
    # manual queue workflow.
    fx_note = None
    currency = extracted.get("currency", "USD")
    if currency in FX_CONFIG:
        sym = FX_CONFIG[currency]["symbol"]
        candidates = find_matches_fx(db, extracted["vendor"],
                                     extracted["amount"], extracted["date"],
                                     currency=currency)
        if candidates:
            fx_note = (f"FX: receipt is {sym}{extracted['amount']:.2f} {currency}; "
                       f"implied rate ~{candidates[0]['fx_rate']} — verify in review")
        else:
            amt_s = f"{extracted['amount']:.2f}" if extracted.get("amount") else "?"
            fx_note = (f"Foreign currency ({currency}) receipt — {sym}{amt_s}; "
                       f"no rate-band match, match manually by date/vendor")
    elif currency != "USD":
        candidates = []
        amt_s = f"{extracted['amount']:.2f}" if extracted.get("amount") else "?"
        fx_note = (f"Foreign currency ({currency}) receipt — {amt_s} {currency}; "
                   f"match manually by date/vendor")
    else:
        candidates = find_matches(db, extracted["vendor"], extracted["amount"],
                                  extracted["date"])
    best = candidates[0] if candidates else None

    # Reprocess protection: if the new pass yielded nothing AND we had a
    # prior match in the DB, preserve the prior match instead of demoting
    # this row to orphan. Common case: external tool went missing between
    # runs (poppler uninstalled, OCR backend changed, etc.).
    if (prior and not best
            and (extracted.get("vendor") is None
                 or extracted.get("amount") is None)
            and prior.get("matched_trx_id")):
        log(f"{path.name}: new extraction empty — preserving prior match "
            f"(trx #{prior['matched_trx_id']}, {prior['match_confidence']})", "info")
        db.execute("""
            UPDATE receipts SET status=?, updated_at=datetime('now')
             WHERE id=?
        """, (prior["status"], receipt_id))
        return receipt_id

    # Duplicate detection — two flavors:
    #
    # (1) "Already filed" — best-match trx already has at least one filed
    #     receipt. The new receipt is most likely a re-send of the same one
    #     (forwarded email, re-screenshot, etc.). The user confirms and the
    #     file is deleted from inbox, OR overrides and we attach as a
    #     second receipt. Until then, the file STAYS in inbox and the
    #     receipts row is set to status='suspected_duplicate' so the
    #     review UI surfaces it.
    #
    # (2) "Same metadata, both pending" — two NEW receipts in this same
    #     run (or earlier unfiled rows) match the same trx with same
    #     amount + date. Common case: same receipt photographed twice or
    #     downloaded twice. Demote HIGH→MED so the user picks which to
    #     keep in the queue UI.
    dup_note = None
    suspected_already_filed = False
    if best:
        # Flavor 1: existing FILED receipt on the same trx
        existing_filed = db.execute("""
            SELECT id, filename, filed_path FROM receipts
             WHERE matched_trx_id=? AND id != ?
               AND status='filed'
             ORDER BY id LIMIT 1
        """, (best["trx_id"], receipt_id or -1)).fetchone()
        if existing_filed:
            suspected_already_filed = True
            dup_note = (f"trx #{best['trx_id']} already has filed receipt "
                        f"#{existing_filed['id']} ({existing_filed['filename']}). "
                        f"Awaiting confirmation to delete this duplicate.")

        # Flavor 2: another unfiled receipt with same metadata (new-batch dupes)
        if not suspected_already_filed:
            existing_dup = db.execute("""
                SELECT id, filename FROM receipts
                 WHERE matched_trx_id=? AND id != ?
                   AND status IN ('matched','queued','linked')
                   AND (extracted_amount = ?
                        OR (extracted_amount IS NULL AND ? IS NULL))
                   AND (extracted_date = ?
                        OR (extracted_date IS NULL AND ? IS NULL))
                 LIMIT 1
            """, (best["trx_id"], receipt_id or -1,
                  extracted.get("amount"), extracted.get("amount"),
                  extracted.get("date"),   extracted.get("date"))).fetchone()
            if existing_dup:
                dup_note = (f"possible duplicate of receipt id={existing_dup['id']} "
                            f"({existing_dup['filename']}) — same trx + amount + date")
                if best["confidence"] == "HIGH":
                    best["confidence"] = "MEDIUM"
                    best["score"] = 2

    # Update DB
    update_fields = {
        "extracted_vendor":     extracted.get("vendor"),
        "extracted_amount":     extracted.get("amount"),
        "extracted_date":       extracted.get("date"),
        "extracted_order_id":   extracted.get("order_id"),
        "extractor_used":       extracted.get("extractor"),
        "extraction_confidence":extracted.get("confidence"),
        "matched_trx_id":       best["trx_id"] if best else None,
        "match_confidence":     best["confidence"] if best else "NONE",
        "match_score":          best["score"] if best else 0,
        "notes":                " | ".join(n for n in (dup_note, fx_note) if n) or None,
        # Receipt owner = matched trx owner. If unmatched (orphan), default
        # to the single configured OWNER so the row shows on the orphans page.
        "owner":                best["trx_owner"] if best else OWNER,
    }

    if suspected_already_filed:
        # File stays in inbox. The review UI asks: delete the dup, or attach
        # as additional receipt? After the user answers, the row gets
        # routed to either status='deleted' (and the file removed) or
        # status='matched' (and the file moves with --file-pending).
        update_fields["status"] = "suspected_duplicate"
    elif best and best["confidence"] == "HIGH":
        # Review-by-default: HIGH goes to the portal queue unless --autofile.
        update_fields["status"] = "matched" if DO_AUTOFILE else "queued"
    elif best and best["confidence"] == "MEDIUM":
        update_fields["status"] = "queued"
    else:
        update_fields["status"] = "orphan"

    # Persist (skipped entirely on dry runs — 2026-07-03; the old code
    # UPDATEd + committed here even with --dry-run)
    if DRY_RUN:
        log(f"{path.name}: DRY-RUN result — vendor={extracted.get('vendor')} "
            f"amount={extracted.get('amount')} date={extracted.get('date')} "
            f"currency={extracted.get('currency', 'USD')} → "
            f"{'trx #%s (%s)' % (best['trx_id'], best['confidence']) if best else 'no match'} "
            f"[status would be: {update_fields['status']}]", "info")
        return None
    sets   = ", ".join(f"{k}=?" for k in update_fields)
    params = list(update_fields.values()) + [receipt_id]
    db.execute(f"UPDATE receipts SET {sets}, updated_at=datetime('now') WHERE id=?", params)
    db.commit()

    # Auto-file HIGH-confidence matches
    # Skip auto-file if this row was flagged as a suspected dup of an
    # already-filed receipt — wait for the user's decision.
    if (best and best["confidence"] == "HIGH"
            and update_fields.get("status") == "matched"
            and not NO_FILE and not DRY_RUN):
        ext = path.suffix.lower()
        dest = filed_path_for(best, ext)
        try:
            final_dest = file_receipt(path, dest)
            db.execute("""
                UPDATE receipts SET filed_path=?, status='filed',
                       updated_at=datetime('now') WHERE id=?
            """, (to_mac_path(final_dest), receipt_id))
            # Sync primary receipt cache on the trx
            db.execute("""
                UPDATE transactions
                   SET receipt_path = COALESCE(
                       (SELECT filed_path FROM receipts
                         WHERE matched_trx_id=? AND status='filed' AND filed_path IS NOT NULL
                         ORDER BY id ASC LIMIT 1),
                       receipt_path)
                 WHERE id=?
            """, (best["trx_id"], best["trx_id"]))
            # If the matched trx is a split parent, push the receipt to its
            # children too (the parent is deleted/hidden after a split).
            db.execute("""
                UPDATE transactions
                   SET receipt_path = COALESCE(
                       (SELECT filed_path FROM receipts
                         WHERE matched_trx_id=? AND status='filed' AND filed_path IS NOT NULL
                         ORDER BY id ASC LIMIT 1),
                       receipt_path)
                 WHERE parent_id=?
            """, (best["trx_id"], best["trx_id"]))
            db.commit()
            log(f"{path.name} → trx #{best['trx_id']} ({best['trx_owner']} "
                f"{best['trx_l1']}/{best['trx_l2']}) → {final_dest}", "ok")
        except Exception as e:
            log(f"{path.name}: file move failed: {e}", "err")

    elif suspected_already_filed:
        log(f"{path.name}: SUSPECTED DUPLICATE — trx #{best['trx_id']} "
            f"already has a filed receipt. Awaiting review confirm.", "warn")
    elif best and best["confidence"] == "HIGH" and not DO_AUTOFILE:
        log(f"{path.name} → trx #{best['trx_id']} HIGH → queued "
            f"(review in portal)", "warn")
    elif best and best["confidence"] == "HIGH":
        log(f"{path.name} → trx #{best['trx_id']} HIGH (skip-file mode)", "ok")
    elif best and best["confidence"] == "MEDIUM":
        log(f"{path.name} → trx #{best['trx_id']} MED (queued for review)", "warn")
    else:
        log(f"{path.name}: no match (orphan)", "warn")

    return receipt_id


def rematch_unresolved(db):
    """(2026-07-04) Every scan re-evaluates ALL unresolved receipts —
    orphans AND queued — against the CURRENT transactions table, not just
    new inbox files:

      - orphan that now has a match          → promoted to queued
      - queued whose suggestion has a better  → suggestion refreshed
        candidate now (still just a suggestion; reviewed as always)
      - queued whose matched trx vanished     → re-matched, else demoted to orphan
      - queued whose matched trx changed owner → receipt owner follows (re-sort)
      - unmatched orphan                      → untouched (incl. manual flips)

    Never touches linked / matched / filed / suspected_duplicate / deleted
    rows — those carry a decision (the user's or the pipeline's) already.
    FX rows have no stored currency; infer TRY/EUR from the pipeline note."""
    import re as _re
    rows = db.execute(
        "SELECT * FROM receipts WHERE status IN ('orphan','queued')").fetchall()
    if not rows:
        return {"promoted": 0, "refreshed": 0, "demoted": 0, "resorted": 0}
    n = {"promoted": 0, "refreshed": 0, "demoted": 0, "resorted": 0}

    def _valid_trx(tid):
        if not tid:
            return None
        return db.execute("""
            SELECT id, owner FROM transactions
             WHERE id=? AND (status='active'
                             OR (status='deleted' AND COALESCE(is_split,0)=1))
        """, (tid,)).fetchone()

    for r in rows:
        old_id  = r["matched_trx_id"]
        old_trx = _valid_trx(old_id)

        # No stored amount → can't re-match; still heal a queued row whose
        # target changed owner or vanished.
        if not r["extracted_amount"]:
            if r["status"] == "queued" and old_trx and old_trx["owner"] != r["owner"]:
                db.execute("UPDATE receipts SET owner=?, updated_at=datetime('now') WHERE id=?",
                           (old_trx["owner"], r["id"]))
                n["resorted"] += 1
                log(f"{r['filename']}: matched trx #{old_id} changed owner → "
                    f"receipt re-sorted to {old_trx['owner']}", "ok")
            elif r["status"] == "queued" and old_id and not old_trx:
                db.execute("""UPDATE receipts SET matched_trx_id=NULL,
                              match_confidence='NONE', match_score=0,
                              status='orphan', updated_at=datetime('now')
                              WHERE id=?""", (r["id"],))
                n["demoted"] += 1
                log(f"{r['filename']}: matched trx #{old_id} no longer exists → orphaned", "warn")
            continue

        # Currency isn't a stored column — infer FX from the pipeline note.
        m = _re.search(r"\b(TRY|EUR)\b", r["notes"] or "")
        currency = m.group(1) if m else "USD"
        fx_note = None
        if currency in FX_CONFIG:
            cands = find_matches_fx(db, r["extracted_vendor"],
                                    r["extracted_amount"], r["extracted_date"],
                                    currency=currency)
            if cands:
                sym = FX_CONFIG[currency]["symbol"]
                fx_note = (f"FX: receipt is {sym}{r['extracted_amount']:.2f} {currency}; "
                           f"implied rate ~{cands[0]['fx_rate']} — verify in review")
        else:
            cands = find_matches(db, r["extracted_vendor"],
                                 r["extracted_amount"], r["extracted_date"])
        best = cands[0] if cands else None

        if not best:
            # Nothing matches now. Keep a queued row whose old suggestion is
            # still a real trx (it can still be reviewed); demote only if the
            # trx is gone. Orphans just stay orphans.
            if r["status"] == "queued" and old_id and not old_trx:
                db.execute("""UPDATE receipts SET matched_trx_id=NULL,
                              match_confidence='NONE', match_score=0,
                              status='orphan', updated_at=datetime('now')
                              WHERE id=?""", (r["id"],))
                n["demoted"] += 1
                log(f"{r['filename']}: matched trx #{old_id} gone, no re-match → orphaned", "warn")
            continue

        if r["status"] == "queued" and old_trx and best["trx_id"] == old_id:
            # Same suggestion — just follow a portal move.
            if best["trx_owner"] != r["owner"]:
                db.execute("UPDATE receipts SET owner=?, updated_at=datetime('now') WHERE id=?",
                           (best["trx_owner"], r["id"]))
                n["resorted"] += 1
                log(f"{r['filename']}: trx #{old_id} changed owner → "
                    f"receipt re-sorted to {best['trx_owner']}", "ok")
            continue

        # New/changed suggestion — mirror process_one's dup handling + status.
        dup_note = None
        new_status = "queued"
        filed_dup = db.execute("""
            SELECT id, filename FROM receipts
             WHERE matched_trx_id=? AND id != ? AND status='filed'
             ORDER BY id LIMIT 1
        """, (best["trx_id"], r["id"])).fetchone()
        if filed_dup:
            dup_note = (f"trx #{best['trx_id']} already has filed receipt "
                        f"#{filed_dup['id']} ({filed_dup['filename']}). "
                        f"Awaiting confirmation to delete this duplicate.")
            new_status = "suspected_duplicate"

        was = r["status"]
        db.execute("""
            UPDATE receipts SET matched_trx_id=?, match_confidence=?,
                   match_score=?, status=?, owner=?, notes=?,
                   updated_at=datetime('now')
             WHERE id=?
        """, (best["trx_id"], best["confidence"], best["score"], new_status,
              best["trx_owner"],
              " | ".join(x for x in (dup_note, fx_note) if x) or None,
              r["id"]))
        if was == "orphan":
            n["promoted"] += 1
            log(f"{r['filename']}: orphan re-matched → trx #{best['trx_id']} "
                f"({best['confidence']}, {best['trx_owner']}) → {new_status}", "ok")
        else:
            n["refreshed"] += 1
            log(f"{r['filename']}: suggestion refreshed → trx #{best['trx_id']} "
                f"({best['confidence']}, {best['trx_owner']}) → {new_status}", "ok")

    db.commit()
    total = sum(n.values())
    if total:
        log(f"Re-match pass: {n['promoted']} orphan(s) matched, "
            f"{n['refreshed']} suggestion(s) refreshed, {n['demoted']} demoted, "
            f"{n['resorted']} re-sorted.", "ok")
    else:
        log("Re-match pass: nothing to update.", "info")
    return n


def report_state(db):
    """Print current receipts table state — what's in inbox vs filed vs queued."""
    print("\n=== RECEIPTS TABLE STATE ===\n")
    counts = db.execute("""
        SELECT status, COUNT(*) AS n FROM receipts GROUP BY status
    """).fetchall()
    if not counts:
        print("(empty)")
        return
    for c in counts:
        print(f"  {c['status']:<10} {c['n']:>4}")
    print()
    queued = db.execute("""
        SELECT id, filename, extracted_vendor, extracted_amount, extracted_date,
               matched_trx_id, match_confidence, status, notes
          FROM receipts
         WHERE status IN ('queued','orphan','inbox','suspected_duplicate','linked')
         ORDER BY status, id
    """).fetchall()
    if queued:
        print("PENDING REVIEW:")
        print(f"  {'id':>4} {'status':<8} {'conf':<7} {'vendor':<20} {'amt':>8} "
              f"{'date':<11} {'trx':>5}  filename")
        for r in queued:
            print(f"  {r['id']:>4} "
                  f"{(r['match_confidence'] or '—'):<7} "
                  f"{(r['extracted_vendor'] or '—')[:20]:<20} "
                  f"{('$%.2f' % r['extracted_amount']) if r['extracted_amount'] else '—':>8} "
                  f"{(r['extracted_date'] or '—'):<11} "
                  f"{(str(r['matched_trx_id']) if r['matched_trx_id'] else '—'):>5}  "
                  f"{r['filename']}")


def file_pending(db):
    """Process every status='linked' receipt — these are rows the user has
    matched to a trx via the portal but whose files are still in the inbox.
    For each: find the file, move + rename to canonical destination, set
    status='filed', sync transactions.receipt_path.

    This is the "second half" of the portal-as-linker / script-as-filer
    split. The portal does the matching UX; this function does all
    filesystem work in a controlled batch.
    """
    # (2026-07-03: also pick up stranded status='matched' rows — an autofile
    # crash between the status commit and the file move used to leave them
    # in a state nothing processed. And carry content_hash for move safety.)
    rows = db.execute("""
        SELECT r.id AS receipt_id, r.filename, r.filed_path, r.matched_trx_id,
               r.content_hash,
               t.owner AS trx_owner, t.trx_date AS trx_date,
               t.vendor AS trx_vendor, t.l1_category AS trx_l1,
               t.l2_category AS trx_l2
          FROM receipts r LEFT JOIN transactions t ON t.id = r.matched_trx_id
         WHERE r.status IN ('linked', 'matched')
         ORDER BY r.id ASC
    """).fetchall()

    if not rows:
        return  # nothing pending

    print(f"\n=== Filing {len(rows)} linked receipt(s) ===\n")

    filed = 0
    failed = 0
    for r in rows:
        if not r["matched_trx_id"]:
            log(f"#{r['receipt_id']} {r['filename']}: no matched_trx_id "
                f"despite status=linked — skipping", "warn")
            failed += 1
            continue

        # Locate file: filed_path first (rare), then inbox
        src = None
        if r["filed_path"] and Path(r["filed_path"]).is_file():
            src = Path(r["filed_path"])
        else:
            inbox_p = INBOX_DIR / r["filename"]
            if inbox_p.is_file():
                src = inbox_p
            else:
                # Last resort — search the receipts tree. Hash-verified
                # (2026-07-03 fix): generic basenames like "Receipt.pdf" /
                # "IMG_1234.jpeg" could resolve to a DIFFERENT, already-filed
                # receipt — and move it. Only accept a content-hash match.
                src = _find_file_anywhere(r["filename"],
                                          expected_hash=r["content_hash"])

        if not src:
            log(f"#{r['receipt_id']} {r['filename']}: file not found "
                f"(checked filed_path, inbox, tree). Skipping.", "err")
            failed += 1
            continue

        ext = src.suffix.lower() or ".pdf"
        # Build a synthetic scored-style dict so filed_path_for() works
        synth = {
            "trx_owner":  r["trx_owner"],
            "trx_date":   r["trx_date"],
            "trx_vendor": r["trx_vendor"],
            "trx_l1":     r["trx_l1"],
            "trx_l2":     r["trx_l2"],
        }
        dest = filed_path_for(synth, ext)

        try:
            final = file_receipt(src, dest)
        except Exception as e:
            log(f"#{r['receipt_id']} {r['filename']}: move failed: {e}", "err")
            failed += 1
            continue

        db.execute("""
            UPDATE receipts SET status='filed', filed_path=?,
                   updated_at=datetime('now') WHERE id=?
        """, (to_mac_path(final), r["receipt_id"]))
        # Sync trx primary
        db.execute("""
            UPDATE transactions
               SET receipt_path = COALESCE(
                   (SELECT filed_path FROM receipts
                     WHERE matched_trx_id=? AND status='filed'
                           AND filed_path IS NOT NULL
                     ORDER BY id ASC LIMIT 1),
                   receipt_path)
             WHERE id=?
        """, (r["matched_trx_id"], r["matched_trx_id"]))
        # Propagate to split children if the match is a split parent.
        db.execute("""
            UPDATE transactions
               SET receipt_path = COALESCE(
                   (SELECT filed_path FROM receipts
                     WHERE matched_trx_id=? AND status='filed' AND filed_path IS NOT NULL
                     ORDER BY id ASC LIMIT 1),
                   receipt_path)
             WHERE parent_id=?
        """, (r["matched_trx_id"], r["matched_trx_id"]))
        log(f"#{r['receipt_id']} {r['filename']} → trx #{r['matched_trx_id']} "
            f"({r['trx_owner']} {r['trx_l1']}/{r['trx_l2']}) → {final}", "ok")
        filed += 1
        # Per-row commit (2026-07-03 fix): the old single batch-end commit
        # meant a crash mid-batch left files moved on disk with the DB
        # still saying 'linked' for all of them.
        db.commit()

    # (2026-07-04) Any trx that ended up WITH a receipt no longer
    # needs its "no receipt needed" pass — clear the flag so a later detach
    # re-surfaces the row in Missing Receipts. (Common case: a Chase-credit
    # contra-expense auto-flagged at import, later linked to the original
    # purchase and adopting its receipt.)
    db.execute("""
        UPDATE transactions SET no_receipt_needed=0
         WHERE receipt_path IS NOT NULL AND COALESCE(no_receipt_needed,0)=1
    """)
    db.commit()
    print(f"\nFiling complete: {filed} filed · {failed} failed\n")


def _find_file_anywhere(filename: str, expected_hash: str | None = None) -> Path | None:
    """Hunt for a file by basename. Tries:
      1. Inbox
      2. The entire Receipts/ tree (recursive)
    When expected_hash is given, every candidate must match it — basename
    collisions ("Receipt.pdf", "IMG_NNNN.jpeg") otherwise risk grabbing and
    moving a DIFFERENT receipt (2026-07-03 fix). Returns first hit or None."""
    def _ok(p: Path) -> bool:
        if expected_hash is None:
            return True
        try:
            return content_hash(p) == expected_hash
        except OSError:
            return False

    inbox_candidate = INBOX_DIR / filename
    if inbox_candidate.is_file() and _ok(inbox_candidate):
        return inbox_candidate
    receipts_root = FILED_ROOT  # the receipts/ tree (inbox + filed)
    for p in receipts_root.rglob(filename):
        if p.is_file() and _ok(p):
            return p
    return None


# Same naming/folder convention as filed_path_for, but takes a
# transactions-table row (not a scored find_matches dict). Used by scrub.
# Moved to receipts_engine (Refactor Phase 4) — local alias.
_canonical_dest = canonical_dest_for_trx


def scrub_filed(db):
    """Walk every status='filed' receipt and verify:
      - The file actually exists at filed_path
      - The file is in the canonical destination (correct folder, name)
      - matched_trx_id is set + valid

    Fixes what it can: relocate misfiled files, rename to YYMMDD Vendor.ext.
    What it can't fix (file lost on disk): mark receipt as orphan.
    Re-syncs every transactions.receipt_path at the end.
    """
    print("\n=== SCRUB: filed receipts ===\n")
    rows = db.execute("""
        SELECT r.*, t.owner AS trx_owner, t.trx_date AS trx_date,
               t.vendor AS trx_vendor, t.l1_category AS trx_l1,
               t.l2_category AS trx_l2
          FROM receipts r LEFT JOIN transactions t ON t.id = r.matched_trx_id
         WHERE r.status='filed'
         ORDER BY r.id ASC
    """).fetchall()

    if not rows:
        log("No filed receipts to scrub.", "info")
        return

    moved = renamed = healed = lost = ok = 0

    for r in rows:
        rid      = r["id"]
        filename = r["filename"]

        if not r["trx_owner"]:
            log(f"#{rid} {filename}: no matched_trx_id — can't rebuild path", "warn")
            continue

        ext = os.path.splitext(filename)[1].lower() or ".pdf"
        canonical = _canonical_dest({
            "owner":       r["trx_owner"],
            "trx_date":    r["trx_date"],
            "vendor":      r["trx_vendor"],
            "l1_category": r["trx_l1"],
            "l2_category": r["trx_l2"],
        }, ext)

        # 1. Where IS the file actually?
        current = None
        if r["filed_path"] and Path(r["filed_path"]).is_file():
            current = Path(r["filed_path"])
        else:
            current = _find_file_anywhere(filename, expected_hash=r["content_hash"])

        if not current:
            log(f"#{rid} {filename}: FILE NOT FOUND → marking orphan", "err")
            db.execute(
                "UPDATE receipts SET status='orphan', filed_path=NULL, "
                "notes='file lost on disk during scrub' WHERE id=?", (rid,)
            )
            lost += 1
            continue

        # 2. If already at canonical path with right name, no-op.
        if current.resolve() == canonical.resolve():
            # Just make sure the DB has the right path (Mac-native form)
            mac = to_mac_path(canonical)
            if r["filed_path"] != mac:
                db.execute("UPDATE receipts SET filed_path=? WHERE id=?",
                           (mac, rid))
                healed += 1
            else:
                ok += 1
            continue

        # 3. Need to move and/or rename. Handle collisions.
        canonical.parent.mkdir(parents=True, exist_ok=True)
        final = canonical
        if final.exists() and final.resolve() != current.resolve():
            stem = canonical.stem
            n = 2
            while True:
                candidate = canonical.with_name(f"{stem} ({n}){ext}")
                if not candidate.exists():
                    final = candidate; break
                n += 1

        action = "moved" if current.parent != final.parent else "renamed"
        try:
            shutil.move(str(current), str(final))
            db.execute(
                "UPDATE receipts SET filed_path=?, updated_at=datetime('now') WHERE id=?",
                (to_mac_path(final), rid)
            )
            log(f"#{rid} {filename}: {action} → {final}", "ok")
            if action == "moved":
                moved += 1
            else:
                renamed += 1
        except Exception as e:
            log(f"#{rid} {filename}: move failed: {e}", "err")

    db.commit()

    # Re-sync transactions.receipt_path globally
    print("\n=== Syncing transactions.receipt_path ===\n")
    trx_with_receipts = db.execute("""
        SELECT DISTINCT matched_trx_id FROM receipts
         WHERE status='filed' AND matched_trx_id IS NOT NULL
    """).fetchall()
    for row in trx_with_receipts:
        primary = db.execute("""
            SELECT filed_path FROM receipts
             WHERE matched_trx_id=? AND status='filed'
                   AND filed_path IS NOT NULL
             ORDER BY id ASC LIMIT 1
        """, (row["matched_trx_id"],)).fetchone()
        db.execute(
            "UPDATE transactions SET receipt_path=? WHERE id=?",
            (primary["filed_path"] if primary else None, row["matched_trx_id"])
        )
        # Re-propagate to split children (2026-07-03 fix: filing propagates
        # receipt_path to children, but scrub used to wipe it — children are
        # never anyone's matched_trx_id, so the cleanup below nuked them and
        # nothing restored them).
        db.execute(
            "UPDATE transactions SET receipt_path=? WHERE parent_id=?",
            (primary["filed_path"] if primary else None, row["matched_trx_id"])
        )
    # Clear stale receipt_path on trxs with no filed receipt — EXCEPT split
    # children whose parent holds the family receipt (2026-07-03 fix).
    db.execute("""
        UPDATE transactions SET receipt_path=NULL
         WHERE id NOT IN (SELECT matched_trx_id FROM receipts
                           WHERE status='filed' AND matched_trx_id IS NOT NULL)
           AND (parent_id IS NULL
                OR parent_id NOT IN (SELECT matched_trx_id FROM receipts
                                      WHERE status='filed' AND matched_trx_id IS NOT NULL))
           AND receipt_path IS NOT NULL
    """)
    db.commit()

    print(f"\nScrub complete: {ok} ok · {moved} moved · {renamed} renamed · "
          f"{healed} db-path healed · {lost} files lost (now orphan)\n")


def setup_environment():
    """Self-heal missing dependencies on first run — the pipeline detects
    gaps and installs/builds them so you don't have to brew/swiftc/pip
    anything by hand.

    Three checks, all idempotent:
      1. poppler (pdftotext + pdftoppm) — needed for PDF text extraction
      2. Swift Apple Vision OCR binary — built from bin/apple_ocr.swift
      3. brew itself — required for #1; if missing, surface a clear error

    Failures are logged but non-fatal: pipeline keeps going with whatever
    backends ARE working. Files needing the missing tool stay as orphans.
    """
    # ── 1. Check brew ────────────────────────────────────────────────────────
    brew = shutil.which("brew")
    if not brew:
        log("Homebrew not found. Some OCR/PDF tools may be unavailable.", "warn")
        log("Install brew once at https://brew.sh, then re-run.", "warn")

    # ── 2. poppler (pdftotext + pdftoppm) ────────────────────────────────────
    if not shutil.which("pdftotext") or not shutil.which("pdftoppm"):
        if brew:
            log("poppler not installed — running `brew install poppler` (one-time)...", "info")
            try:
                r = subprocess.run(
                    [brew, "install", "poppler"],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode == 0:
                    log("poppler installed", "ok")
                else:
                    log(f"brew install poppler failed (exit {r.returncode}). "
                        f"Last stderr: {r.stderr[-200:]}", "warn")
            except subprocess.TimeoutExpired:
                log("brew install poppler timed out (>10 min). Skipping.", "warn")
            except Exception as e:
                log(f"brew install poppler errored: {e}", "warn")
        else:
            log("poppler missing AND brew unavailable — PDFs without a text "
                "layer can't be processed until poppler is installed.", "warn")

    # ── 3. Apple Vision OCR Swift binary ────────────────────────────────────
    swift_src = HERE / "bin" / "apple_ocr.swift"
    swift_bin = HERE / "bin" / "apple_ocr"
    if swift_src.exists() and not swift_bin.exists():
        if shutil.which("swiftc"):
            log("Building Apple Vision OCR binary (one-time)...", "info")
            try:
                r = subprocess.run(
                    ["swiftc", "-O", str(swift_src), "-o", str(swift_bin)],
                    capture_output=True, text=True, timeout=180,
                    cwd=str(swift_src.parent),
                )
                if r.returncode == 0 and swift_bin.exists():
                    log(f"apple_ocr built at {swift_bin}", "ok")
                else:
                    log(f"swiftc failed (exit {r.returncode}). "
                        f"Last stderr: {r.stderr[-200:]}", "warn")
            except subprocess.TimeoutExpired:
                log("swiftc timed out. Skipping.", "warn")
            except Exception as e:
                log(f"swiftc errored: {e}", "warn")
        else:
            log("swiftc not found (Xcode CLT missing?). HEIC/image OCR "
                "will skip Apple Vision and fall back to Tesseract if "
                "installed, else orphan.", "warn")


def main():
    setup_environment()

    if not DB_PATH.exists():
        log(f"DB not found at {DB_PATH}", "err")
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    # (2026-07-03) The Flask app holds this DB too — wait out its write
    # locks instead of crashing with "database is locked".
    db.execute("PRAGMA busy_timeout = 5000")

    if SCRUB:
        scrub_filed(db)
        report_state(db)
        return

    if REPORT:
        report_state(db)
        return

    # --file-pending: skip the inbox scan, only process linked rows.
    # Useful right after confirming a batch in the portal.
    if FILE_PENDING:
        if not DRY_RUN:
            file_pending(db)
        else:
            n = db.execute(
                "SELECT COUNT(*) FROM receipts WHERE status='linked'"
            ).fetchone()[0]
            log(f"DRY-RUN: would file {n} linked receipt(s).", "warn")
        report_state(db)
        return

    # Default mode: process inbox first, then file any pending links.
    # This means a single `python3 process_receipts.py` invocation handles
    # both new inbox files AND any portal-confirmed receipts linked
    # since the last run.
    if not INBOX_DIR.exists():
        log(f"Inbox not found at {INBOX_DIR}", "err")
        sys.exit(1)

    files = sorted(p for p in INBOX_DIR.iterdir()
                   if p.is_file() and not p.name.startswith("."))
    if files:
        log(f"Processing {len(files)} file(s) from {INBOX_DIR}...", "info")
        if DRY_RUN: log("DRY-RUN: no DB writes, no file moves", "warn")
        if NO_FILE: log("NO-FILE: DB writes only, files stay in inbox", "warn")
        if DO_AUTOFILE:
            log("AUTOFILE: HIGH matches auto-file (review-default overridden)", "warn")
        else:
            log("REVIEW MODE (default): all matches → portal queue; confirm there before filing", "warn")
        print()

        for p in files:
            try:
                process_one(db, p)
            except Exception as e:
                log(f"{p.name}: unhandled error: {e}", "err")
                import traceback; traceback.print_exc()
    else:
        log("Inbox empty — skipping intake step.", "info")

    # (2026-07-04) Re-match pass on EVERY scan: re-evaluate all
    # unresolved receipts (orphans + queued) against the current db —
    # transactions imported since the last scan, portal moves, deletions.
    # DB-only (no file moves), so it also runs under --no-file.
    if not DRY_RUN:
        rematch_unresolved(db)

    # Always file pending links after inbox step, unless dry-run or no-file.
    # (2026-07-03 fix: --no-file used to still run this and move files,
    # breaking its "files stay in inbox" promise.)
    if not DRY_RUN and not NO_FILE:
        file_pending(db)
    elif NO_FILE:
        n = db.execute("SELECT COUNT(*) FROM receipts "
                       "WHERE status IN ('linked','matched')").fetchone()[0]
        if n:
            log(f"NO-FILE: {n} linked/matched receipt(s) awaiting a filing run.", "warn")

    print()
    report_state(db)
    db.close()


if __name__ == "__main__":
    main()
