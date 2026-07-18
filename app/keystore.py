"""
SQLite-backed API key store. Only key HASHES are stored (see security.py).

SQLite is the right call at this scale: zero setup, a single file, safe for
many readers + occasional writes (key creation). Lookups are indexed by the
hash (primary key), so verification is O(log n) and constant-time-ish — and
because the client must present a key that hashes to an exact match, timing
side channels on the lookup are not a practical concern here.
"""
import sqlite3
import time

from . import config
from .security import generate_key, hash_key


class KeyStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # A connection per operation: simple and safe across threads/workers.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")  # readers don't block writers
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS api_keys (
                       key_hash TEXT PRIMARY KEY,
                       tier     TEXT NOT NULL,
                       name     TEXT NOT NULL,
                       created  REAL NOT NULL,
                       active   INTEGER NOT NULL DEFAULT 1
                   )"""
            )

    def create_key(self, name: str, tier: str) -> str:
        """Mint a key, store its hash, return the RAW key (shown once only)."""
        if tier not in config.TIERS:
            raise ValueError(f"Unknown tier '{tier}'. Valid: {sorted(config.TIERS)}")
        raw = generate_key()
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_keys (key_hash, tier, name, created) VALUES (?,?,?,?)",
                (hash_key(raw), tier, name, time.time()),
            )
        return raw

    def verify(self, raw_key: str) -> str | None:
        """Return the key's tier if valid+active, else None."""
        if not raw_key:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT tier FROM api_keys WHERE key_hash = ? AND active = 1",
                (hash_key(raw_key),),
            ).fetchone()
        return row[0] if row else None

    def revoke(self, raw_key: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE api_keys SET active = 0 WHERE key_hash = ?",
                (hash_key(raw_key),),
            )
        return cur.rowcount > 0

    def ensure_demo_keys(self) -> None:
        """Insert well-known demo keys for local testing. Disabled in prod via
        ALLOW_DEMO_KEYS=false — demo keys are public knowledge, never ship them."""
        demo = {"demo-key-free": "free", "demo-key-pro": "pro"}
        with self._conn() as c:
            for raw, tier in demo.items():
                c.execute(
                    "INSERT OR IGNORE INTO api_keys (key_hash, tier, name, created) "
                    "VALUES (?,?,?,?)",
                    (hash_key(raw), tier, f"demo-{tier}", time.time()),
                )


keystore = KeyStore(config.DB_PATH)
if config.ALLOW_DEMO_KEYS:
    keystore.ensure_demo_keys()
