"""
Internal full-time Basic Commission report (prod_main) via Postgres read-only proxy.

Environment:
  PG_PROXY_TOKEN   Bearer token (required)

Optional:
  PG_PROXY_URL     default https://pg-proxy-production.up.railway.app/api/sql
  PG_PROXY_DB      default prod_main

Tables printed:
  Table 1 — Accumulate basic commission by agent_name
  Table 2 — By customer / invoice (ordered by agent_name, invoice_date)

Filters:
  invoice.paid = TRUE
  full_payment_date year = --year (default 2026)
  agent.agent_type in ('internal', 'full time')  (case-insensitive)

Formula:
  Basic Commission = (total_amount - epp_interest) * m%
  Executive m = 3%; Senior m = 3.25%
  Senior names: Sunny, Martin, Kent, Zhe Hang (substring match on agent_name)
  Senior override: +0.25% of each report's accumulated basic commission (executive tier only)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent

EXECUTIVE_RATE = Decimal("0.03")
SENIOR_RATE = Decimal("0.0325")
SENIOR_OVERRIDE_RATE = Decimal("0.0025")

AGENT_TYPES_SQL = "'internal', 'full time'"

SENIOR_NAME_TOKENS = ("sunny", "martin", "kent", "zhe hang")

# Executives under Sunny (substring match on agent_name, case-insensitive)
SUNNY_REPORT_TOKENS = (
    "jia keat",
    "zul",
    "denise",
    "jia xuan",
    "vincent",
    "ah zu",
)

# Executives under Teng Kah Kent
KENT_REPORT_TOKENS = (
    "louis ng",
    "anisah najwa",
    "anisah",
)

SENIOR_CANONICAL = {
    "sunny": "Sunny Tan",
    "martin": "Martin Hing",
    "kent": "Teng Kah Kent",
    "zhe hang": "CHING ZHE HANG",
}


def _load_dotenv() -> None:
    env_path = _SCRIPT_DIR / ".env"
    if not env_path.is_file():
        return
    proxy_keys = frozenset(
        {"PG_PROXY_TOKEN", "PG_PROXY_URL", "PG_PROXY_DB", "PG_DB_NAME"}
    )
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if name and (name in proxy_keys or name not in os.environ):
            os.environ[name] = value


def _normalize_proxy_url(url: str | None) -> str | None:
    if not url:
        return url
    base = url.strip().rstrip("/")
    if base.endswith("/api/sql"):
        return base
    return f"{base}/api/sql"


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sanitize_contains_token(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    if not re.fullmatch(r"[\w .,'&/-]+", token, flags=re.UNICODE):
        raise ValueError(
            f"Unsafe customer filter token {token!r}. "
            "Use letters, numbers, spaces, and limited punctuation only."
        )
    return token


def _customer_filter_sql(tokens: list[str]) -> str:
    if not tokens:
        return ""
    parts: list[str] = []
    for raw in tokens:
        t = _sanitize_contains_token(raw)
        if not t:
            continue
        lit = _sql_string_literal(f"%{t}%")
        parts.append(
            f"(coalesce(c.name, i.customer_name_snapshot, '') ilike {lit})"
        )
    if not parts:
        return ""
    return " AND (" + " OR ".join(parts) + ")"


def _proxy_sql(
    *,
    proxy_url: str,
    db_name: str,
    token: str,
    sql: str,
    params: list[Any],
) -> dict[str, Any]:
    body = json.dumps({"db_name": db_name, "sql": sql, "params": params}).encode()
    req = urllib.request.Request(
        proxy_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 400 and "token expired" in detail.lower():
            raise RuntimeError(
                "Postgres proxy token expired. Request a new read-only JWT from your "
                "proxy admin, then update PG_PROXY_TOKEN in .env in this folder:\n"
                f"  {_SCRIPT_DIR / '.env'}"
            ) from e
        raise RuntimeError(f"HTTP {e.code} from proxy: {detail}") from e


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return Decimal(str(value))


def _fmt_money(value: Any) -> str:
    return f"{_to_decimal(value):,.2f}"


def _fmt_rate(rate: Decimal) -> str:
    return f"{(rate * 100):.2f}%"


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _name_has_token(agent_name: str, token: str) -> bool:
    return token in _norm_name(agent_name)


def _is_senior(agent_name: str) -> bool:
    return any(_name_has_token(agent_name, t) for t in SENIOR_NAME_TOKENS)


def _tier_label(agent_name: str) -> str:
    return "Senior" if _is_senior(agent_name) else "Executive"


def _own_rate(agent_name: str) -> Decimal:
    return SENIOR_RATE if _is_senior(agent_name) else EXECUTIVE_RATE


def _reporting_senior(agent_name: str) -> str | None:
    """Senior who earns +0.25% of this executive's accumulated basic commission."""
    if _is_senior(agent_name):
        return None
    n = _norm_name(agent_name)
    if any(tok in n for tok in SUNNY_REPORT_TOKENS):
        return SENIOR_CANONICAL["sunny"]
    if any(tok in n for tok in KENT_REPORT_TOKENS):
        return SENIOR_CANONICAL["kent"]
    return None


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt_row(headers), sep, *[fmt_row(r) for r in rows]])


