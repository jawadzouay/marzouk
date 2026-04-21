from supabase import create_client, Client
from dotenv import load_dotenv
import os

load_dotenv()

# Lazy singleton — we used to call create_client() at module import time,
# which meant any Supabase-side hiccup (slow DNS, unset env var, TLS retry)
# would stall uvicorn's startup and make Railway's /health probe time out
# before the app could answer. Deferring construction to first use keeps
# import lightweight.
_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY env vars are missing"
            )
        _client = create_client(url, key)
    return _client
