-- Agent Portal schema — the tables the Electron app reads and writes.
--
-- This lives in the SAME Supabase project db.py already connects to
-- (DATABASE_URL in the repo-root .env). Nothing here touches the existing
-- dashboard tables (users, commission_rates, agent_roles, special_cases, ...).
--
-- Flask connects as the `postgres` role (or any BYPASSRLS role) via
-- DATABASE_URL, so it ignores every policy below automatically — it keeps
-- full read/write access exactly as today. RLS only constrains connections
-- made with the Supabase anon/authenticated key, which is what the Electron
-- app will use.
--
-- NOT run against the live project yet. Review, then apply via the Supabase
-- SQL editor or `supabase db push` once you're ready.

-- ---------------------------------------------------------------------------
-- agent_accounts — links a Supabase Auth login to a real field agent.
--
-- Deliberately no self-service insert/update policy: onboarding a new agent
-- (creating their auth.users row + this mapping row) is an admin action done
-- with the service_role key, not something the app itself exposes. This is
-- what makes "every entry is tied to a real person" actually hold — an
-- agent can't just claim to be a different bubble_id.
-- ---------------------------------------------------------------------------
create table agent_accounts (
    id             uuid primary key references auth.users(id),
    pg_bubble_id   text not null unique,   -- matches agent_roles.pg_bubble_id / invoice.linked_agent's bubble_id
    agent_name     text not null,
    agent_type     text,                   -- 'internal' | 'outsource', mirrors agent_roles.agent_type
    is_active      boolean not null default true,
    created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- invoices — read-only mirror of the live company Postgres, populated by the
-- one-machine sync job (via PG_PROXY_URL, then written here with the
-- service_role key). Column set matches full_internal_basic_commission.py's
-- _invoices_sql output, plus agent_bubble_id which that query doesn't
-- currently select but the sync job must add — RLS below depends on it.
--
-- Keyed on the invoice's own bubble_id, not invoice_number: invoice_number
-- can be blank in the source data (see fetch_invoice_dates's
-- COALESCE(NULLIF(TRIM(invoice_number),''), bubble_id) fallback).
-- ---------------------------------------------------------------------------
create table invoices (
    invoice_bubble_id      text primary key,
    invoice_number         text,
    agent_bubble_id        text not null,
    agent_name             text,
    agent_type             text,
    customer_name          text,
    invoice_date           date,
    first_payment_date     date,
    full_payment_date      date,
    pct5_date               date,
    pct75_date               date,
    pct100_date               date,
    total_amount            numeric,
    paid_amount              numeric,
    epp_interest             numeric,
    package_type             text,
    package_name_snapshot   text,
    description              text,
    seda_nem_type            text,
    referral_project_type   text,
    referral_name            text,
    synced_at                timestamptz not null default now()
);

create index idx_invoices_agent_bubble_id on invoices (agent_bubble_id);

-- ---------------------------------------------------------------------------
-- commission_entries — what an agent types in remotely. This is a PROPOSED
-- adjustment, not an authoritative override: it starts 'pending' and stays
-- editable by its author only until someone reviews it (status leaves
-- 'pending'), at which point it locks. Review/approve happens from the Flask
-- side (service_role, bypasses RLS) — no review UI is being built here yet.
--
-- entry_type mirrors the existing admin-side "Special Case" types in
-- 8. Web Dashboard (special_cases table / SPECIAL_CASE_FIELDS) plus a plain
-- 'remark' for a note with no numeric adjustment attached.
-- ---------------------------------------------------------------------------
create table commission_entries (
    id                    uuid primary key default gen_random_uuid(),
    invoice_bubble_id     text not null references invoices(invoice_bubble_id),
    agent_bubble_id       text not null,   -- denormalized from invoices for a cheap RLS check; frozen at submit time
    created_by            uuid not null references auth.users(id) default auth.uid(),
    entry_type            text not null check (entry_type in (
                               'adjusted_nfp', 'fee_waiver', 'adjusted_rate',
                               'profit_sharing', 'adjusted_sales_price', 'remark'
                           )),
    adjusted_sales_price  numeric,
    net_floor_price       numeric,
    fee_waiver            numeric,
    rate_pct              numeric,
    profit_sharing_pct    numeric,
    remarks               text,
    status                text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
    reviewed_by           uuid references auth.users(id),
    reviewed_at           timestamptz,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);

create index idx_commission_entries_agent_bubble_id on commission_entries (agent_bubble_id);
create index idx_commission_entries_invoice on commission_entries (invoice_bubble_id);

create function set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger commission_entries_set_updated_at
    before update on commission_entries
    for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
alter table agent_accounts enable row level security;
alter table invoices enable row level security;
alter table commission_entries enable row level security;

-- agent_accounts: read your own mapping row only. No write policy at all —
-- default-deny, onboarding is a service_role action.
create policy agent_accounts_select_own on agent_accounts
    for select to authenticated
    using (id = auth.uid());

-- invoices: read only the rows for your own agent identity. No write policy
-- for `authenticated` at all — only the sync job (service_role) writes here.
create policy invoices_select_own on invoices
    for select to authenticated
    using (
        agent_bubble_id = (select pg_bubble_id from agent_accounts where id = auth.uid())
    );

-- commission_entries: see and create only your own entries, tied to your own
-- agent identity (not just "any invoice_bubble_id you can guess").
create policy commission_entries_select_own on commission_entries
    for select to authenticated
    using (created_by = auth.uid());

create policy commission_entries_insert_own on commission_entries
    for insert to authenticated
    with check (
        created_by = auth.uid()
        and agent_bubble_id = (select pg_bubble_id from agent_accounts where id = auth.uid())
    );

-- Editable only by its author, only while still pending. Once reviewed
-- (approved/rejected) it locks — the agent can submit a new entry instead,
-- which keeps the review trail intact.
create policy commission_entries_update_own_pending on commission_entries
    for update to authenticated
    using (created_by = auth.uid() and status = 'pending')
    with check (created_by = auth.uid() and status = 'pending');

-- No delete policy anywhere: default-deny. Cancelling is a status change /
-- remark, not a delete, so the review trail can't be quietly erased.
