"""
EdgarLens API — clean access to SEC EDGAR by ticker.

  GET /health                     liveness (no auth)
  GET /company/{ticker}           resolve ticker -> CIK + official name
  GET /filings/{ticker}           recent filings, optional ?form=10-K&limit=10
  GET /facts/{ticker}             key financials from official XBRL data

Auth: X-API-Key header. Keys are stored hashed; demo keys exist only while
ALLOW_DEMO_KEYS=true. Mint real keys: python scripts/make_key.py <name> <tier>

Run:  python -m uvicorn app.main:app --reload
"""
import re
import secrets

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from . import config, edgar
from .keystore import keystore
from .ratelimit import limiter
from .security import SecurityHeadersMiddleware, hash_key

app = FastAPI(
    title="EdgarLens API",
    version="1.0.0",
    description="Clean, ticker-based access to SEC EDGAR filings and financials. "
    "Data source: U.S. SEC (public domain). Not investment advice.",
)
app.add_middleware(SecurityHeadersMiddleware)

_client: httpx.AsyncClient | None = None
_TICKER_RE = re.compile(config.TICKER_PATTERN)


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client:
        await _client.aclose()


# --- Auth + per-key rate limit ---------------------------------------------
async def authorize(
    x_api_key: str = Header(default=""),
    x_rapidapi_proxy_secret: str = Header(default=""),
) -> str:
    # Path 1: request came through RapidAPI's proxy (they billed the customer).
    if config.RAPIDAPI_PROXY_SECRET and secrets.compare_digest(
        x_rapidapi_proxy_secret, config.RAPIDAPI_PROXY_SECRET
    ):
        tier = "pro"
    # Path 2: one of our own keys.
    else:
        tier = keystore.verify(x_api_key)
    if tier is None:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")
    # Rate-limit by the key's HASH so raw keys never sit in limiter memory.
    allowed, retry_after = await limiter.check(hash_key(x_api_key), tier)
    if not allowed:
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded.",
            headers={"Retry-After": str(round(retry_after, 2))},
        )
    return tier


# --- Validation helpers -----------------------------------------------------
def _validate_ticker(ticker: str) -> str:
    """Strict whitelist validation. Anything not matching is rejected outright —
    never 'cleaned up'. This is the front door; keep it narrow."""
    if not _TICKER_RE.fullmatch(ticker):
        raise HTTPException(status_code=422, detail="Invalid ticker format.")
    return ticker.upper()


def _validate_form(form: str | None) -> str | None:
    if form is None:
        return None
    form = form.upper()
    if form not in config.ALLOWED_FORMS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported form type. Allowed: {sorted(config.ALLOWED_FORMS)}",
        )
    return form

@app.get("/", include_in_schema=False)
async def root():
    return {"service": "EdgarLens API", "docs": "/docs"}

# --- Routes -----------------------------------------------------------------
@app.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/company/{ticker}")
async def company(ticker: str = Path(...), _tier: str = Depends(authorize)):
    ticker = _validate_ticker(ticker)
    try:
        cik, name = await edgar.get_cik(ticker, _client)
    except edgar.EdgarError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    return {"ticker": ticker, "company": name, "cik": cik}


@app.get("/filings/{ticker}")
async def filings(
    ticker: str = Path(...),
    form: str | None = Query(default=None, description="e.g. 10-K, 10-Q, 8-K"),
    limit: int = Query(default=10, ge=1, le=config.MAX_FILINGS_LIMIT),
    _tier: str = Depends(authorize),
):
    ticker = _validate_ticker(ticker)
    form = _validate_form(form)
    try:
        return await edgar.get_filings(ticker, form, limit, _client)
    except edgar.EdgarError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/facts/{ticker}")
async def facts(ticker: str = Path(...), _tier: str = Depends(authorize)):
    ticker = _validate_ticker(ticker)
    try:
        return await edgar.get_facts(ticker, _client)
    except edgar.EdgarError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
