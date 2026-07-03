"""
Dexscreener source — free / no-auth. Two roles:

  • DISCOVERY (weak, by design): token-profiles/latest + token-boosts/latest, filtered to
    Solana. Dexscreener has NO true "new pairs" REST endpoint, so these surface tokens that
    are minutes-to-hours old, not first-block. Honest limitation — stated in the README.

  • ENRICHMENT (strong): tokens/v1/solana/{mints} → per-pair liquidity, volume, market cap,
    txns, price, pairCreatedAt. We pick the deepest-liquidity pair per token.

Docs: https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

import os
import sys
import time

# make the solana_screener/ root importable whether this file is run directly
# (python3 sources/dexscreener.py) or imported (from sources import dexscreener)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                       # noqa: E402
from http_client import get_json    # noqa: E402

BASE = "https://api.dexscreener.com"


def _f(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def discover_solana_profiles() -> set[str]:
    """Latest token profiles filtered to Solana → set of mint addresses."""
    d = get_json(f"{BASE}/token-profiles/latest/v1")
    out: set[str] = set()
    if isinstance(d, list):
        for it in d:
            if it.get("chainId") == config.CHAIN and it.get("tokenAddress"):
                out.add(it["tokenAddress"])
    return out


def discover_solana_boosts() -> set[str]:
    """Latest + top boosted tokens filtered to Solana → set of mint addresses."""
    out: set[str] = set()
    for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1"):
        d = get_json(f"{BASE}{path}")
        if isinstance(d, list):
            for it in d:
                if it.get("chainId") == config.CHAIN and it.get("tokenAddress"):
                    out.add(it["tokenAddress"])
    return out


def _pick_pair(pairs, mint):
    """From all pairs returned for a batch, pick the one where `mint` is the base token
    with the deepest USD liquidity (the pool you'd actually trade / exit through)."""
    best, best_liq = None, -1.0
    for p in pairs or []:
        if (p.get("baseToken") or {}).get("address") != mint:
            continue
        liq = _f((p.get("liquidity") or {}).get("usd"))
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def _normalize(p: dict, mint: str, now_s: float) -> dict:
    liq = p.get("liquidity") or {}
    vol = p.get("volume") or {}
    txns = p.get("txns") or {}
    pc = p.get("priceChange") or {}
    h1 = txns.get("h1") or {}
    created_ms = p.get("pairCreatedAt")
    age_min = ((now_s * 1000.0 - created_ms) / 60000.0) if created_ms else float("nan")
    base = p.get("baseToken") or {}
    return {
        "mint": mint,
        "symbol": base.get("symbol", "?"),
        "name": base.get("name", ""),
        "price_usd": _f(p.get("priceUsd")),
        "liq_usd": _f(liq.get("usd")),
        "mcap": _f(p.get("marketCap")),
        "fdv": _f(p.get("fdv")),
        "vol_h1": _f(vol.get("h1")),
        "vol_h6": _f(vol.get("h6")),
        "vol_h24": _f(vol.get("h24")),
        "buys_h1": int(_f(h1.get("buys"))),
        "sells_h1": int(_f(h1.get("sells"))),
        "price_chg_h1": _f(pc.get("h1")),
        "pair_created_ms": created_ms,
        "pair_age_min": age_min,
        "dex": p.get("dexId", ""),
        "url": p.get("url", ""),
    }


def enrich(mints, now_s: float | None = None) -> dict:
    """Batch mints (30 at a time) → {mint: market dict}. `now_s` (live epoch seconds) is
    passed IN by the caller — captured once per run so every token's age is consistent —
    and defaults to time.time() only for standalone use."""
    now_s = time.time() if now_s is None else now_s
    out: dict[str, dict] = {}
    mints = list(dict.fromkeys(mints))  # dedupe, preserve order
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        csv = ",".join(chunk)
        cache = os.path.join(config.CACHE_DIR, f"dex_{chunk[0]}_{len(chunk)}.json")
        data = get_json(f"{BASE}/tokens/v1/{config.CHAIN}/{csv}",
                        cache_path=cache, max_age_sec=config.ENRICH_CACHE_MIN * 60)
        for mint in chunk:
            pair = _pick_pair(data, mint)
            if pair:
                out[mint] = _normalize(pair, mint, now_s)
    return out


def forward_snapshot(mint: str, now_s: float | None = None) -> dict | None:
    """Single-token enrichment for the ledger's forward-return fills."""
    return enrich([mint], now_s=now_s).get(mint)


if __name__ == "__main__":
    prof = discover_solana_profiles()
    boost = discover_solana_boosts()
    print(f"discovery: {len(prof)} profiles, {len(boost)} boosts (solana); "
          f"{len(prof | boost)} unique")
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    m = enrich([USDC])
    if USDC in m:
        d = m[USDC]
        print(f"enrich USDC: {d['symbol']}  price=${d['price_usd']:.4f}  "
              f"liq=${d['liq_usd']:,.0f}  vol24=${d['vol_h24']:,.0f}  "
              f"buys/sells h1={d['buys_h1']}/{d['sells_h1']}  dex={d['dex']}")
    else:
        print("enrich USDC: FAILED")
