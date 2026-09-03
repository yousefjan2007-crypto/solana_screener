"""
Shared HTTP client for solana_screener — pure stdlib urllib + certifi SSL, per-host
token-bucket throttle, 3-retry with backoff, transparent gzip, optional JSON disk cache.
Returns None on failure so one bad token never kills a run.

Same idiom as signal_lab/deepvalue/edgar_client.py, generalized to throttle per host so
Dexscreener and RugCheck each stay under their own rate ceiling.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request

import certifi

import config

_SSL = ssl.create_default_context(cafile=certifi.where())
_last_call: dict[str, float] = {}
_throttle_lock = threading.Lock()

_HOST_HZ = {
    "api.dexscreener.com": config.DEXSCREENER_RATE_HZ,
    "api.rugcheck.xyz": config.RUGCHECK_RATE_HZ,
    "openapi.gmgn.ai": 1.0,   # documented default limit: 1 req/s
    "lite-api.jup.ag": 1.0,   # Jupiter free tier — stay conservative
    "api.geckoterminal.com": config.GECKOTERMINAL_RATE_HZ,   # hard 30/min free tier
}


def _throttle(host: str) -> None:
    """Wait until this host's next slot is due.

    Held under a lock, and the sleep happens INSIDE it — that is the whole point.
    Unsynchronised, N threads all read the same stale _last_call, all compute the
    same gap, all sleep the same interval and all fire together, so a "1 Hz" host
    ceiling becomes N requests in one instant. That is not hypothetical here:
    solana_screener/listener.py evaluates pump.fun migrations concurrently, and
    graduations arrive in clusters. RugCheck's limit is undocumented, which is
    exactly why it is pinned low and must not be burst through.
    """
    with _throttle_lock:
        hz = _HOST_HZ.get(host, 2.0)
        gap = 1.0 / hz
        dt = time.monotonic() - _last_call.get(host, 0.0)
        if dt < gap:
            time.sleep(gap - dt)
        _last_call[host] = time.monotonic()


def get_json(url: str, cache_path: str | None = None, max_age_sec: float | None = None,
             headers: dict | None = None):
    """GET a JSON document. If cache_path is fresh (< max_age_sec) return it without a
    network call. Returns the parsed object, or None on any failure."""
    if cache_path and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if max_age_sec is None or age <= max_age_sec:
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception:
                pass

    host = urllib.parse.urlparse(url).netloc
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"}
    if headers:
        hdrs.update(headers)

    for attempt in range(config.HTTP_RETRIES):
        try:
            _throttle(host)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT, context=_SSL) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
            if cache_path:
                try:
                    with open(cache_path, "w") as f:
                        json.dump(data, f)
                except Exception:
                    pass
            return data
        except Exception as exc:
            if attempt == config.HTTP_RETRIES - 1:
                print(f"  [http] failed {url}: {exc}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


if __name__ == "__main__":
    # smoke test: fetch a known liquid token (USDC) from Dexscreener, then prove caching.
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    url = f"https://api.dexscreener.com/tokens/v1/solana/{USDC}"
    d = get_json(url)
    n = len(d) if isinstance(d, list) else "?"
    print(f"dexscreener tokens/v1 USDC: {'ok' if d else 'FAILED'}  ({n} pairs)")

    cache = os.path.join(config.CACHE_DIR, "_smoke.json")
    if os.path.exists(cache):
        os.remove(cache)
    get_json(url, cache_path=cache, max_age_sec=300)   # populates cache (miss)
    t0 = time.monotonic()
    get_json(url, cache_path=cache, max_age_sec=300)   # should be a cache hit (no throttle)
    dt = time.monotonic() - t0
    print(f"cache: {'hit' if os.path.exists(cache) else 'miss'}  (2nd read {dt*1000:.0f} ms)")
