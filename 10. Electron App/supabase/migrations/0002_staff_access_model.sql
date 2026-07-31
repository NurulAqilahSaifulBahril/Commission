-- Replaces the bubble_id-scoped "agent portal" access model from migration
-- 0001 with a staff-allowlist model.
--
-- Correction: the app's users are IT Admin / IT Manager / HR Exec /
-- Finance Exec / Founder / Co-Founder — internal leadership/admin roles who
-- each need FULL visibility, working from a different physical location
-- than wherever Flask runs. They are not field sales agents, so there is no
-- bubble_id to scope them to. agent_accounts and the bubble_id-based RLS on
-- invoices/commission_entries from 0001 never had real data in them and are
-- replaced outright, not migrated.
--
-- The 5,942 rows already synced into `invoices` by migration 0001 /
-- sync_invoices.py are untouched — only its RLS policy changes.

-- ---------------------------------------------------------------------------
-- portal_staff — the allowlist. A Supabase Auth login only gets into the
-- app if a row exists here for it. Deactivate access by flipping
-- is_active, not by deleting the auth.users row — keeps the login's audit
-- trail (who created which commission_entries) intact.
-- ---------------------------------------------------------------------------
drop table if exists agent_accounts cascade;

create table portal_staff (
    id          uuid primary key references auth.users(id),
    full_name   text not null,
    role        text not null check (role in (
                    'IT Admin', 'IT Manager', 'HR Exec', 'Finance Exec', 'Founder', 'Co-Founder'
                )),
    is_active   boolean not null default true,
    created_at  timestamptz not null default now()
);

alter table portal_staff enable row level security;

-- Everyone in the allowlist can see the allowlist (useful for an
-- eventual "who else has access" screen) but only their own row's shape
-- matters for the other tables' policies below.
create policy portal_staff_select_all on portal_staff
    for select to authenticated
    using (true);

-- ---------------------------------------------------------------------------
-- invoices — was scoped to agent_bubble_id = caller's own agent identity.
-- Now: any active portal_staff member sees every invoice.
-- ---------------------------------------------------------------------------
drop policy if exists invoices_select_own on invoices;

create policy invoices_select_staff on invoices
    for select to authenticated
    using (
        exists (select 1 from portal_staff where id = auth.uid() and is_active)
    );

-- ---------------------------------------------------------------------------
-- commission_entries — rebuilt. Drops agent_bubble_id (there's no agent
-- identity to freeze) and the pending/approved/rejected review workflow
-- (no separate reviewer population — everyone with app access is already
-- the trusted tier). Mirrors the existing Flask "Special Case" model:
-- any staff member can create or edit an entry, audited by who.
-- ---------------------------------------------------------------------------
drop table if exists commission_entries cascade;

create table commission_entries (
    id                    uuid primary key default gen_random_uuid(),
    invoice_bubble_id     text not null references invoices(invoice_bubble_id),
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
    created_by            uuid not null references auth.users(id) default auth.uid(),
    created_by_name       text not null,
    updated_by            uuid references auth.users(id),
    updated_by_name       text,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);

create index idx_commission_entries_invoice on commission_entries (invoice_bubble_id);

create trigger commission_entries_set_updated_at
    before update on commission_entries
    for each row execute function set_updated_at();

alter table commission_entries enable row level security;

create policy commission_entries_select_staff on commission_entries
    for select to authenticated
    using (exists (select 1 from portal_staff where id = auth.uid() and is_active));

create policy commission_entries_insert_staff on commission_entries
    for insert to authenticated
    with check (exists (select 1 from portal_staff where id = auth.uid() and is_active));

create policy commission_entries_update_staff on commission_entries
    for update to authenticated
    using (exists (select 1 from portal_staff where id = auth.uid() and is_active))
    with check (exists (select 1 from portal_staff where id = auth.uid() and is_active));

-- No delete policy: default-deny, same reasoning as 0001 — correcting an
-- entry is a new row or an edit, not an erasure, so the audit trail holds.
