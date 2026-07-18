"""
Mint a production API key.

    python scripts/make_key.py "customer name" free
    python scripts/make_key.py "acme corp" pro

Prints the raw key ONCE. It is never stored — only its hash is. If a customer
loses their key, revoke it and mint a new one.
"""
import sys

sys.path.insert(0, ".")  # allow running from the project root
from app.keystore import keystore  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    name, tier = sys.argv[1], sys.argv[2]
    raw = keystore.create_key(name, tier)
    print(f"API key for '{name}' (tier: {tier}):\n\n    {raw}\n")
    print("Save it now — it cannot be recovered, only revoked and re-issued.")


if __name__ == "__main__":
    main()
