
"""
SEC EDGAR client.
 
Design notes:
  - EVERY outbound call goes through _get_json(), which (a) waits on the global
    outbound limiter so we never exceed SEC's rate rules, and (b) sends the
    required User-Agent. There is no other network path — that's deliberate.
  - Unlike LinkLens, there's no SSRF surface here: we only ever contact two
    fixed, hardcoded SEC hosts. User input never becomes a URL host.
  - Parsing is factored into PURE functions (parse_filings, parse_facts) that
    take JSON dicts and return clean data. Pure functions = trivially testable
    with fixtures, no network, no mocks of HTTP plumbing.
"""
import httpx
 
from . import config
from .cache import cache
from .ratelimit import outbound
 
 
class EdgarError(Exception):
    """Upstream or lookup failure that should surface as a clean 4xx/502."""
 
 
# --- Low-level fetch (the ONLY network path) --------------------------------
async def _get_json(url: str, client: httpx.AsyncClient) -> dict:
    await outbound.acquire()  # respect SEC's global rate limit — never skip
    try:
        resp = await client.get(
            url,
            timeout=config.FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": config.SEC_USER_AGENT,
                     "Accept-Encoding": "gzip"},
        )
    except httpx.HTTPError as e:
        raise EdgarError(f"SEC request failed: {type(e).__name__}")
    if resp.status_code == 404:
        raise EdgarError("Not found at SEC (unknown CIK or no data).")
    if resp.status_code != 200:
        raise EdgarError(f"SEC returned HTTP {resp.status_code}.")
    return resp.json()
 
 
# --- Ticker -> CIK ----------------------------------------------------------
async def get_cik(ticker: str, client: httpx.AsyncClient) -> tuple[int, str]:
    """Resolve a ticker to (CIK number, official company name)."""
    ticker = ticker.strip().upper()
    mapping = cache.get("__ticker_map__")
    if mapping is None:
        raw = await _get_json(config.SEC_TICKER_MAP_URL, client)
        # SEC format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        mapping = {v["ticker"].upper(): (int(v["cik_str"]), v["title"])
                   for v in raw.values()}
        cache.set("__ticker_map__", mapping)  # note: shares DATA_TTL; see README
    entry = mapping.get(ticker)
    if entry is None:
        raise EdgarError(f"Unknown ticker '{ticker}'.")
    return entry
 
 
# --- Filings ----------------------------------------------------------------
def parse_filings(submissions: dict, cik: int,
                  form_type: str | None, limit: int) -> list[dict]:
    """Pure function: SEC 'submissions' JSON -> clean list of filings."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
 
    out: list[dict] = []
    for form, date, acc, doc in zip(forms, dates, accs, docs):
        if form_type and form != form_type:
            continue
        acc_nodash = acc.replace("-", "")
        out.append({
            "form": form,
            "filed": date,
            "accession": acc,
            "url": f"{config.SEC_ARCHIVES_BASE}/{cik}/{acc_nodash}/{doc}",
        })
        if len(out) >= limit:
            break
    return out
 
 
async def get_filings(ticker: str, form_type: str | None, limit: int,
                      client: httpx.AsyncClient) -> dict:
    cik, name = await get_cik(ticker, client)
    cache_key = f"filings:{cik}:{form_type}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    subs = await _get_json(config.SEC_SUBMISSIONS_URL.format(cik=cik), client)
    data = {
        "ticker": ticker.upper(),
        "company": name,
        "cik": cik,
        "filings": parse_filings(subs, cik, form_type, limit),
    }
    cache.set(cache_key, data)
    return {**data, "cached": False}
 
 
# --- Financial facts (XBRL) -------------------------------------------------
# A curated whitelist of the concepts most people actually want. The raw
# companyfacts blob has hundreds of tags; this keeps responses sane.
FACT_CONCEPTS = {
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
}
 
 
def _rows_for_tag(tag_data: dict) -> list[dict]:
    """Extract clean, deduped rows for one SEC tag, newest first."""
    for unit, entries in tag_data.get("units", {}).items():
        rows = [
            {"value": e["val"], "unit": unit,
             # period_start matters: for the same period_end, a quarter
             # (3-month) and a year-to-date (6/9-month) figure differ ONLY
             # by their start date. Dropping it made them look like dupes.
             "period_start": e.get("start"),
             "period_end": e.get("end"), "fiscal_year": e.get("fy"),
             "fiscal_period": e.get("fp"), "form": e.get("form")}
            for e in entries if e.get("form") in ("10-K", "10-Q")
        ]
        if not rows:
            continue
        rows.sort(key=lambda r: r["period_end"] or "", reverse=True)
        # Dedupe on (start, end): later filings restate earlier periods as
        # comparatives, producing identical rows. Keep the first (newest) copy.
        seen: set = set()
        deduped = []
        for r in rows:
            key = (r["period_start"], r["period_end"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        return deduped
    return []
 
 
def parse_facts(facts_json: dict, per_concept: int = 4) -> dict:
    """Pure function: SEC companyfacts JSON -> {friendly_name: [recent values]}.
 
    Where several SEC tags map to one concept (e.g. old vs new revenue tags
    after the ASC 606 accounting change), the tag with the MOST RECENT data
    wins — companies switch tags over time, and 'first match' silently served
    years-stale numbers.
    """
    gaap = facts_json.get("facts", {}).get("us-gaap", {})
 
    # Group candidate tags by friendly name.
    candidates: dict[str, list[str]] = {}
    for tag, friendly in FACT_CONCEPTS.items():
        candidates.setdefault(friendly, []).append(tag)
 
    out: dict[str, list] = {}
    for friendly, tags in candidates.items():
        best: list[dict] = []
        for tag in tags:
            if tag not in gaap:
                continue
            rows = _rows_for_tag(gaap[tag])
            if rows and (not best or
                         (rows[0]["period_end"] or "") > (best[0]["period_end"] or "")):
                best = rows
        if best:
            out[friendly] = best[:per_concept]
    return out
 
 
async def get_facts(ticker: str, client: httpx.AsyncClient) -> dict:
    cik, name = await get_cik(ticker, client)
    cache_key = f"facts:{cik}"
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    raw = await _get_json(config.SEC_FACTS_URL.format(cik=cik), client)
    data = {
        "ticker": ticker.upper(),
        "company": name,
        "cik": cik,
        "facts": parse_facts(raw),
    }
    cache.set(cache_key, data)
    return {**data, "cached": False}
