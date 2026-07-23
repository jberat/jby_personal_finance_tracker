"""
receipts_engine.py — shared pure receipt-filing logic (paths + moves).
Extracted from app.py + process_receipts.py in Refactor Phase 4
(2026-07-04). PURE MOVE — no behavior change.

Before this module, the vendor sanitizer, the canonical filed-path
formula, and the " (n)" collision-move existed in duplicate copies
marked "keep in sync" across app.py and process_receipts.py. This is
the single copy. DB-touching wrappers (app._file_receipt's UPDATE,
scrub, file_pending) stay in their callers — only pure path/move
logic lives here.

Canonical filed path:
    RECEIPTS_ROOT/<owner_folder>/<YYYY>/<L1>/<L2>/<MM>/<YYMMDD Vendor><ext>
owner_folder = "filed" in this single-owner build (config maps every
owner value there via _owner_to_receipts_folder).
"""
import re
import shutil
from pathlib import Path

from config import RECEIPTS_ROOT, _owner_to_receipts_folder


def filename_vendor(vendor) -> str:
    """Sanitize a vendor name for use in a filed-receipt filename
    (2026-07-03): strip filesystem-hostile chars, trim weird
    leading/trailing punctuation (no more 'Vendor..jpg' double dots), cap at
    20 chars breaking at a word boundary."""
    v = (vendor or "Unknown").replace("/", "-")
    v = re.sub(r'[\\:*?"<>|]', "", v)
    v = re.sub(r"\s{2,}", " ", v).strip()
    v = v.strip(" .,-_;'")
    if len(v) > 20:
        cut = v[:20]
        v = cut.rsplit(" ", 1)[0] if " " in cut else cut
        v = v.strip(" .,-_;'")
    return v or "Unknown"


def canonical_filed_path(owner, trx_date, vendor, l1, l2, ext) -> Path:
    """ONE implementation of the filed-path formula:
       RECEIPTS_ROOT/<owner_folder>/<YYYY>/<L1>/<L2>/<MM>/<YYMMDD Vendor><ext>
    `ext` is used verbatim — the adapters below lowercase it exactly where
    their originals did. Callers use the adapters, which map each caller's
    row/dict shape onto these six positional facts."""
    owner_folder = _owner_to_receipts_folder(owner)
    year   = trx_date[:4]
    mm     = trx_date[5:7]
    yymmdd = trx_date[2:4] + mm + trx_date[8:10]
    l1     = (l1 or "Uncategorized").replace("/", "-")
    l2     = (l2 or "Uncategorized").replace("/", "-")
    fname  = f"{yymmdd} {filename_vendor(vendor)}{ext}"
    return Path(RECEIPTS_ROOT) / owner_folder / year / l1 / l2 / mm / fname


# ── Thin adapters — one per historical call shape ────────────────────────────

def canonical_dest_for_trx(trx, ext: str) -> Path:
    """transactions-table row shape (owner / trx_date / vendor /
    l1_category / l2_category). Extension lowercased, Path return.
    Was process_receipts._canonical_dest (scrub's variant)."""
    return canonical_filed_path(
        trx["owner"], trx["trx_date"], trx["vendor"],
        trx["l1_category"], trx["l2_category"], ext.lower())


def canonical_filed_path_for_trx(trx, ext: str) -> str:
    """Same transactions-row shape, str return (app.py works in
    strings/os.path). Was app._canonical_filed_path."""
    return str(canonical_dest_for_trx(trx, ext))


def filed_path_for_scored(trx, ext: str) -> Path:
    """Pipeline scored-dict shape (trx_owner / trx_date / trx_vendor /
    trx_l1 / trx_l2). Extension verbatim — the pipeline lowercases it at
    every call site before calling, and the original filed_path_for did
    not re-lowercase. Was process_receipts.filed_path_for."""
    return canonical_filed_path(
        trx["trx_owner"], trx["trx_date"], trx["trx_vendor"],
        trx["trx_l1"], trx["trx_l2"], ext)


def move_with_collision_suffix(src, dest) -> Path:
    """Move src to dest, creating dest's parent dirs. Handles same-name
    collisions by appending a " (2)", " (3)" counter; if dest already
    carries a counter, counting continues from it.

    2026-07-03 fixes (carried over): (a) src==dest is a no-op instead of
    renaming the file onto itself as a "collision" — including when src
    already sits at a counter-suffixed name; (b) parenthesized counter —
    the old bare " 2" regex treated any vendor name ending in digits as
    a counter ("Studio 54" → "Studio 55").

    Accepts str or Path; returns the final destination as a Path
    (app.py's wrapper str()s it)."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and src.resolve() == dest.resolve():
        return dest  # already exactly where it belongs
    m = re.match(r"^(.*) \((\d+)\)$", dest.stem)
    base    = m.group(1) if m else dest.stem
    counter = (int(m.group(2)) + 1) if m else 2
    final = dest
    while final.exists():
        if src.resolve() == final.resolve():
            return final
        final = dest.with_name(f"{base} ({counter}){dest.suffix}")
        counter += 1
    shutil.move(str(src), str(final))
    return final
