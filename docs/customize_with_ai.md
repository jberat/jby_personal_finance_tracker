# Customizing PFT with an AI Assistant

Personal Financial Tracker is designed to be reshaped around *your* finances, and the intended way to do that is to have an AI assistant make the code changes for you. You don't need to read or write Python. You need to know what you want, hand the AI the right files, and check the result.

This guide is a set of prompting recipes. Each one is a ready-to-copy block you paste into an AI conversation, with blanks to fill in.

## Rule zero: back up before every recipe

Before you let an AI touch the code, make a copy of the whole app folder — code, `finance.db`, receipts, everything. On macOS, right-click the folder and Duplicate; on Windows, copy-paste it in Explorer. It takes ten seconds and it means the worst-case outcome of any experiment is "restore the copy."

Even better: run the recipe *on the copy* first. Point the AI at the duplicated folder, let it make changes there, start that copy of the app, and confirm it works before repeating the change on (or promoting the copy to) your real install. AI assistants are good at this kind of work, but they are not infallible, and this is your financial ledger.

Every prompt block below includes a line reminding the AI of this. Don't delete it.

---

## Recipe 1: Add your bank (a new import source)

PFT ships with importers for Chase checking, Chase credit card, and Venmo statements. If your bank isn't one of those, you'll have the AI build an importer from a sample of your bank's CSV. This is the most common customization and it works remarkably well, because bank CSVs are simple and the app's importer pattern is deliberately uniform.

**First, check whether you need this recipe at all.** Adding the *account itself* never requires AI or code: Docs & Settings → Accounts in the app adds, renames, and deactivates accounts directly. If your new account's CSV already matches a supported format (another Chase card, another Chase checking account, another Venmo-style wallet), add it there and start importing — done. This recipe is only for teaching the app a *new CSV format* it can't parse yet.

### Step 1 — Sanitize a sample CSV first (do not skip this)

You're about to hand a chunk of your bank statement to an AI service. Before you do, strip anything identifying from it:

1. Download a CSV statement from your bank and open it in a spreadsheet app or text editor.
2. Keep the **header row** exactly as-is — the AI needs the real column names.
3. Keep **10–20 real data rows** so the AI sees genuine date formats, amount signs, and description quirks.
4. In those rows, **replace anything identifying**: account numbers (change `...1234` to `...0000`), your name, other people's names in Zelle/transfer descriptions, addresses, check numbers if they bother you. Changing digits is fine; what matters is that the *format* stays identical (same length, same punctuation).
5. Save it as `sample_sanitized.csv`.

Amounts and merchant names are generally fine to leave real — they're what make the sample useful — but scramble them too if you prefer; just keep the formatting authentic.

### Step 2 — The prompt

Attach or paste three things into the AI conversation: your `sample_sanitized.csv`, the file `importers/chase_cc.py` from the app folder (this is the reference pattern), and the block below.

```text
I'm using Personal Financial Tracker, a local Flask + SQLite personal
finance app. I want to add an importer for a new bank: <BANK NAME,
e.g. "Ally Bank checking">.

Attached:
1. sample_sanitized.csv — a sanitized sample of this bank's CSV export
   (real column headers, real formats; identifying digits scrambled).
2. importers/chase_cc.py — an existing importer from the app. Follow its
   pattern exactly.

What to build:
- A new file importers/<bank_name>.py with a parse function that takes
  the CSV contents and yields one dict per row, in the SAME dict shape
  the Chase importer produces (same keys: date fields, raw description,
  cleaned vendor, amount, transaction type, category guesses, etc.).
  Match the contract precisely — the review queue, duplicate detection,
  and auto-categorization all depend on that shape.
- Keep the amount's RAW BANK SIGN as-is (the app flips signs at approval
  time, not at import time — you'll see this in the Chase importer).
- Convert dates to YYYY-MM-DD. If this bank has only one date column,
  use it for both the transaction date and post date, like the checking
  importer pattern.
- Map the bank's own category column (if it has one) to the app's
  (trx_type, l1_category, l2_category) the way chase_cc.py's classify
  step does. If there's no category column, leave categories blank and
  let the review queue handle it.
- Register the new importer in the import chooser — find where the app
  decides which parser to use for an upload (the same place chase_cc and
  chase_checking are registered) and add this bank as an option.
- I add the account itself inside the app (Docs & Settings → Accounts),
  so don't insert accounts in code — only tell me if this bank genuinely
  needs a new account TYPE.

Before writing code, tell me your reading of the sample: which column is
the date, which is the amount, what the sign convention appears to be
(are charges negative or positive?), and anything ambiguous. Ask me to
confirm before proceeding.

After building it, tell me exactly how to test: which file to import in
the app and what I should expect to see in the review queue.

Constraints: I have backed up the folder, but still — only create the new
importer file and the minimal registration change. Do not modify the
database, the schema, or any other importer.
```

### Step 3 — Test it

Restart the app, import a real (un-sanitized) statement from that bank, and look at the review queue. Check that dates look right, amounts have the right direction (charges as expenses, deposits as income), and vendor names are readable. If something's off, describe the wrongness to the AI ("all my deposits are showing as expenses") — sign-convention mixups are the most common bug and a one-line fix.

---

## Recipe 2: Add, rename, or remove categories

The starter category tree lives in `categories.py` as plain Python data — it's what seeds the `categories` table in the database. Categories are two-level: L1 (like "Food & Dining") with L2 children (like "Groceries", "Restaurants", "Takeout").