def _invoices_sql(*, year: int, customer_filter_sql: str) -> str:
    return f"""
SELECT
  COALESCE(NULLIF(TRIM(a.name), ''), '(unknown)') AS agent_name,
  a.bubble_id AS agent_bubble_id,
  COALESCE(NULLIF(TRIM(c.name), ''), NULLIF(TRIM(i.customer_name_snapshot), ''), '(unknown)') AS customer_name,
  i.invoice_number,
  i.invoice_date,
  i.full_payment_date,
  COALESCE(i.total_amount, 0)::numeric AS total_amount,
  COALESCE(
    NULLIF(epp_items.epp_interest, 0),
    NULLIF(pay.epp_sum, 0),
    NULLIF(i.effective_epp, 0),
    0
  )::numeric AS epp_interest
FROM invoice i
INNER JOIN agent a ON a.bubble_id = i.linked_agent
LEFT JOIN customer c ON c.customer_id = i.linked_customer
LEFT JOIN LATERAL (
  SELECT COALESCE(
    NULLIF(SUM(CASE WHEN COALESCE(ii.epp, 0) > 0 THEN ii.epp ELSE 0 END), 0),
    SUM(
      CASE
        WHEN COALESCE(ii.description, '') ILIKE '%epp%interest%'
             OR COALESCE(ii.description, '') ILIKE '%epp interest%'
        THEN COALESCE(ii.amount, ii.unit_price, 0)
        ELSE 0
      END
    ),
    0
  ) AS epp_interest
  FROM invoice_item ii
  WHERE ii.linked_invoice = i.bubble_id
) epp_items ON TRUE
LEFT JOIN LATERAL (
  SELECT SUM(COALESCE(p.epp_cost, 0)) AS epp_sum
  FROM payment p
  WHERE p.linked_invoice = i.bubble_id
) pay ON TRUE
WHERE i.paid IS TRUE
  AND i.full_payment_date IS NOT NULL
  AND EXTRACT(YEAR FROM i.full_payment_date)::int = {int(year)}
  AND LOWER(btrim(COALESCE(a.agent_type, ''))) IN ({AGENT_TYPES_SQL})
  {customer_filter_sql}
ORDER BY agent_name ASC, i.invoice_date ASC NULLS LAST, i.invoice_number ASC NULLS LAST
""".strip()


@dataclass
class InvoiceLine:
    agent_name: str
    customer_name: str
    invoice_number: str
    invoice_date: Any
    total_amount: Decimal
    epp_interest: Decimal
    tier_label: str
    commission_rate: Decimal
    basic_commission: Decimal

    @property
    def net_base(self) -> Decimal:
        return self.total_amount - self.epp_interest


def _process_invoices(rows: list[dict[str, Any]]) -> list[InvoiceLine]:
    lines: list[InvoiceLine] = []
    for row in rows:
        agent_name = str(row.get("agent_name") or "(unknown)")
        total = _to_decimal(row.get("total_amount"))
        epp = _to_decimal(row.get("epp_interest"))
        net = total - epp
        rate = _own_rate(agent_name)
        lines.append(
            InvoiceLine(
                agent_name=agent_name,
                customer_name=str(row.get("customer_name") or "(unknown)"),
                invoice_number=str(row.get("invoice_number") or ""),
                invoice_date=row.get("invoice_date"),
                total_amount=total,
                epp_interest=epp,
                tier_label=_tier_label(agent_name),
                commission_rate=rate,
                basic_commission=net * rate,
            )
        )
    return lines


