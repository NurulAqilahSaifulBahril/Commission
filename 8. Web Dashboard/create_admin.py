#!/usr/bin/env python3
"""Create or reset a dashboard user account from the local terminal.

The password is entered locally via getpass (hidden input, never transmitted
over the network) and only its hash is stored in dashboard.db.

Usage:
    python create_admin.py
    python create_admin.py --username "IT Admin" --role admin
    python create_admin.py --username "HR" --role staff
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import db
from auth import hash_password

# getpass's hidden-input mode relies on a real Win32 console. It silently
# swallows keystrokes (no error, nothing typed) under Git Bash/MSYS2 terminals,
# which set MSYSTEM. Detect that up front instead of waiting for it to fail.
_GETPASS_UNRELIABLE = bool(os.environ.get("MSYSTEM"))


def _read_password(prompt: str) -> str:
    if not _GETPASS_UNRELIABLE:
        try:
            return getpass.getpass(prompt)
        except Exception:
            pass
    print("(Hidden input isn't supported in this terminal — your password will be visible as you type.)")
    return input(prompt)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Create or reset a dashboard user account.")
    parser.add_argument("--username", help="Account name, e.g. 'IT Admin'")
    parser.add_argument("--role", choices=["admin", "staff"], help="Account role")
    args = parser.parse_args(argv)

    db.init_db()
    db.migrate_from_json()

    username = args.username or input("Username: ").strip()
    if not username:
        print("Username cannot be empty.")
        return 1

    role = args.role
    if role is None:
        role_input = input("Role [admin/staff] (default: admin): ").strip().lower()
        role = role_input if role_input in ("admin", "staff") else "admin"

    existing = db.get_user_by_username(username)
    if existing:
        print(f"User '{username}' already exists (role: {existing['role']}). Resetting password.")

    while True:
        password = _read_password("Password: ")
        if len(password) < 6:
            print("Password must be at least 6 characters.")
            continue
        confirm = _read_password("Confirm password: ")
        if password != confirm:
            print("Passwords do not match. Try again.")
            continue
        break

    password_hash = hash_password(password)
    if existing:
        db.update_user(existing["id"], password_hash=password_hash, role=role, is_active=True)
        print(f"Password reset for '{username}' (role: {role}).")
    else:
        db.create_user(username, password_hash, role)
        print(f"Created user '{username}' with role '{role}'.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
