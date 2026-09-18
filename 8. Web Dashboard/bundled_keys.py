"""Proxy tokens that ship with the build, applied as environment defaults.

WHY THIS EXISTS. Every part of this project reads its proxy token from the
environment, and each one reads it on its own: pgproxy.py wants
PG_MIRROR_TOKEN, and the commission engines -- full_internal_basic_commission,
nfp_commission, the ANP and EGA/ESA scripts -- each read PG_PROXY_TOKEN
directly, with no shared helper between them. On a machine with no .env that
means the dashboard cannot reach its own tables AND no commission can be
calculated, and the two fail in different places with different messages.

Staff were expected to close that gap by hand: open a hidden file in
Application Support, paste two tokens, restart. It is one step per machine,
forever, and it is where every install has stalled.

So the tokens live here and are applied with setdefault before anything reads
them. setdefault, not assignment, so precedence still runs the right way:

    real environment  >  .env (loaded by pgproxy)  >  these

A machine with its own credentials, a CI build with seeded secrets, and a
developer exporting a token in their shell all keep working exactly as before;
only a machine with nothing configured falls through to these.

TWO DATABASES, TWO TOKENS. They are not interchangeable and swapping them
fails with {"error":"Token does not allow this db_name"}, which names the
symptom and not the cause. Check db_name in a token's middle segment before
pasting it.

    PG_MIRROR_TOKEN   NUrul_DB    full        the dashboard's own tables
    PG_PROXY_TOKEN    prod_main   read_only   ERP source data for calculations

SECURITY. These are live credentials in tracked source. That is a deliberate
trade -- it is the price of an install that needs no setup -- and it holds only
while the repository is private. Anyone who can read this file can read the
commission database. Before making the repository public, empty both constants.
Rotate them here when they are rotated at the proxy: this file and
monthly_contest.py are the two places a literal token appears.
"""

from __future__ import annotations

import os

# ── Paste the tokens between the quotes ──────────────────────────────────────
#
# The JWT only. No "Bearer " prefix -- the callers add their own, and
# "Bearer Bearer ..." fails as an auth error that names nothing useful.
#
# Leave either empty and that half falls back to asking for configuration:
# an empty MIRROR token gives the "Portal needs its access keys" page, and an
# empty PROXY token lets the dashboard load but fails the calculations.

# NUrul_DB, full access. Used by the dashboard for its own tables.
BUNDLED_MIRROR_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3ODU0ODg4NDQsImV4cCI6NTM4NTU5OTk1NSwiZGJfbmFtZSI6Ik5VcnVsX0RCIiwiYWNjZXNzIjoiZnVsbCIsInByb3h5X3VybCI6Imh0dHBzOi8vcGctcHJveHktcHJvZHVjdGlvbi51cC5yYWlsd2F5LmFwcC8iLCJhcGlfZG9jc191cmwiOiJodHRwczovL3BnLXByb3h5LXByb2R1Y3Rpb24udXAucmFpbHdheS5hcHAvZG9jcyJ9.7duxO0kY1-p6Cd-RDwwqFMQYS2etNfyYDMHLr_OlEYw"
# prod_main, read only. Used by the commission engines for ERP source data.
BUNDLED_PROXY_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3ODQyNTEzMTcsImV4cCI6MTgyMDI1MjQyOCwiZGJfbmFtZSI6InByb2RfbWFpbiIsImFjY2VzcyI6InJlYWRfb25seSIsInByb3h5X3VybCI6Imh0dHBzOi8vcGctcHJveHktcHJvZHVjdGlvbi51cC5yYWlsd2F5LmFwcC8iLCJhcGlfZG9jc191cmwiOiJodHRwczovL3BnLXByb3h5LXByb2R1Y3Rpb24udXAucmFpbHdheS5hcHAvZG9jcyJ9.0phLfmZ3JhBMewbn9bCSndCE4yJ346KUw9-E4GproDo"
# Non-secret, and already the defaults in pgproxy.py. Repeated here so that the
# commission engines -- which have no defaults of their own -- see them too.
BUNDLED_PROXY_URL = "https://pg-proxy-production.up.railway.app/api/sql"
BUNDLED_MIRROR_DB = "NUrul_DB"
BUNDLED_MIRROR_SCHEMA = "dashboard"
BUNDLED_PROXY_DB = "prod_main"


def _load_dotenv_first() -> None:
    """Read .env into the environment before any default is filled in.

    Order matters and is easy to get backwards. pgproxy calls load_dotenv with
    override=False, so whatever is already in os.environ wins over the file. If
    this module seeded its constants first, a real .env would be read and then
    discarded -- a machine with its own credentials would silently run on the
    bundled ones instead, and nothing would report that it had happened.

    So the file is loaded here, before apply() fills any gaps. Same candidates
    and same override=False as pgproxy, so this stays a no-op when pgproxy has
    already run.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, ".env"),
                      os.path.join(os.path.dirname(here), ".env")):
        if os.path.isfile(candidate):
            load_dotenv(candidate, override=False)


def apply() -> dict[str, bool]:
    """Fill in any proxy setting the environment does not already carry.

    Returns a map of name -> whether this module supplied it, so a caller can
    log what it had to provide without ever logging a value.
    """
    _load_dotenv_first()
    supplied: dict[str, bool] = {}
    for name, value in (
        ("PG_MIRROR_TOKEN", BUNDLED_MIRROR_TOKEN),
        ("PG_PROXY_TOKEN", BUNDLED_PROXY_TOKEN),
        ("POSTGRES_PROXY_TOKEN", BUNDLED_PROXY_TOKEN),  # older scripts read this name
        ("PG_PROXY_URL", BUNDLED_PROXY_URL),
        ("PG_MIRROR_DB", BUNDLED_MIRROR_DB),
        ("PG_MIRROR_SCHEMA", BUNDLED_MIRROR_SCHEMA),
        ("PG_PROXY_DB", BUNDLED_PROXY_DB),
    ):
        if not value:
            supplied[name] = False
            continue
        # An empty string in the environment counts as absent. A .env written
        # with a bare "PG_MIRROR_TOKEN=" line is the common way that happens,
        # and treating it as set would defeat the whole point of this module.
        if os.environ.get(name):
            supplied[name] = False
        else:
            os.environ[name] = value
            supplied[name] = True
    return supplied


def have_tokens() -> tuple[bool, bool]:
    """(mirror, proxy) -- whether each token is available from any source."""
    return (bool(os.environ.get("PG_MIRROR_TOKEN") or BUNDLED_MIRROR_TOKEN),
            bool(os.environ.get("PG_PROXY_TOKEN") or BUNDLED_PROXY_TOKEN))
