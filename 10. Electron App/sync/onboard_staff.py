"""Grant portal access to a staff member (IT Admin, IT Manager, HR Exec,
Finance Exec, Founder, Co-Founder).

Onboarding is a two-step, admin-only process:

  1. In the Supabase Dashboard -> Authentication -> Users -> "Add user",
     create the person's login (email + password) and copy the new user's
     UUID.
  2. Run this script to grant them portal access:

         python onboard_staff.py --user-id <uuid> --name "Jane Tan" --role "Finance Exec"
         python onboard_staff.py --list                              # see who has access
         python onboard_staff.py --deactivate --user-id <uuid>       # revoke access

The app has no signup flow on purpose (see plan.md): a login only gets in if
a portal_staff row exists for it, which is what ties every commission entry
to a verified, named person.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")

VALID_ROLES = ["IT Admin", "IT Manager", "HR Exec", "Finance Exec", "Founder", "Co-Founder"]


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in the repo-root .env")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def list_staff() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ps.id, au.email, ps.full_name, ps.role, ps.is_active, ps.created_at
            FROM portal_staff ps
            JOIN auth.users au ON au.id = ps.id
            ORDER BY ps.created_at
        """)
        rows = cur.fetchall()
    if not rows:
        print("No one has portal access yet.")
        return
    print(f"{'active':<7} {'role':<14} {'name':<28} email")
    print("-" * 80)
    for user_id, email, name, role, is_active, _created in rows:
        print(f"{'yes' if is_active else 'NO':<7} {role:<14} {name:<28} {email}  ({user_id})")


def grant(user_id: str, name: str, role: str) -> None:
    if role not in VALID_ROLES:
        raise SystemExit(f"role must be one of: {', '.join(VALID_ROLES)}")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT email FROM auth.users WHERE id = %s", (user_id,))
        auth_row = cur.fetchone()
        if not auth_row:
            raise SystemExit(
                f"No Supabase Auth user with id {user_id}. Create the login in "
                "the Supabase Dashboard (Authentication -> Users) first."
            )
        cur.execute(
            """
            INSERT INTO portal_staff (id, full_name, role, is_active)
            VALUES (%s, %s, %s, true)
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                role = EXCLUDED.role,
                is_active = true
            """,
            (user_id, name, role),
        )
        conn.commit()
    print(f"Granted portal access: {auth_row[0]} -> {name} ({role}).")


def deactivate(user_id: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE portal_staff SET is_active = false WHERE id = %s", (user_id,))
        if cur.rowcount == 0:
            raise SystemExit(f"No portal_staff row for {user_id}.")
        conn.commit()
    print(f"Deactivated portal access for {user_id}. (Auth login itself is untouched.)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="List everyone with portal access")
    parser.add_argument("--user-id", help="Supabase Auth user UUID (from the dashboard)")
    parser.add_argument("--name", help="Full name to display in the app")
    parser.add_argument("--role", help=f"One of: {', '.join(VALID_ROLES)}")
    parser.add_argument("--deactivate", action="store_true", help="Revoke portal access (with --user-id)")
    args = parser.parse_args()

    if args.list:
        list_staff()
    elif args.deactivate and args.user_id:
        deactivate(args.user_id)
    elif args.user_id and args.name and args.role:
        grant(args.user_id, args.name, args.role)
    else:
        parser.error("use --list, --deactivate --user-id <uuid>, or --user-id/--name/--role together")


if __name__ == "__main__":
    main()
