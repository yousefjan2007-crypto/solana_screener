"""
GMGN OpenAPI source — the second opinion RugCheck can't give. GMGN tags wallets
behaviorally (bundler / sniper / rat-trader / fresh / smart money) from trade patterns,
which catches wallet-farm scams that funding-graph clustering misses entirely:
$Cubrate post-mortem — RugCheck insiderNetworks: 0 (before AND after the rug);
GMGN: 88 bundler wallets, 11 snipers, and 62 real holders vs the 1,344 inflated ones.

Auth: X-APIKEY header + timestamp/client_id query params (per GMGNAI/gmgn-skills).
Key comes from config.load_credentials() — config.local.json locally, the
GMGN_API_KEY GitHub Secret in the cloud. GRACEFUL DEGRADATION IS MANDATORY: no key or
a failed call returns {}, and every gate keyed on these fields passes-through — the
screener must never go dark because one vendor did.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                       # noqa: E402
from http_client import get_json    # noqa: E402

BASE = "https://openapi.gmgn.ai"


def _f(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def token_features(mint: str) -> dict:
    """Behavioral wallet-tag stats for a mint → flat gate-input dict, or {} when the
    key is absent / the call fails (gates then pass through)."""
    key = config.load_credentials().get("gmgn_api_key")
    if not key:
        return {}
    q = urllib.parse.urlencode({"chain": "sol", "address": mint,
                                "timestamp": int(time.time()),
                                "client_id": str(uuid.uuid4())})
    cache = os.path.join(config.CACHE_DIR, f"gmgn_{mint}.json")
    d = get_json(f"{BASE}/v1/token/info?{q}", cache_path=cache,
                 max_age_sec=config.REPORT_CACHE_MIN * 60,
                 headers={"X-APIKEY": key, "Content-Type": "application/json"})
    data = (d or {}).get("data") or {}
    stat = data.get("stat") or {}
    tags = data.get("wallet_tags_stat") or {}
    if not stat and not tags:
        return {}
    holders = _f(stat.get("holder_count"))
    bundlers = _f(tags.get("bundler_wallets"))
    return {
        "gmgn_ok": True,
        # bundlers RELATIVE to holders — raw count scales with volume (WIF: 1,000
        # bundlers, ratio 0.002; the Cubrate wallet-farm rug: 88 bundlers, ratio 1.42)
        "gmgn_bundler_ratio": bundlers / max(holders, 1.0),
        "gmgn_bundler_wallets": int(_f(tags.get("bundler_wallets"))),
        "gmgn_sniper_wallets": int(_f(tags.get("sniper_wallets"))),
        "gmgn_rat_wallets": int(_f(tags.get("rat_trader_wallets"))),
        "gmgn_smart_wallets": int(_f(tags.get("smart_wallets"))),
        "gmgn_sniper_hold_pct": _f(stat.get("top70_sniper_hold_rate")) * 100,
        "gmgn_insider_hold_pct": _f(stat.get("suspected_insider_hold_rate")) * 100,
        "gmgn_bundler_vol_pct": _f(stat.get("top_bundler_trader_percentage")) * 100,
        "gmgn_rat_vol_pct": _f(stat.get("top_rat_trader_percentage")) * 100,
        "gmgn_fresh_wallet_pct": _f(stat.get("fresh_wallet_rate")) * 100,
        "gmgn_holders": int(_f(stat.get("holder_count"))),
    }


if __name__ == "__main__":
    CUBRATE = "HMX8oxVFtpxz92NeoSifkQrJATwEpKQMDjVBbrmppump"  # known wallet-farm rug
    feat = token_features(CUBRATE)
    if not feat:
        print("gmgn: no key configured (or call failed) — source degrades gracefully")
    else:
        print("gmgn features for the Cubrate rug (expect heavy bundler count):")
        for k, v in feat.items():
            print(f"  {k:24s} {v}")
