# Data Page — Unified Rates & Rules Table

Plan for merging the Basic Commission "Rates" grid and "Rules" grid on `/data`
(Data Details edit view + Data read view) into one combined table. Written
2026-07-22, not yet implemented — build this when told to proceed.

## Goal

Replace the current two-grid layout (Rates table, Rules table) with a single
table that can express a plain rate row, an override row, a profit-sharing
row, or a payout/advance rule row — all in the same column set, on both the
edit page (Data Details) and the read page (Data).

## Column list (final, confirmed with user)

| Column | Meaning |
|---|---|
| Payment Date | Month this entry takes effect from (same mechanic as today's "Month" — effective-dated, copy-on-write per month) |
| Agent Type | Internal / Outsource / blank (= both) |
| Role | Role dropdown, or blank |
| Agent Name | Optional named agent (Sunny, Safwan, Gan Lai Soon, Kent…) — blank = applies to the whole role |
| Rate (%) | Base commission rate for this row |
| Override Rate (%) | Senior-override-style rate (e.g. 0.25% to reporting Senior on downline sales) |
| Profit Sharing Rate (%) | Base rate + sharing for Factory/NGO/Government deals |
| Properties Type | Residential / Commercial / Shop Lot / Factory / NGO Project / Government Project / blank |
| Payment ≥ (%) | Payment trigger threshold for a Payout/Advance rule row |
| Invoice Date | Month from which this rule applies to invoices (blank = all invoice dates) |
| Rule Type | Dropdown: **Advance**, **Payout** (blank = this is a plain rate row, not a rule) |
| Amount (RM) | Only meaningful when Rule Type = Advance; auto-enabled for Advance rows, disabled otherwise |

A row uses only the columns relevant to what it represents; the rest stay
blank. This intentionally trades "no blank cells" for "one table, one mental
model" — confirmed acceptable to the user.

## Row types / examples

| Row | Payment Date | Agent Type | Role | Agent | Rate% | Override% | Profit Sharing% | Property | Pay≥% | Invoice Date | Rule Type | Amount RM |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Senior rate | Jun 2026 | Internal | Senior | | 4.25 | | | | | | | |
| Sunny's SDM rate | Aug 2026 | Internal | SDM | Sunny | 5.00 | | | | | | | |
| Safwan (Factory, both agent types) | Jan 2026 | *(blank)* | | Safwan | 0.5 | | | Factory | 100 | | Payout | |
| Gan Lai Soon (Outsource, Factory, OSA/OUM only) | Jan 2026 | Outsource | | Gan Lai Soon | 0.75 | | | Factory | 100 | | Payout | |
| Referral Fee (both agent types) | Jan 2026 | *(blank)* | | | 2.00 | | | | 100 | | Payout | |
| Senior override | — | Internal | Senior | | | 0.25 | | | | | | |
| Factory profit sharing base | Jan 2026 | | | | | | 2 | Factory | | | | |
| Advance (RM300 @ ≥5%) | Jul 2026 | | | | | | | | 5 | Jul 2026 | Advance | 300 |
| Payout balance (≥75%) | Jul 2026 | | | | | | | | 75 | Jul 2026 | Payout | |
| Payout pre-July (100%) | Jan 2026 | | | | | | | | 100 | *(blank)* | Payout | |

**Tradeoff resolved (2026-07-23, user decision):** the earlier draft accepted
losing Safwan / Gan Lai Soon / Referral Fee's "paid only at 100% payment, no
partial advance" condition from the UI. The user rejected that — those
conditions may well change in future, so they must stay **visible and
editable**, not buried in calculation code.

Under Option A this costs nothing: one physical row carries both the rate
columns and the payment-trigger columns, so those three rows are written as
rate + `Payment ≥ 100%` + `Rule Type = Payout` in a single row (see the
examples table above). No information is lost in the merge.

This is the concrete reason Option A wins over Option B: with two physical
tables a row is either a rate or a rule, and these three rows are both.

**Rule Type is scoped to Basic Commission's own payout mechanics only:**
- **Advance** = the 1st-payout cap (today's RM300 at ≥5% payment)
- **Payout** = the normal milestone payout condition (pre-July 100% rule,
  and the July+ ≥75% balance rule)

Both only make sense inside the Basic & Net Floor Price Commission section;
other commission types (NFP, ANP, etc.) won't offer this dropdown.

## Storage decision — **Option A, confirmed 2026-07-23**

**Option A — merge into one physical DB table.** *(chosen)*
Combine `basic_rates` and `rule_settings` into a single table with all the
columns above. Cleanest long-term, but a real migration: existing rows in
both tables need to be transformed and re-inserted, and every place that
reads `db.list_basic_rates()` / `db.list_rule_settings()` needs updating
(rate resolution in `basic_commission_rates.py`, the `/resolved` API, the
NFP tier code path, audit logging). Higher risk, one bigger step.

**Option B — keep two DB tables, merge only the display.** *(rejected)*
`basic_rates` and `rule_settings` stay as they are (both already have most
of these columns after today's work — `basic_rates` needs `override_rate_pct`
and `profit_sharing_rate_pct` added; `rule_settings` already has
`trigger_pct`, `invoice_date_from`, `property_type`). The edit grid and read
view render rows from both tables interleaved into one visual table, and
route each save back to whichever API the row belongs to (rate vs rule) —
invisible to the user. Lower risk, no migration of existing rows, can be
verified incrementally like every other change made today.

Option B was rejected because a row in it must be *either* a rate or a rule,
which cannot express the Safwan / GLS / Referral rows (rate **and** payment
condition) — see the tradeoff note above.

Option A carries real migration risk, so it is done the same careful way as
every other change in this build: additive schema first, dual-read while both
old tables still exist, zero-diff verification against the live engine, and
only then drop the old tables. Never risk live commission numbers on an
unverified schema change.

## Build status — structure merged 2026-07-23, table left EMPTY

Per user instruction the merge was built **without migrating any data**: the
unified table ships empty and gets populated by hand through the Data page, so
the rate/rule → calculation link can be observed directly. That deliberately
drops the backfill step and the drop-old-tables step from the checklist below;
`basic_rates` and `rule_settings` keep their rows and stay the fallback.

Precedence now in force: **unified row → legacy table row → cached sheet CSV →
hardcoded default.** While `commission_rates` is empty every lookup falls
through to exactly today's behavior (verified — see below).

## Implementation checklist

- [x] Create the unified table `commission_rates` in `db.py`'s schema block,
      with the full column set plus `label` and `condition` (the latter keeps
      the NFP tier lineage working) and the standard audit columns. Index
      `idx_commission_rates_lookup` added.
- [~] Backfill — **deliberately skipped** (user instruction: merge the
      structure, enter the data by hand). If a backfill is ever wanted, it
      must be idempotent and preserve `created_by` / `created_at`.
- [~] Merging Safwan / GLS / Referral into single rows — now a data-entry
      task, not a migration. Shape: rate + `Payment ≥ 100` + Rule Type
      `Payout` (+ `Properties Type = Factory` for Safwan / GLS).
- [x] `db.list_commission_rates()` / `db.save_commission_rates()` added
      next to the legacy helpers, which are untouched.
- [x] `/api/commission-rates` (GET/POST) added; POST is admin-only, clears
      the rates cache and the commission cache exactly like the old endpoints.
- [x] `/resolved` reads the unified table too: it contributes rate combos and
      payout rules, and a unified rule outranks a legacy rule of the same key.
- [x] `data.html`: one table, one column set, no "Rates" / "Rules" headers.
- [x] `data.js`: single `addUnifiedRow()` row-builder; Amount (RM) enables
      only for Rule Type = Advance; Rule Type / Amount columns appear only in
      the Basic Commission scope.
- [x] `collectEntries()` posts one payload to `/api/commission-rates`;
      copy-on-write per month retained, keyed on everything that makes a row
      distinct (agent type, role, agent, property, rule type, label).
- [x] Engine link: `basic_commission_rates.py` reads `commission_rates`
      first for both rates and the advance/payout rules, then falls back.
      `property_type` is now plumbed through `get_basic_rate()` so a
      property-scoped row can match.
- [x] Verified: with the table empty, rate resolution and the RM300 cap are
      byte-identical to before; with rows inserted, the engine picks them up
      (role rate, agent-specific rate, property-scoped rate, advance amount,
      effective-dating all confirmed).
- [ ] **Not done — read (Data) page still renders the old two-section view.**
      Its rate rows and payout bullets do reflect unified entries via
      `/resolved`, but it is not yet the unified 12-column read-only table.
- [ ] Click-test the edit grid in the browser (needs a login).
- [ ] Only after the table is populated and verified: drop `basic_rates` and
      `rule_settings` and remove their helpers. Back up the SQLite file first
      — the one irreversible step. Until then they are the rollback path.

## Legacy rows cleared 2026-07-23 (user instruction)

`basic_rates` (24 rows) and `rule_settings` (6 rows) were emptied on user
instruction, before the unified table was populated. Consequences, accepted
knowingly:

- Every rate now falls through to the cached sheet CSV / hardcoded defaults
  until it is re-entered. Internal Senior currently resolves to **3.25%**
  (the Jan figure in the CSV), not the 4.25% that was in force from Jun 2026.
- The RM300 advance cap comes from the hardcoded default, not from data.
- The 6 NFP tier rates are gone from the Data page until re-entered.

Recovery, if ever needed: `dashboard.db.backup-20260723-094355` next to the
live DB, and the pre-delete state is also captured in `audit_log.before_json`
for both tables.

**Source badges now name the real source** so coverage is visible while
re-entering: `unified` (green — entered on the Data page, this is the goal),
`legacy` (amber — leftover old-table row), `sheet` / `default` (red — a
fallback is answering, the row is not yet under Data page control).

Verified round-trip on localhost: POST to `/api/commission-rates` → `/resolved`
reports the rate with source `unified` → `get_basic_rate()` returns it.

## BUILT 2026-07-23: Roles table (`agent_roles`)

Table, `/api/agent-roles` (GET/POST), `/api/agent-roles/seed`, and the "Agent
Roles & Hierarchy" card on the Data page. All four `get_reporting_senior()`
copies now consult it before their hardcoded maps, so the dashboard and the PDF
packs can no longer disagree about the hierarchy.

**Precedence is tri-state, and the distinction matters:** listed with a Reports
To → that senior; listed with Reports To *blank* → reports to NOBODY; not
listed → fall back to the legacy name matching. Collapsing the middle case into
the last one would silently re-apply the `startswith('j')` guess to a
top-level Senior you had deliberately given no senior. Verified across the
engine and the pack.

Seeding pulls only agents whose `agent_type` is internal/outsource — 28 rows,
11 with a branch. Without that filter it returns the whole staff directory
(184 rows, "ADMIN" included). The seed never overwrites an existing row and a
re-run adds 0.

Left to do: fill in Role and Reports To for the 28 agents, then the hardcoded
maps can be deleted from all four files.

## Original proposal (raised by user 2026-07-23)

Motivation: the Data grid has no way to say "this Executive reports to that
Senior", so the override rate cannot be attributed. Today that mapping is
**hardcoded string matching**, duplicated across four files
(`full_internal_basic_commission.py:209`, `build_commission_pack.py:648`,
`build_finance_commission_pack.py:624`,
`Outsource_build_finance_commission_pack.py:598`). It includes
`if n.startswith('j') ... return "MARTIN HING"` — every new agent whose name
begins with J is silently assigned to Martin Hing.

What Postgres can and cannot supply (checked against the live schema):

| Column | Source | Status |
|---|---|---|
| Agent Name | `user`/`agent` union on `bubble_id` | extractable |
| Agent Type | `user.agent_type` | extractable |
| Branch | `user.main_department` — "JB Sales Branch", "Kluang Sales Branch", "Seremban Sales Branch" | extractable (16/18 internal, 9/10 outsource populated) |
| Role | `user.outsource_role` has only 2 rows filled (OSM, OUM); internal roles absent entirely | **must be entered by hand** |
| Reports to | `user.outsource_parent_user_id` is 100% empty | **must be entered by hand** |

So the table is worth building precisely because Role and Reports-to exist
nowhere else. Proposed columns — the user's four plus two:

**Month · Agent Name · Agent Type · Role · Reports To · Branch**

Month keeps the same effective-dating as the rates table, so a promotion or a
team move is a new row rather than an overwrite, and a recalculation of an old
month uses the hierarchy as it stood then.

Build shape (same approach that worked for the rates merge): new `agent_roles`
table, seeded from Postgres with Agent Name / Agent Type / Branch and blank
Role / Reports To for hand-filling; then repoint `get_reporting_senior()` in
all four files at it, keeping the hardcoded map as fallback until the table is
complete. Not started — awaiting go-ahead.

## Known gap found while wiring (2026-07-23)

`full_internal_basic_commission.py` handles Factory deals in its own branch —
it uses a hardcoded `0.02` plus the factory profit-sharing map and never calls
`get_basic_rate()`. So a row entered with **Properties Type = Factory** is
stored and resolvable, but that branch will not consult it. The lookup itself
now accepts and honours `property_type`; what is missing is the engine calling
it for Factory deals. Wiring that changes live Factory numbers, so it was left
alone — decide separately whether Factory rates should come from the Data page.

## Decisions made 2026-07-23

1. **Storage: Option A** — one physical `commission_rates` table.
2. **Safwan / GLS / Referral conditions stay in the UI.** Their payout
   condition may change in future, so it must be editable in the table, not
   hardcoded in calculation code. Option A makes this free.

## Open questions still to confirm with user

1. Should "Advance" / "Payout" Rule Type ever be needed outside Basic
   Commission (e.g. a future Production Bonus advance)? If yes, the
   dropdown options list needs to stay extensible rather than hardcoded to
   two values. (Leaning yes — store `rule_type` as free TEXT and drive the
   dropdown from a list, given decision 2's "conditions will change" logic.)