def _table1_rows(lines: list[InvoiceLine]) -> tuple[int, list[list[str]]]:
    """
    Per agent: sum of invoice basic commission, plus for seniors an extra
    0.25% of each reporting executive's accumulated basic commission.
    """
    total_by_agent: dict[str, Decimal] = defaultdict(Decimal)
    own_commission_by_agent: dict[str, Decimal] = defaultdict(Decimal)

    for ln in lines:
        total_by_agent[ln.agent_name] += ln.total_amount
        own_commission_by_agent[ln.agent_name] += ln.basic_commission

    accumulated_by_agent = dict(own_commission_by_agent)

    for exec_name, exec_accum in own_commission_by_agent.items():
        senior = _reporting_senior(exec_name)
        if not senior:
            continue
        override = exec_accum * SENIOR_OVERRIDE_RATE
        accumulated_by_agent[senior] = accumulated_by_agent.get(senior, Decimal("0")) + override

    table_rows: list[list[str]] = []
    for agent in sorted(
        accumulated_by_agent.keys(),
        key=lambda x: (-accumulated_by_agent[x], x),
    ):
        table_rows.append(
            [
                agent,
                _fmt_money(total_by_agent.get(agent, Decimal("0"))),
                _fmt_money(accumulated_by_agent[agent]),
            ]
        )
    return len(accumulated_by_agent), table_rows


def _table2_rows(lines: list[InvoiceLine]) -> list[list[str]]:
    return [
        [
            ln.agent_name,
            ln.customer_name,
            ln.invoice_number,
            str(ln.invoice_date or ""),
            _fmt_money(ln.epp_interest),
            _fmt_money(ln.total_amount),
            _fmt_money(ln.basic_commission),
        ]
        for ln in lines
    ]


def main(argv: list[str]) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Basic Commission report — Table 1 (by agent) and Table 2 (by invoice)."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument(
        "--customer-contains",
        action="append",
        default=[],
        help="Substring filter on customer name (repeat for OR).",
    )
    parser.add_argument(
        "--proxy-url",
        default=_normalize_proxy_url(
            _env(
                "PG_PROXY_URL",
                "https://pg-proxy-production.up.railway.app/api/sql",
            )
        ),
    )
    parser.add_argument(
        "--db-name",
        default=_env("PG_PROXY_DB") or _env("PG_DB_NAME", "prod_main"),
    )
    args = parser.parse_args(argv)

    token = _env("PG_PROXY_TOKEN")
    if not token:
        print(
            f"Missing PG_PROXY_TOKEN. Create or edit:\n  {_SCRIPT_DIR / '.env'}",
            file=sys.stderr,
        )
        return 2
    if not args.proxy_url or not args.db_name:
        print("Missing proxy URL or database name.", file=sys.stderr)
        return 2

    tokens: list[str] = []
    for chunk in args.customer_contains:
        for part in str(chunk).split(","):
            part = part.strip()
            if part:
                tokens.append(part)
    try:
        customer_filter_sql = _customer_filter_sql(tokens)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    payload = _proxy_sql(
        proxy_url=args.proxy_url,
        db_name=args.db_name,
        token=token,
        sql=_invoices_sql(year=args.year, customer_filter_sql=customer_filter_sql),
        params=[],
    )
    lines = _process_invoices(list(payload.get("rows") or []))

    user_count, table1 = _table1_rows(lines)
    table2 = _table2_rows(lines)

    print("=== Basic Commission Report ===")
    print(f"Year (full_payment_date): {args.year}")
    print("Filters: paid = TRUE, agent_type in (internal, full time)")
    print(
        "Customer filter: "
        + (" OR ".join(repr(t) for t in tokens) if tokens else "(none)")
    )
    print(f"Total internal full-time agents with qualifying invoices: {user_count}")
    print()
    print("Rates: Executive m = 3.00%; Senior m = 3.25%")
    print(
        "Senior override: +0.25% of each report's accumulated basic commission "
        "(Sunny: Jia Keat, Zul, Denise, Jia Xuan, Vincent Tan, Ah Zu; "
        "Kent: Louis Ng, Anisah Najwa)"
    )
    print("Formula: Basic Commission = (total amount - epp interest) x m%")
    print()

    print("=== Table 1: Accumulate basic commission ===")
    print(_render_table(["Agent_name", "Total amount", "Accumulated Basic Commission"], table1))
    print()

    print("=== Table 2: Accumulate basic commission by customer ===")
    print(
        _render_table(
            [
                "agent_name",
                "Customer name",
                "Invoice number",
                "Invoice date",
                "EPP interest",
                "Total amount",
                "Basic Commission",
            ],
            table2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
