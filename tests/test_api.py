"""
Run: python -m pytest -q
 
SEC endpoints are NOT hit by these tests. Parsers are pure functions tested on
fixtures shaped exactly like real SEC responses; endpoint tests monkeypatch the
edgar layer. Live smoke-testing happens on your machine via /docs.
"""
import asyncio
import os
import time
 
import pytest
from fastapi.testclient import TestClient
 
# Isolated DB per test run so tests never touch a real keys DB.
os.environ["DB_PATH"] = "test_keys.db"
 
from app import config, edgar, main                       # noqa: E402
from app.edgar import parse_facts, parse_filings          # noqa: E402
from app.keystore import KeyStore                         # noqa: E402
from app.ratelimit import OutboundLimiter, RateLimiter    # noqa: E402
from app.security import KEY_PREFIX, generate_key, hash_key  # noqa: E402
 
 
@pytest.fixture(autouse=True, scope="module")
def _cleanup_db():
    yield
    for f in ("test_keys.db", "test_keys.db-wal", "test_keys.db-shm"):
        try:
            if os.path.exists(f):
                os.remove(f)
        except PermissionError:
            pass  # Windows still holds the file; harmless
 
 
# --- Security primitives ----------------------------------------------------
def test_generated_keys_are_unique_and_prefixed():
    a, b = generate_key(), generate_key()
    assert a != b and a.startswith(KEY_PREFIX) and len(a) > 30
 
 
def test_hash_is_deterministic_and_not_reversible_looking():
    raw = generate_key()
    assert hash_key(raw) == hash_key(raw)
    assert raw not in hash_key(raw)
 
 
# --- Key store ---------------------------------------------------------------
def test_keystore_create_verify_revoke(tmp_path):
    ks = KeyStore(str(tmp_path / "k.db"))
    raw = ks.create_key("tester", "pro")
    assert ks.verify(raw) == "pro"          # valid key -> tier
    assert ks.verify("wrong-key") is None    # bad key -> None
    assert ks.verify("") is None             # empty -> None
    assert ks.revoke(raw) is True
    assert ks.verify(raw) is None            # revoked -> None
 
 
def test_keystore_rejects_unknown_tier(tmp_path):
    ks = KeyStore(str(tmp_path / "k2.db"))
    with pytest.raises(ValueError):
        ks.create_key("x", "enterprise-mega")
 
 
# --- Rate limiters -----------------------------------------------------------
def test_per_key_bucket_bursts_then_blocks():
    rl = RateLimiter()
    results = [asyncio.run(rl.check("h", "free"))[0] for _ in range(11)]
    assert results[:10] == [True] * 10 and results[10] is False
 
 
def test_outbound_limiter_paces_calls():
    lim = OutboundLimiter(max_rps=50)  # 50/s so the test is fast
 
    async def burst():
        start = time.monotonic()
        for _ in range(60):
            await lim.acquire()
        return time.monotonic() - start
 
    took = asyncio.run(burst())
    # 50 tokens free, 10 more need refill at 50/s -> at least ~0.2s of pacing
    assert took >= 0.15
 
 
# --- Parsers (pure, fixture-driven) -----------------------------------------
SUBMISSIONS_FIXTURE = {
    "filings": {"recent": {
        "form": ["10-K", "8-K", "10-Q"],
        "filingDate": ["2026-02-01", "2026-03-15", "2026-05-01"],
        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002",
                            "0000320193-26-000003"],
        "primaryDocument": ["aapl-10k.htm", "aapl-8k.htm", "aapl-10q.htm"],
    }}
}
 
 
def test_parse_filings_filters_and_builds_urls():
    rows = parse_filings(SUBMISSIONS_FIXTURE, cik=320193, form_type="10-K", limit=10)
    assert len(rows) == 1
    assert rows[0]["form"] == "10-K"
    assert rows[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/aapl-10k.htm"
    )
 
 
def test_parse_filings_respects_limit():
    rows = parse_filings(SUBMISSIONS_FIXTURE, cik=320193, form_type=None, limit=2)
    assert len(rows) == 2
 
 