**The one thing you must know first:** transactions store their category as *plain text*, not as a reference. So if you rename "Food & Dining" to "Food" in the tree, every already-categorized transaction still says "Food & Dining" and will no longer line up with the tree. A rename therefore needs two parts: change the tree, *and* update the existing transaction rows to match (a cascade update). The prompt below handles both. Adding brand-new categories has no such complication, and removing a category just stops it appearing in dropdowns — history keeps the old label.

```text
I'm using Personal Financial Tracker, a local Flask + SQLite personal
finance app. I want to change my category tree. The tree is defined in
categories.py, which seeds the categories table in finance.db.

Changes I want:
<LIST THEM, e.g.:
- Add a new L1 "Pets" with L2s "Vet", "Food & Supplies"
- Add L2 "Coffee Shops" under existing L1 "Food & Dining"
- Rename L1 "Auto & Transport" to "Transportation"
- Rename L2 "Gas" (under Transportation) to "Fuel"
- Remove L2 "Fast Food" (fold anything in it into "Takeout")>

How to do it:
1. Edit the tree in categories.py for all additions/renames/removals.
2. IMPORTANT — for every RENAME (and any removal where I asked you to
   fold transactions into another category): existing transactions store
   the category as a plain string, so you must also run a cascade UPDATE
   on the transactions table (and any pending rows in the staging table)
   changing the old L1/L2 strings to the new ones. Show me the exact
   UPDATE statements and row counts before running them.
3. Check whether budgets or any other table also store category names as
   strings, and cascade the rename there too if so.

I have backed up the app folder AND finance.db before this session.
Before touching the database, make one more safety copy of finance.db
(e.g. finance.db.bak) yourself.

Afterwards, tell me to restart the app and what to check: the new tree
should appear in category dropdowns, and my renamed categories' history
should still show up under the new name in the Expenses Overview.
```

A useful habit: after any category surgery, open the Expenses Overview for the full year and eyeball it. If a category's history "disappeared," the cascade missed some rows — tell the AI and it will find them.

---

## Recipe 3: Teach the auto-categorizer your vendors

At import time the app does two things to each row: it cleans the raw bank description into a readable vendor name (that logic lives in `vendor_rules.py`), and it applies auto-categorization rules that map vendors to categories, so most rows arrive in the review queue already categorized correctly. Out of the box the rules are generic. The more you teach it your own merchants, the more the review queue becomes a rubber stamp.

The recipe: collect examples of transactions you keep re-categorizing by hand, then hand them to the AI.

```text
I'm using Personal Financial Tracker, a local Flask + SQLite personal
finance app. I want to add auto-categorization rules so imports come in
pre-categorized. Two files are involved:
- vendor_rules.py — cleans raw bank descriptions into canonical vendor
  names (ordered substring/fragment matching).
- The auto-categorization rules applied at import time — an ordered,
  first-match-wins ladder that maps vendor/description (and sometimes
  amount) to (trx_type, l1_category, l2_category). Find where the
  existing rules live (the import pipeline applies them right after
  parsing) and follow their exact style.

Here are my rules, written as "raw description or vendor → what I want":
<EXAMPLES, e.g.:
- Every "FOO LLC" charge → Food & Dining / Takeout
- Anything containing "SQ *BLUE BOTTLE" → vendor "Blue Bottle Coffee",
  category Food & Dining / Coffee Shops
- "CITY OF SPRINGFIELD UTIL" → Bills & Utilities / Water & Sewer
- Charges from "AMZN" over $200 → leave uncategorized (I'll review those
  by hand), under $200 → Shopping / Household>

How to do it:
1. Where a raw description needs a nicer vendor name, add a cleanup rule
   in vendor_rules.py. Use word-boundary fragment matching the way the
   existing rules do, so short fragments don't accidentally match inside
   longer words (the "APPLE" should not match "APPLEBEES" problem).
2. Add category rules to the import-time rules ladder, placed so
   more-specific rules come before general ones (first match wins).
3. My category names must EXACTLY match names in my category tree
   (categories.py / the categories table) — verify each one and tell me
   if any of my requested categories don't exist.

Note that these rules apply to FUTURE imports; they do not re-categorize
transactions already in my ledger, and that's fine.

I have backed up the app folder first. Only edit the rules — don't touch
the database or the importers.

When done, tell me how to test (re-import a statement and check the
review queue arrives pre-categorized; duplicate detection will flag the
already-approved rows, which is expected).
```

Do this once a quarter with whatever you've been hand-fixing, and the review queue gets progressively boring — which is the goal.

---

## Other things you can ask for

The same approach — back up, hand the AI the relevant file plus a clear description, test on a copy — works for nearly anything:

- New receipt extractors for vendors whose receipts don't parse well (the receipts engine has an ordered extractor list built for exactly this — point the AI at the existing extractors as the pattern).
- Tweaks to what counts as "skippable" noise at import (e.g. your bank's credit-card autopay lines).
- Cosmetic changes to pages, extra columns in exports, and so on.

## General advice for AI-assisted changes

- **Backup first, every time.** The folder plus `finance.db`. This is the whole safety model.
- **Test on a copy when the change touches the database.** Code-only changes (a new importer, new rules) are low-risk; anything that runs UPDATE statements against `finance.db` deserves a dress rehearsal.
- **One recipe per conversation.** Don't ask for a new bank importer, a category overhaul, and rules changes in one go. Small, separately testable changes are how you stay in control.
- **Make the AI explain before it acts.** The prompts above ask the AI to state its plan (or its reading of your CSV) and wait for your confirmation. Keep that pattern — it catches misunderstandings while they're still free.
- **If a change goes sideways,** stop, restore your backup copy, and start a fresh conversation that includes what went wrong. Never let an AI "dig out" of a broken state by making more and more changes to your only copy.
