"""HTTP client for pg-proxy (used by NFP commission report)."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from nfp_paths import get_proxy_token, proxy_token_help

PROXY_URL = os.environ.get(
    "POSTGRES_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql"
)
DB_NAME = os.environ.get("POSTGRES_DB_NAME", "prod_main")
MAX_RETRIES = 6
RETRY_DELAY_SEC = 5
# Railway / gateway errors — usually temporary; retry instead of failing immediately
RETRYABLE_HTTP_CODES = (502, 503, 504)


class ApiRetryableError(Exception):
    """pg-proxy or gateway temporarily unavailable."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


def _server_error_help(status: int, body: str) -> str:
    return (
        f"API error {status}: pg-proxy did not respond in time.\n"
        f"{body}\n\n"
        "This is a **server-side** problem on Railway (not your token or NFP folder).\n"
        "Your token was accepted; the hosted app failed to answer.\n\n"
        "Try:\n"
        "  1. Wait 2–5 minutes and run: python nfp_commission.py --test-api\n"
        "  2. Check if Basic Commission can query the DB right now\n"
        "  3. Ask whoever manages pg-proxy / Railway to restart or check the service\n"
        "  4. If your team uses local docker pg-proxy when Railway is down:\n"
        '       $env:POSTGRES_PROXY_URL="http://127.0.0.1:PORT/api/sql"\n'
    )


def _warn_if_token_expired(token: str) -> None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp and time.time() > float(exp):
            exp_dt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp))
            print(
                f"WARNING: Bearer token expired at {exp_dt}. "
                "Request a new read-only token.",
                file=sys.stderr,
            )
    except (ValueError, json.JSONDecodeError, TypeError):
        return


def _network_help(exc: BaseException) -> str:
    return (
        f"Could not reach the database API.\n"
        f"URL: {PROXY_URL}\n"
        f"Error: {exc}\n\n"
        "NFP Commission uses the same pg-proxy API as Basic Commission — "
        "you do NOT need a separate Postgres/Docker for NFP.\n\n"
        "Try:\n"
        "  1. pip install requests\n"
        "  2. New token in ..\\4. data\\pg_proxy_token.txt\n"
        "  3. If Basic uses local docker proxy, set POSTGRES_PROXY_URL to that URL\n"
        "  4. python nfp_commission.py --test-api\n"
    )


def _post_with_requests(
    body: Dict[str, Any], headers: Dict[str, str]
) -> Dict[str, Any]:
    import requests
    from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

    try:
        resp = requests.post(PROXY_URL, json=body, headers=headers, timeout=180)
    except (ConnectionError, Timeout) as e:
        raise ConnectionResetError(str(e)) from e
    except RequestException as e:
        raise ConnectionResetError(str(e)) from e

    if resp.status_code in (401, 403):
        raise SystemExit(
            f"API error {resp.status_code} (auth failed). "
            f"Get a new Bearer token.\n{resp.text}"
        )
    if resp.status_code in RETRYABLE_HTTP_CODES:
        raise ApiRetryableError(resp.status_code, resp.text)
    try:
        resp.raise_for_status()
    except HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        text = e.response.text if e.response is not None else str(e)
        if code in RETRYABLE_HTTP_CODES:
            raise ApiRetryableError(code, text) from e
        raise SystemExit(f"API error {code}: {text}") from e
    return resp.json()


def _post_with_urllib(body: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    req = urllib.request.Request(
        PROXY_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code in RETRYABLE_HTTP_CODES:
            raise ApiRetryableError(e.code, detail) from e
        if e.code in (401, 403):
            raise SystemExit(
                f"API error {e.code} (auth failed). Get a new Bearer token.\n{detail}"
            ) from e
        raise SystemExit(f"API error {e.code}: {detail}") from e


def _post_json(body: Dict[str, Any], token: str) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "nfp-commission-report/1.0",
    }
    try:
        import requests  # noqa: F401

        return _post_with_requests(body, headers)
    except ImportError:
        return _post_with_urllib(body, headers)


def query_sql(sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    token = get_proxy_token()
    if not token:
        raise SystemExit(proxy_token_help())
    print("Token: found (from env or pg_proxy_token.txt)", file=sys.stderr)
    _warn_if_token_expired(token)

    body: Dict[str, Any] = {
        "db_name": DB_NAME,
        "sql": sql,
        "params": params or [],
    }

    last_error: Optional[BaseException] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt == 1:
                print(f"Connecting to API (attempt {attempt}/{MAX_RETRIES})...", file=sys.stderr)
            else:
                print(f"Retrying API (attempt {attempt}/{MAX_RETRIES})...", file=sys.stderr)
            payload = _post_json(body, token)
            if "error" in payload:
                raise SystemExit(f"SQL error: {payload['error']}")
            return payload.get("rows", [])
        except SystemExit:
            raise
        except ApiRetryableError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                print(
                    f"API temporarily unavailable ({e.status}) "
                    f"— retry {attempt}/{MAX_RETRIES} in {RETRY_DELAY_SEC}s...",
                    file=sys.stderr,
                )
                time.sleep(RETRY_DELAY_SEC)
                continue
            raise SystemExit(_server_error_help(e.status, e.body)) from e
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            urllib.error.URLError,
            OSError,
        ) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                print(
                    f"API connection failed ({attempt}/{MAX_RETRIES}): {e}",
                    file=sys.stderr,
                )
                print(f"Retrying in {RETRY_DELAY_SEC}s...", file=sys.stderr)
                time.sleep(RETRY_DELAY_SEC)
                continue
            raise SystemExit(_network_help(e)) from e

    raise SystemExit(_network_help(last_error or RuntimeError("unknown")))


def test_connection() -> List[Dict[str, Any]]:
    print(f"API URL: {PROXY_URL}")
    print(f"Database: {DB_NAME}")
    print("Checking token and network (may take up to ~30 seconds)...", flush=True)
    return query_sql("SELECT 1 AS ok")
