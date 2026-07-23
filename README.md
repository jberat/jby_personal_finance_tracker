# Personal Financial Tracker

A personal finance tracker that runs entirely on your own computer. Import your bank statements, review and categorize every transaction, attach receipts, set budgets, reconcile paychecks and credit-card statements, estimate your taxes, and export clean Excel reports — without ever sending your financial data to anyone.

## Your data never leaves your machine

This is the whole point, so it goes first.

Personal Financial Tracker (PFT) is a local web app: a small server on your own computer, used in your browser at `http://127.0.0.1:5005` — an address only your machine can reach. Unplug your internet and it works exactly the same. There is no cloud, no account, no signup, no bank-credential linking, no analytics, no telemetry — none of it exists to opt out of.

Your entire ledger is one SQLite file (`finance.db`) in a folder you control: you can see it, copy it, and back it up by dragging it somewhere. Your receipts are ordinary files in ordinary folders. You feed the app yourself — download CSV statements from your bank's website and import them; nothing ever connects to your bank on your behalf. The only way data leaves your machine is you exporting or copying it.

The app is protected by a login password, so someone wandering past your open laptop can't browse your finances. A fresh install uses the temporary password `changeme` — change it right away in Docs & Settings → Security inside the app.

## Who this is for

A lot of people track their spending — or their whole financial life — in a spreadsheet. This is meant to replace that spreadsheet, or at least be the starting point you reshape (with an AI agent's help) until it fits your exact needs.

Smart people who want real control over their money data and are comfortable asking an AI assistant for help with anything technical. You do not need to be a developer. The documentation is written so that the fiddly parts — installing, adding support for your specific bank, customizing categories — are things you hand to an AI with a ready-made prompt. The complete guide, including those prompts, is **[docs/handbook.html](docs/handbook.html)**.

## What it does

**The core loop.** Download a CSV statement from your bank. Import it — click to browse or just drag the CSV onto the import page. The app automatically detects duplicates (so re-importing an overlapping statement is harmless), cleans up messy vendor names (`TST* JOES PIZZA #1234` becomes `Joes Pizza`), auto-flags noise like credit-card autopay lines, and takes a first pass at categorizing each row. Everything lands in a review queue where you approve, edit, or discard each transaction. Approved rows become your ledger. Nothing enters your books without your sign-off.

**Importers included.** Chase checking CSV, Chase credit-card CSV, and Venmo statements (venmo.com → Statements → download the CSV) work out of the box. If you bank somewhere else, there's a prompting recipe for having an AI assistant build an importer for your bank in the handbook (also in [docs/customize_with_ai.md](docs/customize_with_ai.md)) — it typically takes a few minutes. Import errors are loud and specific: the app tells you when a file doesn't look like the right export type instead of failing silently.

**Venmo and P2P done right.** Your bank statement only shows Venmo as lump transfers — a cashout that bundles ten individual payments, while balance-funded payments never touch the bank at all. So PFT imports the Venmo statement itself: every individual payment arrives with the counterparty as the vendor and the note attached, bank-side "VENMO" lump rows are auto-segregated so nothing counts twice, and Venmo-to-bank cashouts come in as neutral transfers. The same pattern extends to other P2P/wallet apps — see the handbook's "Venmo & P2P payments" section for the double-counting rule and why Zelle is different.

**Accounts managed in-app.** Add, rename, and deactivate accounts in Docs & Settings → Accounts — no code editing. Deactivating hides an account from pickers while keeping all its history; there's deliberately no delete, because transactions reference their account forever.

**Two-level categories.** Every transaction gets a top-level category (L1, like "Food & Dining") and a subcategory (L2, like "Groceries"). The app ships with a sensible generic starter tree, and the whole thing is customizable.

**The pages.**

- **Dashboard** — year-to-date cards, recent activity, top categories at a glance.
- **Expenses Overview** — a category-by-month pivot table; click any L1 row to see its L2 breakdown.
- **Expenses Transactions** — the full filterable list (by period, account, category, search).
- **Income Overview and Income Transactions** — the same views for money coming in.
- **By Vendor** — spending grouped by merchant. Great for spotting subscription creep.
- **Review Queue** — where imported rows wait for your approval.
- **Trash** — a recycle bin. Deleting a transaction is always reversible.

**Transaction tools.** Split one transaction into parts (a warehouse-store run into groceries plus electronics — the parts must sum to the original). Link related transactions (a purchase and its refund). Add tags, notes, and receipt attachments. Amounts and categories are click-to-edit right in the list — changes save automatically.

**Receipts pipeline.** Drop receipt PDFs or photos into an inbox folder. The app reads them (OCR for photos and scans), extracts the vendor and amount, and matches them to your transactions. Confident matches file themselves; borderline ones go to a one-at-a-time review screen. Filed receipts land in an organized folder tree (year / category / month) and the transaction remembers where its receipt lives. Full details in [docs/receipts_guide.md](docs/receipts_guide.md). Honest expectations: this is the tool's weakest flow — OCR on photos is hit-or-miss, so expect some receipts to end up orphaned or matched wrong, especially when the transaction they belong to hasn't been imported yet. The review screen exists precisely to catch those.

**Budgets.** Set budget amounts at the L1 or L2 level (L2 budgets roll up into their L1) in Docs & Settings → Assumptions → Budget Values, then check the Actuals-vs-Budget report — with an "am I on pace?" view and a "how much room is left?" view — to see how you're tracking.

**Payroll tool.** Your bank only shows the *net* direct deposit; this tool books each paycheck at *gross*, splitting the deposit into full wages, offsetting lines for taxes withheld and pre-tax health deductions, and a *transfer* for your 401k/retirement contribution (your own savings, not income lost) that can sync straight into the Investments module as a contribution — so your income pages show what you actually earned and where a third of it went. Two paths: upload a Gusto Payroll Journal Report CSV, or type numbers off any paystub (per paycheck, or a single year-to-date true-up). Everything previews first and can be undone.

**Tax Estimator.** A six-step walkthrough that estimates your 2026 federal and state income taxes from a handful of numbers — brackets for all filing statuses, capital-gains stacking, self-employment tax, and a payments section that turns the estimate into "what you still owe" with a simple quarterly split. Entirely stateless: nothing you type is saved. **States with full or approximate support:** no-income-tax states (AK, FL, NV, SD, TN, TX, WA, WY, plus NH) and flat-tax approximations for AZ, CO, GA, ID, IL, IN, KY, MI, MS, NC, PA, UT — more coming. Graduated-bracket states still get the full federal estimate. It is an estimate, not tax advice; the tool itself lists every simplification it makes.

**Reconcile Card.** A monthly statement-reconciliation wizard for credit cards: pick an open card payment, see exactly which charges it settled, prove the math to the penny, and diagnose in-transit charges (a carried balance is diagnosed, not an error). Set each card's statement close day on Docs & Settings → Accounts first — it drives statement-date assignment and the cycle math. Nothing writes until the residual is $0.00, and it's unwindable.

**Excel exports.** Every overview and transactions page has an Export button that produces a formatted `.xlsx` file. A popup lets you choose the date range, date basis, L1-only or L1+L2 detail, and whether to include zero-dollar categories.

**Data Cleanup.** A one-page review of vendor-name messes, tag duplicates, missing dates, duplicate suspects, category problems, and missing receipts — fixed one item at a time.

**Investments tracker (experimental).** An optional, more advanced module that tracks investment contributions, withdrawals, moves between accounts, and value snapshots using a lot-based engine, and computes both money-weighted (XIRR) and time-weighted (TWR) returns. It works, but it operates largely independently of the rest of the app and is the least battle-tested part — treat it as a bonus, not the core product.

## Quickstart

Requires Python 3.10+. The fastest path is to hand the install to an AI assistant — there's a copy-paste prompt for exactly that in the handbook and in [docs/install_guide.md](docs/install_guide.md). Doing it by hand looks like this:

```bash
git clone https://github.com/jberat/jby_personal_finance_tracker.git personal-financial-tracker
cd personal-financial-tracker
pip install -r requirements.txt
python3 app.py
```

Then open **http://127.0.0.1:5005** in your browser. On first run the app creates an empty database and seeds the starter categories and three placeholder accounts. Log in with the temporary password `changeme`, then change it in Docs & Settings → Security. (On macOS/Linux, `./start.sh` starts the app in the background and `./restart.sh` does a hard restart.)

## Try it with sample data

The [`sample_data/`](sample_data/) folder contains three fully synthetic statements — a checking CSV, a credit-card CSV, and a Venmo CSV — so you can drive the whole pipeline before touching your real data:

1. Go to **Tools → Import → CSV Upload**.
2. Import `sample_data/sample_checking.csv` against **My Checking**, `sample_creditcard.csv` against **My Credit Card**, and `sample_venmo.csv` against **Venmo**.
3. Work through the **Review Queue** (approve rows), then look at the Dashboard and the Expenses/Income Overviews.

Step-by-step version with screenshots of what to expect: the handbook's "Try it with sample data" section. When you're done playing, each import's **Undo** button (on the Import page) removes it completely.

## Documentation

- **[docs/handbook.html](docs/handbook.html)** — **the definitive guide.** A single self-contained page (works offline, straight from the file) covering install, first launch, every tool, the AI-customization prompts, troubleshooting, and backups. If you read one thing, read this.
- [docs/install_guide.md](docs/install_guide.md) — installation, AI-assisted and manual, macOS and Windows.
- [docs/customize_with_ai.md](docs/customize_with_ai.md) — prompting recipes for adding your bank, reshaping categories, and teaching the auto-categorizer your vendors.
- [docs/receipts_guide.md](docs/receipts_guide.md) — the receipts pipeline, end to end.
- [docs/finance_tips.md](docs/finance_tips.md) — practical patterns the tool supports well, from the HSA reimburse-later play to taming subscriptions.

## A note on backups

Everything lives in one folder: the code, `finance.db`, your archived import CSVs, and your receipts tree. Copy that folder somewhere safe on a schedule you trust — that's the entire backup story. Before letting an AI assistant modify any of the code, copy the folder first; it takes ten seconds and makes every experiment reversible.

## The maker

Personal Financial Tracker is made by **JBY Advisory Inc.** 

I own and operate JBY Advisory, a contract-based independent consulting firm. I'm the sole owner + employee of the business. I built this tool to not only help with my personal finances, but also manage the business' finances. I use gusto for payroll, chase for banking + credit cards, and my 'complete' tool as a full accounting software that spans my business and my personal finances. This open-source version has many less features (and complications) to reach a wider audience, but the tool I built for my personal situation is far more robust and capable. Long story short - instead of paying for QBO that wouldnt fit my exact situation super well, I built a complete tool that makes both my personal tracking and business tracking super easy.

What the full suite has that PFT doesn't:

- **Full double-entry accounting** with automatic posting and always-verified books — trial balance and A = L + E, checked continuously.
- **Balance sheet and journal**, with an accrual/cash reporting toggle.
- **Monthly budgeting & forecasting** — per-line worksheets, finalized-month projections, and a modeled income statement.
- **Budget-vs-actuals** with projected full-year views.
- **A deep tax-planning suite** — multi-year projections, bracket breakouts, QBI sensitivity, quarterly remittance planning.
- **Payroll reconciliation including the employer side.** - I need to know how much i have paid in taxes (and will pay, through withholdings) to accurately make quarterly estimate payments. The business also needs to enumerate all the tax lines items paid as a business expense. One simple import allows me to take the banking transaction and break them into all the individual children transactions that they are composed of (e.g. employer 401K contribution, employer payroll taxes, etc.)
- **Time tracking and invoice generation.** - this is used for my actual revenue: i send out invoices for my time, and i need both the "time sheet" and an exportable PDF to send to my client.
- **Expense-sheet / reimbursement workflows.** - some of my business expenses (e.g. travel) are reimbursed by my clients, which mean the expense itself and reimbursement is excluded from my income statement. This not only helps manage the receiables, but also generates me a full PDF for the expense sheet, with receipts, that I share with my clients to get reimbursed.
- **Receipt OCR with automatic filing.**
- **Investments with benchmark comparison.**

It's tailored to my stack (Chase + Gusto) but easy to adapt to another. For a small contractor or consulting business, a system like this is an incredible value-add — and for many, it could replace QuickBooks Online.

**If your setup looks like mine and you'd like a demo of the full suite, reach out: jbyadvisory@gmail.com.** Same address for questions, ideas, or a bank importer you'd like to share.

## License and contributions

MIT License, © 2026 JBY Advisory Inc. — see [LICENSE](LICENSE). Use it, fork it, bend it to your own finances. Issues and pull requests welcome — especially new bank importers and receipt extractors that others can benefit from.
