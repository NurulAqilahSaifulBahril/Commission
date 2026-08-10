# Using the Commission Portal

How to read and work the reports once the Portal is installed.
For installing and updating, see [Install & Update](install-and-update.md).

## The report screen

### The sidebar — set your scope first

Everything on screen answers to these, so set them before reading any numbers:

| Control | What it does |
|---------|--------------|
| **Scope** | The year |
| **Reporting Period** | The month being reported |
| **Commission Section** | Which report you are looking at (see below) |
| **Data** | Rates, rules and agent roles (see *Data page*) |
| **Account** | Who you are logged in as, Logout, and Admin for admins |

### The five report sections

- **Basic & Net Floor Price Commission** — the main report
- **ANP Commission** — internal agents only; the Agent Type selector locks to
  Internal while you are on it
- **EGA/ESA Award**
- **Production Bonus**
- **Monthly Contest**

### Filters across the top

| Filter | What it does |
|--------|--------------|
| **Agent Type** | All Agents / Internal Agents / Outsource Agents |
| **Search Agent or Customer** | Type any part of a name to narrow the table |
| **Payout Stage** | **July onwards only** — see below |
| **Clear** | Resets the filters |

Searching narrows the table and the Total Agents / Total Customers counts, but
**not** the money cards at the top — those always show the month's full totals,
so searching for one customer can never look like the month's commission dropped.

### Payout Stage (July 2026 onwards)

From July 2026, a commission is paid in two stages instead of all at once:

- an **advance of RM 300** as soon as the invoice reaches the first payment
  trigger, then
- the **balance** once it reaches 75% paid.

The **Payout Stage** dropdown filters the report to one stage:

- **All Payouts** — everything (default)
- **Advance RM 300** — only rows with an advance payable this month
- **Balance Payout** — only rows whose balance is payable this month

An invoice that hit both milestones in the same month shows under either one,
because both tranches pay that month. The **Summary Agent Commission** table
follows the same filter and lists only agents who still have a matching row.

The dropdown is hidden before July, because earlier invoices pay in full at 100%
and there is nothing to split.

### Reading the Basic & NFP report

Two tables:

- **Summary Agent Commission** — one block per agent, their totals for the month
- **Summary Agent by Customer** — the detail behind it, two rows per customer:
  one **Basic Commission**, one **Net Floor Price Commission**

Cells tell you *why* a figure is missing rather than just showing a blank:

| You see | It means |
|---------|----------|
| `pending` / `pending full payment` | The invoice exists but has not reached the payment milestone yet |
| `invoice before july` | Pre-July invoice — it pays in full at 100%, it has no RM 300 tranche |
| `invoice before Oct 25` | Predates the Net Floor Price scheme |
| `JinkoSolar package not included` | No panel data, so Net Floor Price does not apply |
| `TBC with Finance` | Needs a Net Floor Price before the commission can be worked out |
| `-` | Genuinely nothing |

### Special cases

When a customer needs a one-off adjustment:

- **Add Special Case Customer** (button above the table) adds a customer who is
  not in the month's report at all.
- Clicking an existing row's special-case action adjusts that row.

The adjusted figure appears as a red **New Basic Commission** / **New Net Floor
Price Commission** row directly under the original, so the before and after sit
side by side. Special cases are saved centrally — everyone sees them, and they
survive a refresh.

### Downloading the month's pack

Top-right of the report:

- **Download PDF** — the presentation pack
- **Download Excel** — the workbook
- **Sync Data** — pulls fresh data from the company database

Both downloads follow the Year and Reporting Period in the sidebar.

### The Data page

**Data Management → Data** in the sidebar. This is the **source of truth** for
how commission is calculated — the reports read it directly, so an edit here
changes the numbers:

- **Basic Commission / Net Floor Price rates** — the rate per agent type, role,
  property type and month, plus each rate's **payout condition** (pay in full at
  100%, or the multi-stage advance-then-balance rule).
- **Agent Roles & Hierarchy** — each agent's full name, type (Internal /
  Outsource), role, and who they report to.

Two things to know before editing:

1. **Every row is dated.** A rate applies from its effective month (or across a
   month range). Changing an old row re-prices months that may already have been
   paid — add a new row for the new month instead.
2. **Saving rebuilds the reports.** The figures refresh shortly after; give it a
   moment rather than editing again.

### If numbers look stale

Computed figures cache for about five minutes per machine. If a colleague just
changed a rate, wait a few minutes or press **Sync Data**. Raw invoice data is
always live — only the calculated view is cached.

---

## Getting help

| Problem | What to do |
|---------|-----------|
| "Token expired" | Ask IT for a new `PG_PROXY_TOKEN` and update `.env` |
| Portal will not open | Check Python is installed; re-run **Setup Environment.bat** in the install folder |
| Update failed | Send `8. Web Dashboard\dashboard.log` to IT |
| A number looks wrong | Check the Data page rate and its effective month first, then raise it with Finance |
