"""
Security primitives.
 
API keys are treated like passwords:
  - Generated with `secrets` (cryptographically secure randomness, not `random`).
  - Only the SHA-256 HASH is stored. If the database ever leaks, the attacker
    has hashes, not usable keys. The raw key is shown exactly once, at creation.
  - 32 random bytes -> brute-forcing the hash is computationally infeasible,
    which is why plain SHA-256 (no salt/bcrypt) is acceptable for high-entropy
    machine keys, unlike human passwords.
 
Security headers middleware: defense-in-depth HTTP headers on every response.
For a JSON API these mostly guard against a browser rendering a response in a
harmful way if someone opens an endpoint URL directly.
"""
import hashlib
import secrets
 
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
 
 
KEY_PREFIX = "el_"  # makes keys recognizable in logs/leaks scanners
 
 
def generate_key() -> str:
    """Create a new raw API key. Show it to the user ONCE; store only its hash."""
    return KEY_PREFIX + secrets.token_urlsafe(32)
 
 
def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
 
 
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The interactive docs pages need to run their own scripts/styles;
        # the strict lockdown applies to the API responses themselves.
        if request.url.path not in ("/docs", "/redoc", "/openapi.json"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response