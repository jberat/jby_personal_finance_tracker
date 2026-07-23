# Receipts Guide

PFT includes a receipts pipeline: you drop receipt files into a folder, and the app reads them, figures out which transaction each one belongs to, asks you to confirm the uncertain ones, and files everything into a tidy folder tree. The end state is that any transaction in your ledger can show you its receipt in one click, and your receipts on disk are organized by year, category, and month instead of rotting in your Downloads folder.

This matters more than it sounds. If you ever need proof of a purchase — a warranty claim, a tax question, an HSA reimbursement years after the fact (see [finance_tips.md](finance_tips.md)) — the difference between "it's attached to the transaction" and "it's somewhere in my email" is enormous.

## The flow at a glance

1. You drop receipt PDFs and photos into the **inbox folder**.
2. You run a **scan**. The app reads each file, extracts vendor, amount, and date, and searches your ledger for the matching transaction.
3. **High-confidence matches file themselves.** Medium-confidence matches go to a review queue. No-match receipts are held as "orphans" for you to place by hand.
4. You work through the **review screen**, one receipt at a time: the receipt image on one side, the proposed transaction on the other. Confirm, reject, or pick a different transaction.
5. Confirmed receipts are **filed**: the file is moved into an organized folder tree and the transaction records the path.

## The inbox folder

The inbox is an ordinary folder inside the app's data area. Anything you drop in is fair game for the next scan: PDFs (both real PDFs and scanned-image PDFs), and photos (JPG, PNG, HEIC straight off an iPhone — HEIC is converted automatically).

Practical habits that work well:

- Emailed receipts: use the email's "print to PDF" or download the attachment, drop the PDF in.
- Paper receipts: photograph them (flat, decent light, whole receipt in frame including the total) and drop the photos in.
- Don't bother renaming files. The app names them properly when it files them.

Dropping the same file twice is harmless — the app fingerprints every file's contents, so exact duplicates are recognized and skipped rather than double-filed.

## What OCR does (and what it can't)

For files that are images — photos, and PDFs that are really just scans — the app uses OCR (optical character recognition) to turn the picture into text. Real PDFs have their text extracted directly, which is essentially perfect; OCR on photos is very good but not perfect.

From the extracted text, the app pulls out three things:

- **Vendor** — using purpose-built extractors for known vendors first, then a generic pass that takes the most plausible name line and cross-checks it against vendor names already in your ledger.
- **Amount** — the total. The generic logic hunts for the "Total" line (while dodging "Subtotal" and similar traps) and sanity-checks with subtotal-plus-tax arithmetic where it can.
- **Date** — tolerant of most common formats.

## How matching works

With vendor, amount, and date in hand, the app searches your active transactions for candidates within a window (roughly a month either side of the receipt date) and scores each candidate on three signals:

- **Amount** — does it match, within a small tolerance? For restaurant-type receipts the matcher also allows a tip-sized amount *above* the printed total, since the card charge includes the tip the receipt doesn't show.
- **Date** — within a few days?
- **Vendor** — do the names overlap once both are cleaned up?

The score puts each receipt into a confidence tier:

- **High** (all three signals agree, or a single candidate nails amount and date exactly) — eligible for automatic filing, no review needed.
- **Medium** (two signals, or a tip-adjusted amount match) — goes to the **review queue** for a human yes/no.
- **Low or none** — becomes an **orphan**: the app is not going to guess, and waits for you to place it manually.

One more case: if the app finds a high-confidence match but that transaction *already has a receipt filed*, the new file is flagged as a **suspected duplicate** rather than filed on top — you decide whether it's a second copy to discard or a genuinely separate receipt.

Orphans and queued receipts aren't stuck forever: every scan re-checks them against the current ledger, so a receipt that arrived before its transaction (you imported the bank statement a week later) matches up on its own once the transaction exists.

## The review screen

The review screen shows one receipt at a time: a large preview of the file next to the candidate transaction (or candidates). Your actions:

- **Confirm & file** — yes, that's the one. The receipt is linked and filed immediately.
- **Reject** — not that transaction. The receipt becomes an orphan for manual placement.
- **Pick a different transaction** — search your ledger and attach the receipt to the right row yourself.
- **Discard duplicate** — for suspected duplicates that really are second copies.
- **Not a receipt** — for the stray screenshot or menu photo that wandered into the inbox. Moved to the receipts trash folder (nothing is ever hard-deleted from disk by the app; the trash view lets you restore or truly delete later).

A few minutes on this screen after each scan is the entire ongoing cost of the system.

## Where files end up

Filed receipts move out of the inbox into a folder tree organized as:

```
receipts / filed / <year> / <L1 category> / <L2 category> / <month> / "YYMMDD Vendor.pdf"
```

The year and month come from the *transaction's* date (not the file's), and the categories from the transaction's L1/L2 — so the filing mirrors your ledger. The file is renamed to a compact `date + vendor` name, and name collisions get a numeric suffix rather than overwriting anything.

The transaction stores the filed path, which is what powers the receipt link in list views. One receipt can serve several transactions — if you split a transaction, or link a purchase to its refund, the whole family points at the same physical file. No copies are made.

## The no-receipt-needed flag

Plenty of transactions will never have a receipt and shouldn't nag you about it — bank fees, interest, peer-to-peer transfers, that $4 coffee. Mark these with the **no-receipt-needed** flag and they drop out of the "missing receipts" filter, keeping that view an honest to-do list of receipts you actually intend to chase.

## Honest limitations

- **Photographed receipts sometimes OCR the wrong total.** Crumpled paper, glare, or a thermal-paper fade can make the extractor grab a subtotal or an item price instead of the total. The confidence tiers exist precisely for this — a wrong amount usually lands the receipt in review or orphans rather than misfiled — but it's the main reason review exists at all. Better photos mean fewer orphans.
- **Foreign-currency receipts need manual matching.** A receipt in euros can't be tied to the USD amount your card was actually charged, so foreign-currency receipts are never auto-filed — at best they're queued for review on date proximity, and often you'll place them by hand. Expect this for travel.
- **Handwritten and very unusual receipts** may extract nothing useful and go straight to orphans. You can still attach them manually; the pipeline just can't help.
- **It matches against your ledger.** If the transaction hasn't been imported and approved yet, the receipt waits as an orphan. Import your statements first, scan receipts second, and things click together.