FACTS_FIXTURE = {
    "facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            {"val": 100, "start": "2025-01-01", "end": "2025-12-31",
             "fy": 2025, "fp": "FY", "form": "10-K"},
            {"val": 90, "start": "2024-01-01", "end": "2024-12-31",
             "fy": 2024, "fp": "FY", "form": "10-K"},
            {"val": 5, "start": "2019-01-01", "end": "2020-01-01",
             "fy": 2019, "fp": "FY", "form": "S-1"},
        ]}},
    }}
}
 
 
def test_parse_facts_filters_forms_and_sorts_newest_first():
    out = parse_facts(FACTS_FIXTURE)
    rows = out["net_income"]
    assert [r["value"] for r in rows] == [100, 90]   # S-1 row excluded, sorted desc
    assert rows[0]["period_start"] == "2025-01-01"   # start date now included
 
 
# Bug 1 regression: Apple stopped using the old "Revenues" tag after the
# ASC 606 change; first-match parsing served 2018 data forever.
STALE_TAG_FIXTURE = {
    "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [                       # OLD tag, stale
            {"val": 265, "start": "2017-10-01", "end": "2018-09-29",
             "fy": 2018, "fp": "FY", "form": "10-K"},
        ]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"val": 400, "start": "2024-10-01", "end": "2025-09-27",  # NEW tag, fresh
             "fy": 2025, "fp": "FY", "form": "10-K"},
        ]}},
    }}
}
 
 
def test_parse_facts_prefers_tag_with_most_recent_data():
    out = parse_facts(STALE_TAG_FIXTURE)
    assert out["revenue"][0]["value"] == 400          # fresh tag wins
    assert out["revenue"][0]["period_end"] == "2025-09-27"
 
 
# Bug 2 regression: later filings restate earlier periods as comparatives,
# creating identical (start, end) rows; and quarter vs year-to-date rows share
# an end date but differ by start — those must be KEPT, not deduped.
DUPES_FIXTURE = {
    "facts": {"us-gaap": {
        "Assets": {"units": {"USD": [
            {"val": 359, "start": None, "end": "2025-09-27",
             "fy": 2025, "fp": "FY", "form": "10-K"},
            {"val": 359, "start": None, "end": "2025-09-27",   # restated dupe
             "fy": 2026, "fp": "Q1", "form": "10-Q"},
        ]}},
        "NetIncomeLoss": {"units": {"USD": [
            {"val": 71, "start": "2025-09-28", "end": "2026-03-28",  # 6-mo YTD
             "fy": 2026, "fp": "Q2", "form": "10-Q"},
            {"val": 29, "start": "2025-12-28", "end": "2026-03-28",  # 3-mo qtr
             "fy": 2026, "fp": "Q2", "form": "10-Q"},
        ]}},
    }}
}
 
 
def test_parse_facts_drops_restated_dupes_but_keeps_qtr_vs_ytd():
    out = parse_facts(DUPES_FIXTURE)
    assert len(out["total_assets"]) == 1              # identical restated row dropped
    assert len(out["net_income"]) == 2                # different starts -> both kept
    starts = {r["period_start"] for r in out["net_income"]}
    assert starts == {"2025-09-28", "2025-12-28"}
 
 
# --- Endpoints (edgar layer monkeypatched, no network) ----------------------
client = TestClient(main.app)
 
 
def test_health_no_auth_and_security_headers():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
 
 
def test_missing_key_401():
    assert client.get("/company/AAPL").status_code == 401
 
 
def test_bad_ticker_422():
    r = client.get("/company/AAPL;DROP", headers={"X-API-Key": "demo-key-free"})
    assert r.status_code == 422
 
 
def test_bad_form_type_422():
    r = client.get("/filings/AAPL?form=EVIL",
                   headers={"X-API-Key": "demo-key-free"})
    assert r.status_code == 422
 
 
def test_company_endpoint_happy_path(monkeypatch):
    async def fake_get_cik(ticker, _client):
        return 320193, "Apple Inc."
    monkeypatch.setattr(edgar, "get_cik", fake_get_cik)
    r = client.get("/company/aapl", headers={"X-API-Key": "demo-key-free"})
    assert r.status_code == 200
    assert r.json() == {"ticker": "AAPL", "company": "Apple Inc.", "cik": 320193}