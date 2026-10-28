"""Session-based login for the dashboard.

Passwords are hashed with werkzeug's PBKDF2 implementation (generate_password_hash /
check_password_hash) -- never stored or logged in plaintext.
"""

from __future__ import annotations

import hashlib
import threading
import time
from functools import wraps
from typing import Callable

from flask import jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db


def hash_password(password: str) -> str:
    return generate_password_hash(password)


# ── Available pages (Admin page, 28 Oct 2026) ─────────────────────────────────
# The menu pages an account may open: (key, label, path). An admin may open
# every page; a staff account every page but Admin unless its `pages` column
# names a narrower set. A page left out is hidden from the menu and refused by
# the server, its data too.
PAGES = (
    ("overview", "Overview", "/"),
    ("sales_report", "Sales Report", "/sales-report"),
    ("basic_nfp", "Basic & Net Floor Price Commission", "/report?section=basic_nfp"),
    ("anp", "ANP Commission", "/report?section=anp"),
    ("ega_esa", "EGA/ESA Award", "/report?section=ega_esa"),
    ("production_bonus", "Production Bonus", "/report?section=production_bonus"),
    ("monthly_contest", "Monthly Contest", "/report?section=monthly_contest"),
    ("data", "Data", "/data"),
    ("admin", "Admin", "/admin"),
)
PAGE_KEYS = tuple(k for k, _l, _p in PAGES)
COMMISSION_PAGES = ("basic_nfp", "anp", "ega_esa", "production_bonus", "monthly_contest")


def parse_pages(value) -> list[str] | None:
    """The stored `pages` cell as page keys, or None for "every page"."""
    text = str(value or "").strip()
    if not text:
        return None
    return [k for k in (p.strip() for p in text.split(",")) if k in PAGE_KEYS and k != "admin"]


def allowed_pages(user: dict | None) -> list[str]:
    """The page keys this account may open, in menu order."""
    if not user:
        return []
    if user.get("role") == "admin":
        return list(PAGE_KEYS)
    chosen = user.get("pages")
    if chosen is None:
        return [k for k in PAGE_KEYS if k != "admin"]
    return [k for k in PAGE_KEYS if k in chosen]


def can_view(user: dict | None, key: str) -> bool:
    return key in allowed_pages(user)


def first_page_path(user: dict | None) -> str | None:
    """Where to send someone who opened a page they may not see."""
    keys = allowed_pages(user)
    return next((p for k, _l, p in PAGES if k in keys), None)


def verify_login(username: str, password: str) -> dict | None:
    user = db.get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "mark": _password_mark(user["password_hash"])}


# A sign-in is remembered for 14 days (PERMANENT_SESSION_LIFETIME in app.py),
# so the cookie alone can no longer be trusted for that long: an admin who
# deactivates someone or resets their password expects it to take effect, not
# to wait out the fortnight. Every signed-in visit is therefore checked against the
# users table -- that the account still exists, is active, and has the password
# it had at sign-in.
#
# A lookup costs a round trip to the proxy (about a third of a second) and a
# page makes several API calls, so each account is re-checked at most once a
# minute per server. A change made through this server's Admin page clears the
# entry at once (forget_user); one made elsewhere lands within the minute.
_CHECK_EVERY_SECONDS = 60
_checked: dict[int, tuple[float, dict | None]] = {}
_checked_lock = threading.Lock()


def _password_mark(password_hash: str) -> str:
    """A short fingerprint of the stored hash, kept in the cookie so a password
    change can be noticed. The cookie is signed, not encrypted, so it carries
    this rather than the hash itself."""
    return hashlib.sha256(str(password_hash or "").encode("utf-8")).hexdigest()[:16]


def _account(user_id: int) -> dict | None:
    """The account as the users table has it now, or None if it is gone or
    deactivated. Raises only when the table cannot be read at all."""
    now = time.time()
    with _checked_lock:
        hit = _checked.get(user_id)
        if hit and now - hit[0] < _CHECK_EVERY_SECONDS:
            return hit[1]
    row = db.get_user_by_id(user_id)
    account = None
    if row and int(row.get("is_active") or 0) == 1:
        account = {"username": row["username"], "role": row["role"],
                   "name": str(row.get("name") or "").strip(),
                   "pages": parse_pages(row.get("pages")),
                   "mark": _password_mark(row["password_hash"])}
    with _checked_lock:
        _checked[user_id] = (now, account)
    return account


def forget_user(user_id: int) -> None:
    """Drop the remembered check for one account so its next request re-reads
    the users table -- called whenever an admin changes that account."""
    with _checked_lock:
        _checked.pop(int(user_id), None)


def current_user() -> dict | None:
    if "user_id" not in session:
        return None
    user_id = session["user_id"]
    try:
        account = _account(user_id)
    except Exception:
        # The users table is unreachable. Signing everyone out whenever the
        # proxy has a bad minute would be worse than trusting a signed cookie
        # for that minute, and nobody can sign in meanwhile either.
        return {"id": user_id, "username": session.get("username"), "role": session.get("role"),
                "name": session.get("name") or "", "pages": session.get("pages")}

    if account is None:
        session.clear()
        return None
    if "pw" not in session:
        # Signed in before remembered sign-ins existed. Adopt the current
        # password rather than signing them out on the upgrade, and let the
        # cookie outlive the window from now on.
        session["pw"] = account["mark"]
        session.permanent = True
    elif session["pw"] != account["mark"]:
        session.clear()
        return None

    # The role is read from the table, not the cookie, so demoting an admin
    # takes effect without waiting for them to sign in again.
    session["username"] = account["username"]
    session["role"] = account["role"]
    # The Name from the Admin page, for the sidebar's "Hi Name!".
    session["name"] = account.get("name") or ""
    # Kept in the cookie too, so the pages stay limited for the minute the
    # users table may be unreachable.
    session["pages"] = account.get("pages")
    return {"id": user_id, "username": account["username"], "role": account["role"],
            "name": account.get("name") or "", "pages": account.get("pages")}


def login_user(user: dict) -> None:
    session.clear()
    session.permanent = True        # survives closing the app, for 14 days
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["pw"] = user["mark"]
    forget_user(user["id"])


def logout_user() -> None:
    session.clear()


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if _wants_json():
                return jsonify({"error": "Login required"}), 401
            return redirect(f"/login?next={request.path}")
        return view(*args, **kwargs)
    return wrapped


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify({"error": "Login required"}), 401
            return redirect(f"/login?next={request.path}")
        if user["role"] != "admin":
            if _wants_json():
                return jsonify({"error": "Admin access required"}), 403
            return "Admin access required", 403
        return view(*args, **kwargs)
    return wrapped
