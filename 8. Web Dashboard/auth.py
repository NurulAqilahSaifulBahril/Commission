"""Session-based login for the dashboard.

Passwords are hashed with werkzeug's PBKDF2 implementation (generate_password_hash /
check_password_hash) -- never stored or logged in plaintext.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_login(username: str, password: str) -> dict | None:
    user = db.get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def current_user() -> dict | None:
    if "user_id" not in session:
        return None
    return {
        "id": session["user_id"],
        "username": session.get("username"),
        "role": session.get("role"),
    }


def login_user(user: dict) -> None:
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]


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
