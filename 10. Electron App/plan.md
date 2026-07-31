# Commission Portal (Electron shell) — architecture notes

Written 2026-07-31. This document reflects the FINAL direction after two
same-day pivots; the earlier drafts are summarized under "History" so the
leftover artifacts make sense.

## What this is

`10. Electron App/app/` is a thin Electron shell around the existing Flask
dashboard (`8. Web Dashboard`). It does exactly what `Launch Dashboard.bat`
does — free port 5001, drop `dashboard_cache.pkl`, start Flask with the
install's `.venv` python (fallback: `python` on PATH), wait for the port —
but shows the dashboard in a native "Commission Portal" window (Eternalgy
icon, no browser chrome) instead of opening Chrome. Closing the window
taskkills the Flask child; a force-killed shell leaves Flask running, which
the next launch's free-port step cleans up (same contract as the bat).

Deployment model: **per-machine install, no central server, no tunnel.**
Each user (IT Admin, IT Manager, HR Exec, Finance Exec, Founder,
Co-Founder) installs the dashboard on their own PC and runs it locally —
"different location" is solved by the server running wherever the user is.
Data is shared because every install talks to the same two backends:
- **Reads**: company Postgres via the read-only HTTP proxy
  (`PG_PROXY_URL`/`PG_PROXY_TOKEN`).
- **Writes**: the shared Supabase Postgres (`DATABASE_URL`) — rates, rules,
  special cases, users, audit log. This was already true of the Flask app
  before any of this session's work.

Accepted trade-off (2026-07-31): each install holds the real
`DATABASE_URL`/`PG_PROXY_TOKEN` in its local `.env`, like the existing
dashboard installs already do. Fine for this trusted six-person tier; the
anon-key-only model (see History) was designed for a less trusted audience
that turned out not to be the real user base.

Cross-machine freshness: raw data is immediately consistent (one shared
DB). Computed commission views cache per-machine (`_data_cache` +
`dashboard_cache.pkl`, 300s TTL at `app.py:147`); another machine's edit
can leave stale *computed* numbers for up to 5 minutes. Decision
2026-07-31: leave as-is, no refresh button, no cross-machine invalidation.

## Files

- `app/main.js` — the whole shell (launcher + window). No preload, no
  renderer code, no supabase-js: the window just shows `127.0.0.1:5001`.
- `app/loading.html` — logo + spinner while Flask boots (up to 60s grace
  for first-run venv situations, then a pointed error dialog).
- `app/assets/` — `logo-mark-black.png` (copied from
  `8. Web Dashboard/static/`) and `icon.ico` (generated from it via PIL).
- `app/package.json` — electron + electron-builder only; `npm start` to
  run in dev, `npm run dist` for an unpacked `--dir` build.

Flask's login page (`8. Web Dashboard/static/login.html`) got the black
logo mark above the "Commission Portal" heading (title was already right).

Verified end-to-end 2026-07-31: shell boots Flask from the repo root,
`/login` answers 200 with logo + heading, no renderer errors.

## Updates (the OTA story)

Two update paths, both via GitHub, no other infrastructure:
- **Dashboard code** (changes often): the existing, already-shipped OTA —
  `8. Web Dashboard/updater.py` polls GitHub Releases, `apply_update.py`
  swaps files. Nothing changed here.
- **The shell itself** (changes rarely): rides the *installer*, not OTA.
  Ship the packaged shell inside the Inno payload; a shell change means a
  new installer version. If shell churn ever becomes frequent, revisit
  with electron-updater — deliberately not built now.

## Distribution — wired 2026-07-31

1. **Done.** `tools/build_package.py` copies the `npm run dist` output
   (`10. Electron App/app/dist/win-unpacked/`) into the payload as
   `<root>/shell/` (warns and falls back to bat-only if the build is
   missing). `installer/CommissionDashboard.iss` shortcuts + the
   post-install "Start now" entry point at `shell\Commission Portal.exe`;
   `Launch Dashboard.bat` still ships as a fallback launcher, no shortcut.
   `.github/workflows/release.yml` builds the shell (setup-node + npm ci +
   npm run dist) before the package step, so a `v*` tag produces an
   installer with the shell inside.
2. **Done.** OTA zips never include `shell/` (INCLUDE globs don't match
   it — verified 0 shell files in the built zip, 471 KB total) and both
   PRESERVE_PATHS copies (updater.py + apply_update.py) pin `shell` so an
   update can't delete it.
3. Repo is public — installer download is a plain link, no GitHub account
   needed by users. Flagged 2026-07-31: source + binaries are visible to
   the internet; conscious choice, revisit if that changes.
4. Code signing cert — unsigned builds trip SmartScreen. Decide later.

Verified locally 2026-07-31: packaged exe from the dev-tree dist path
boots Flask (login answers 200) and kills it on close; a loadURL retry
loop covers the race where the just-killed previous listener accepts one
TCP connect before dying. Small-screen fix same day: window opens
maximized, Ctrl+= / Ctrl+- / Ctrl+0 zoom the page.

Not yet done: an end-to-end install test of the built installer itself
(needs Inno Setup locally or a pushed `v*` tag to run the workflow).

## History (same-day pivots, 2026-07-31)

**Draft 1 — "agent portal":** custom Electron UI talking directly to
Supabase with the anon key; per-agent RLS scoping via a `bubble_id` link
(migration `0001`). Scrapped when the real audience turned out to be
internal staff, not sales agents.

**Draft 2 — "staff portal":** same custom UI, but `portal_staff`
allowlist + full visibility (migration `0002`, applied live). Scrapped
when it became clear the user wanted the *existing dashboard* in the
window, not a rebuilt subset of it.

**Leftovers that still exist and are harmless:**
- Supabase tables `invoices`, `portal_staff`, `commission_entries` (live,
  RLS on) — currently unused by any app. The `invoices` mirror + sync
  give a clean path to future Supabase-side features (e.g. the "commission
  calc as DB function" idea), so they were left in place.
- **The sync job + its two Task Scheduler entries are still running**
  (`sync/sync_invoices.py`, hourly current-month + nightly 03:00 full;
  first backfill 2026-07-31: 5,942 invoices, zero failed months). Keeping
  the mirror warm costs ~1 proxy query/hour. Stop via Task Scheduler if
  unwanted.
- `sync/onboard_staff.py` — manages `portal_staff` (one row exists:
  Aqila / IT Admin / nurul@eternalgy.me). Unused until something reads
  that table again.
- Migration `0001` is superseded by `0002` but kept for the record.
