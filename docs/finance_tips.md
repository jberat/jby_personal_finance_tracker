# Finance Tips: Patterns the Tool Is Built For

PFT is deliberately unopinionated — it tracks what you feed it. But some personal-finance patterns fit its features unusually well, and knowing them turns the app from a record-keeper into leverage. None of this is financial or tax advice; it's a tour of what the machinery is good at.

## The HSA reimburse-later play

If you have a Health Savings Account, there's a well-known move: when a medical expense comes up, **pay it out of pocket today and leave the HSA money invested**. HSA rules (as of this writing — verify with your own tax advisor) don't require you to reimburse yourself in the same year the expense happened. You can let the receipt sit for years while the HSA balance compounds tax-free, then reimburse yourself for the old expense whenever you actually want the cash.

The entire play lives or dies on one thing: **proof, years later, that the expense was real, medical, and unreimbursed**. That's exactly what PFT's receipts pipeline plus tags gives you:

1. When a qualifying medical expense hits your ledger, tag it — something like `HSA-eligible`.
2. Drop the receipt into the receipts inbox. It gets matched, filed into the year/category/month tree, and permanently attached to the transaction (see [receipts_guide.md](receipts_guide.md)).
3. Years later, filter transactions by the `HSA-eligible` tag: there's your reimbursable balance, every line with its receipt one click away. Reimburse yourself, then re-tag the reimbursed rows (e.g. change to `HSA-reimbursed`) so the running list stays honest.

Without a system, this play collapses under lost receipts. With one, it's ten seconds per expense.

## Split vs. link: two tools, two different problems

These get confused, so here's the clean distinction.

**Split when one charge covers genuinely different categories.** A single $412 Costco charge that was really $310 of groceries and $102 of electronics is *one* payment but *two* kinds of spending. Splitting breaks the transaction into child parts — each with its own category, each independently reportable — with the app enforcing that the parts sum exactly to the original. Your Groceries number and your Electronics number both end up true. Classic split candidates: warehouse-store runs, a hotel bill that's lodging plus meals, an Amazon order spanning categories.

**Link when two separate transactions belong together.** A $89 purchase and the $89 refund three weeks later are *two* rows that tell one story. Linking ties them so you can see the pair (and they can share one receipt), while each remains its own transaction with its own date and amount. Same for a deposit and its correction, or a charge and a partial refund. Don't try to "fix" a refunded purchase by deleting rows — link the pair, and the categories net themselves out honestly.

Rule of thumb: **one transaction, many categories → split. Many transactions, one story → link.**

## Tags: the cross-category dimension

Categories answer "what kind of spending was this?" Tags answer every *other* question. Because a tag can sit on any transaction regardless of category, tags are how you track things that cut across your tree:

- A **`Vacation 2027`** tag on flights (Travel), restaurants (Food & Dining), gear (Shopping), and the dog sitter (Pets) gives you the true all-in cost of the trip — a number no category view can produce.
- A **project tag** (`Kitchen Remodel`) does the same for a renovation scattered across contractors, hardware stores, and permits.
- A **person tag** works for tracking spending on a kid, or shared expenses to settle with a partner.

Search supports tag names, so pulling up a tagged set is trivial. The discipline that makes tags work: create few, name them consistently, and tag at review time while you still remember what the charge was.

## Read the By-Vendor view for subscription creep

Once a quarter, open the **By Vendor** view and sort by total. Two things reliably fall out:

- **Subscriptions you forgot.** Recurring charges are invisible in a category view (they hide inside "Entertainment" or "Software" among one-off purchases) but unmissable in a vendor view — the same name, the same amount, every month. Anything you don't recognize or don't use gets cancelled. This single habit usually pays for the time you spend on the whole app.
- **Death by a thousand visits.** The vendor view also surfaces the merchant you visit "occasionally" that's somehow number three on the year. No single charge was notable; the total is.

## Budget only what you're actually trying to control

PFT lets you set budgets at the L1 or L2 level, with L2 budgets rolling up into their L1, and the Actuals-vs-Budget report showing where you stand. The temptation is to set a budget on everything. Resist it.

A budget line you don't care about is noise that trains you to ignore the report. Your rent doesn't need a budget — it's fixed, and a budget won't change it. Set **L2 budgets on the handful of categories where your behavior actually moves the number** — restaurants, shopping, hobbies, whatever your honest leak is — and maybe a coarse L1 budget over discretionary areas as a backstop. Three to six real budget lines you check monthly beat thirty you check never.

The rollup helps here: if you budget `Food & Dining / Restaurants` and `Food & Dining / Takeout` but not Groceries, the L1 line still shows total food spend against the sum of what you chose to cap, so you keep the big picture without pretending to control the uncontrollable.

## Small habits that compound

- **Import on a rhythm.** Monthly, when statements close, is plenty. The review queue is pleasant at 40 rows and a chore at 400.
- **Review is where the data quality happens.** Fix vendor names and categories in the queue, not in the ledger later — and when you notice yourself fixing the same vendor twice, teach the auto-categorizer instead ([customize_with_ai.md](customize_with_ai.md), Recipe 3).
- **Use notes for future-you.** A five-word note on any transaction you'll otherwise puzzle over in eleven months ("split furnace repair w/ neighbor") is the cheapest insurance in the app.
- **Trust the trash.** Deleting is soft and restorable, so don't hoard junk rows out of fear. Keep the ledger clean; the Trash page has your back.
