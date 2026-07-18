# EdgarLens API

Clean, ticker-based access to SEC EDGAR — company filings and key financials
as friendly JSON. Data source: the U.S. SEC (public domain, explicitly free,
fully legal to use and resell access to).

```
GET /company/AAPL              -> CIK + official name
GET /filings/AAPL?form=10-K    -> recent filings with direct document URLs
GET /facts/AAPL                -> revenue, net income, EPS, assets... from XBRL
```

Why people pay for this: the SEC's raw API is free but hostile — CIK numbers
instead of tickers, zero-padded URLs, giant unfiltered JSON blobs. sec-api.io
charges $50+/mo for a friendly layer over the same data. This is that layer.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                          # 14 passed
python -m uvicorn app.main:app --reload
```

Open http://localhost:8000/docs, Authorize with `demo-key-free`, try
`/filings/AAPL`.

**Before your first real request**, set your contact info (the SEC requires it
and blocks anonymous clients):

```bash
# Windows PowerShell:            macOS/Linux:
$env:SEC_USER_AGENT="EdgarLens/1.0 (you@example.com)"     export SEC_USER_AGENT="EdgarLens/1.0 (you@example.com)"
```

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /health` | liveness (no auth) |
| `GET /company/{ticker}` | CIK number + official company name |
| `GET /filings/{ticker}?form=&limit=` | recent filings; filter by 10-K, 10-Q, 8-K, 4, S-1, DEF 14A, 13F-HR, 20-F, 6-K |
| `GET /facts/{ticker}` | recent values for revenue, net income, diluted EPS, assets, liabilities, cash — sourced from official XBRL 10-K/10-Q data |

All authed endpoints take header `X-API-Key`.

## Security model (what's protecting what)

- **Hashed API keys.** Keys are 32 bytes of `secrets` randomness; only their
  SHA-256 hash is stored (SQLite). A leaked DB yields no usable keys. Raw keys
  are printed once at mint time and never again.
- **Per-key token-bucket rate limiting** with `Retry-After` on 429. Limiter is
  keyed by the key's hash, so raw keys never sit in limiter memory.
- **Global outbound limiter (8 req/s)** — the critical one. The SEC's fair-access
  policy caps clients at 10 req/s and bans violators' IPs. All users' requests
  share one paced budget; the app physically cannot exceed it.
- **Strict input validation.** Tickers must match `^[A-Za-z][A-Za-z0-9.\-]{0,9}$`;
  form types come from a fixed whitelist; limits are bounded. Invalid input is
  rejected, never "cleaned."
- **No SSRF surface.** Unlike a URL-fetching service, user input never becomes a
  URL host — the app only ever contacts two hardcoded SEC hosts.
- **Security headers** on every response (nosniff, DENY framing, no-referrer,
  restrictive CSP, no-store).
- **No secrets in code.** Config via environment variables. Demo keys exist only
  while `ALLOW_DEMO_KEYS=true` — set it `false` in production, they're public.

## Managing keys

```bash
python scripts/make_key.py "customer name" free
python scripts/make_key.py "acme corp" pro
```

Prints the raw key once. Tiers (edit in `config.py`): free ~30 req/min,
pro ~300 req/min.

## Deploying (Render free tier)

1. Push this folder to a GitHub repo.
2. Render → New Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Environment variables: `SEC_USER_AGENT` (your real email), `ALLOW_DEMO_KEYS=false`.
5. Note: Render's free-tier disk is ephemeral — your SQLite key DB resets on
   redeploy. Fine for testing; for real customers attach a persistent disk
   (paid) or move keys to a hosted Postgres (free tiers exist).

## Scaling notes (read when you outgrow one process)

- Limiters and cache are in-memory, per-process. With `--workers N`: per-key
  limits become N× looser and the outbound budget becomes N×8/s — either set
  `SEC_MAX_RPS` to `8/N`, or centralize both in Redis.
- The ticker map cache entry currently shares the 15-min `DATA_TTL` rather than
  its own 24h TTL — harmless (one extra SEC call per 15 min), and a nice small
  upgrade if you want one: give `TTLCache` per-key TTLs.

## Compliance

- SEC data is public domain; commercial reuse is allowed.
- This API serves factual filing data. It is **not investment advice** — keep
  that disclaimer in your docs and marketing. Don't add "buy/sell signals";
  that changes your regulatory exposure entirely.

## The honest business read

- Proven category with paying customers (sec-api.io et al.), and unlike social
  scrapers this is legally clean — the data source can't be yanked away.
- You are still the new entrant against polished incumbents. Realistic path:
  list on RapidAPI freemium, write good docs, target the underserved hobbyist/
  student quant niche that finds $50/mo too steep. Realistic outcome: $0 for a
  while, then possibly low hundreds/mo with sustained marketing effort.
- The durable value either way: this is a strong portfolio piece — auth,
  hashing, two-sided rate limiting, upstream compliance, input validation, and
  tests, on a real government data source.

## Tests

```bash
python -m pytest -q     # 14 tests: security, keystore, limiters, parsers, endpoints
```

Tests never hit the SEC (parsers are pure functions tested on realistic
fixtures). Live smoke test = run the server and hit `/filings/AAPL` in /docs.
