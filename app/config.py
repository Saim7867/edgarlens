"""
EdgarLens configuration. Everything env-overridable.

IMPORTANT — before deploying you MUST set SEC_USER_AGENT to include YOUR real
contact info. The SEC requires it and blocks anonymous clients:
    export SEC_USER_AGENT="EdgarLens/1.0 (your-email@example.com)"
"""
import os

# --- SEC upstream ----------------------------------------------------------
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "EdgarLens/1.0 (set-your-email@example.com)")
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# SEC's published fair-access limit is 10 requests/sec per client. We cap our
# OUTBOUND rate at 8/sec globally (all users combined) to stay safely under it.
# Violating this gets your server's IP banned — this limiter is not optional.
SEC_MAX_RPS = float(os.getenv("SEC_MAX_RPS", 8.0))

FETCH_TIMEOUT_SECONDS = float(os.getenv("FETCH_TIMEOUT_SECONDS", 15.0))

# --- Cache -----------------------------------------------------------------
# The ticker->CIK map changes rarely: cache 24h. Filings/facts: 15 min is a
# good freshness/traffic balance (new filings appear throughout the day).
TICKER_MAP_TTL = int(os.getenv("TICKER_MAP_TTL", 60 * 60 * 24))
DATA_TTL = int(os.getenv("DATA_TTL", 60 * 15))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", 5_000))

# --- API keys --------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "edgarlens.db")
# Demo keys are auto-created ONLY when this is true. Set to "false" in
# production and mint real keys with: python scripts/make_key.py <name> <tier>
ALLOW_DEMO_KEYS = os.getenv("ALLOW_DEMO_KEYS", "true").lower() == "true"

# --- Per-key rate limit tiers (token bucket) -------------------------------
TIERS = {
    "free": {"refill_per_sec": 30 / 60, "capacity": 10},    # ~30 req/min
    "pro":  {"refill_per_sec": 300 / 60, "capacity": 60},   # ~300 req/min
}

# --- Input validation ------------------------------------------------------
TICKER_PATTERN = r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$"   # AAPL, BRK.B, RDS-A ...
ALLOWED_FORMS = {"10-K", "10-Q", "8-K", "4", "13F-HR", "S-1", "DEF 14A", "20-F", "6-K"}
MAX_FILINGS_LIMIT = 50
RAPIDAPI_PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET", "")