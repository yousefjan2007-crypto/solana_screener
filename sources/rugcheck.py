"""
RugCheck source — free / no-auth (https://api.rugcheck.xyz). Two roles:

  • DISCOVERY: /v1/stats/new_tokens → recently detected mints. This is the primary
    new-launch feed (Dexscreener can't do it).

  • SAFETY: /v1/tokens/{mint}/report → authorities, LP lock, holders, risk score, rugged
    flag. safety_features() normalizes the report into the flat dict the gates consume.

Authority semantics (load-bearing): mintAuthority / freezeAuthority are `null` when
REVOKED (good) or a base58 address when STILL ACTIVE (a supply-flood / honeypot risk).
null == revoked == good.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                       # noqa: E402
from http_client import get_json    # noqa: E402

BASE = "https://api.rugcheck.xyz/v1"

# addresses that legitimately hold big balances but are NOT dump risk (exclude from
# holder-concentration): the SPL incinerator (burn) and the system program / null.
BURN_ADDRS = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

# RugCheck knownAccounts.type values that are infrastructure (pool/locker/burn) rather
# than a dump risk, so they're excluded from holder concentration. CREATOR is deliberately
# NOT here — dev holdings ARE a dump risk and get their own gate (dev_pct).
EXCLUDE_TYPES = {"AMM", "LOCKER", "MARKET", "POOL", "DEX", "BURN", "INCINERATOR", "VAULT"}


def _f(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _is_revoked(auth) -> bool:
    """True when the authority is revoked (null/empty/system) i.e. safe."""
    if auth in (None, "", "null", "None"):
        return True
    return auth in BURN_ADDRS


def discover_new_tokens() -> list[str]:
    """Recently detected Solana mints (the primary early-discovery feed)."""
    d = get_json(f"{BASE}/stats/new_tokens")
    out: list[str] = []
    if isinstance(d, list):
        for it in d:
            mint = it.get("mint") or it.get("address")
            if mint:
                out.append(mint)
    return out


def report(mint: str) -> dict | None:
    """Full safety report for a mint (cached ~10 min)."""
    cache = os.path.join(config.CACHE_DIR, f"rug_{mint}.json")
    return get_json(f"{BASE}/tokens/{mint}/report",
                    cache_path=cache, max_age_sec=config.REPORT_CACHE_MIN * 60)


def safety_features(rep: dict | None) -> dict:
    """Normalize a rugcheck report → the flat gate-input dict. Defensive to missing keys
    and to pct values that come as either fractions (0-1) or percents (0-100)."""
    if not rep:
        return {}
    token = rep.get("token") or {}
    mint_auth = token.get("mintAuthority", rep.get("mintAuthority"))
    freeze_auth = token.get("freezeAuthority", rep.get("freezeAuthority"))

    # LP locked %: max across markets, with a summary-endpoint fallback.
    lp_locked = 0.0
    for mk in rep.get("markets") or []:
        lp = mk.get("lp") or {}
        lp_locked = max(lp_locked, _f(lp.get("lpLockedPct")))
    if lp_locked == 0.0 and rep.get("lpLockedPct") is not None:
        lp_locked = _f(rep.get("lpLockedPct"))

    # Addresses to exclude from holder concentration: pool vaults / lockers / burn. They
    # hold big balances but are NOT a dump risk. RugCheck's knownAccounts maps a WALLET
    # (a holder's `owner`, not its token-account `address`) to a labeled type.
    known = rep.get("knownAccounts") or {}
    infra_owners = {addr for addr, meta in known.items()
                    if str((meta or {}).get("type", "")).upper() in EXCLUDE_TYPES}
    infra_owners |= BURN_ADDRS

    # Holder concentration. Detect the pct scale (percent vs fraction) from the data, then
    # drop infra owners before summing the top 10.
    holders = rep.get("topHolders") or []
    raw_pcts = [_f(h.get("pct")) for h in holders]
    scale = 100.0 if (raw_pcts and max(raw_pcts) <= 1.5) else 1.0

    def _is_infra(h) -> bool:
        return (h.get("owner") in infra_owners) or (h.get("address") in BURN_ADDRS)

    conc = sorted((h for h in holders if not _is_infra(h)),
                  key=lambda h: _f(h.get("pct")), reverse=True)
    top10_pct = sum(_f(h.get("pct")) for h in conc[:10]) * scale
    insider_pct = sum(_f(h.get("pct")) for h in holders if h.get("insider")) * scale

    # Dev / creator holdings as a % of supply, if the fields are present.
    supply = _f(token.get("supply"))
    creator_bal = _f(rep.get("creatorBalance"))
    dev_pct = (creator_bal / supply * 100.0) if supply > 0 else 0.0

    # Insider networks — RugCheck's own funding-graph clustering (wallets linked by
    # transfers/common funding). This is the signal that catches BUNDLED launches which
    # look clean on top-10 concentration: supply split across many "unrelated" fresh
    # wallets that are actually one entity. tokenAmount is in raw units; supply-relative.
    nets = rep.get("insiderNetworks") or []
    insider_net_amount = sum(_f(n.get("tokenAmount")) for n in nets)
    insider_networks_pct = (insider_net_amount / supply * 100.0) if supply > 0 else 0.0
    graph_insiders = int(_f(rep.get("graphInsidersDetected")))

    # Serial-deployer history: creatorTokens lists the creator's PRIOR launches with
    # their current market caps. Many prior launches, mostly dead => scam factory.
    prior = rep.get("creatorTokens") or []
    creator_prior_tokens = len(prior)
    dead = sum(1 for t in prior if _f(t.get("marketCap")) < config.CREATOR_DEAD_MCAP_USD)
    creator_dead_frac = (dead / creator_prior_tokens) if creator_prior_tokens else 0.0

    risks = rep.get("risks") or []
    danger = [r.get("name", "") for r in risks
              if str(r.get("level", "")).lower() == "danger"]

    score = rep.get("score_normalised",
                    rep.get("scoreNormalised", rep.get("score")))
    total_holders = int(_f(rep.get("totalHolders"))) or len(holders)

    return {
        "mint_authority_active": not _is_revoked(mint_auth),
        "freeze_authority_active": not _is_revoked(freeze_auth),
        "lp_locked_pct": lp_locked,
        "top10_pct": top10_pct,
        "insider_pct": insider_pct,
        "insider_networks_pct": insider_networks_pct,
        "graph_insiders": graph_insiders,
        "creator_prior_tokens": creator_prior_tokens,
        "creator_dead_frac": creator_dead_frac,
        "dev_pct": dev_pct,
        "total_holders": total_holders,
        "risk_score": _f(score),
        "rugged": bool(rep.get("rugged", False)),
        "danger_risks": danger,
    }


if __name__ == "__main__":
    new = discover_new_tokens()
    print(f"/stats/new_tokens: {len(new)} mints")
    if new:
        mint = new[0]
        rep = report(mint)
        feat = safety_features(rep)
        print(f"report for {mint[:8]}...:")
        if feat:
            print(f"  mint active?   {feat['mint_authority_active']}")
            print(f"  freeze active? {feat['freeze_authority_active']}")
            print(f"  lp locked %    {feat['lp_locked_pct']:.1f}")
            print(f"  top10 %        {feat['top10_pct']:.1f}")
            print(f"  insider %      {feat['insider_pct']:.1f}")
            print(f"  holders        {feat['total_holders']}")
            print(f"  risk score     {feat['risk_score']:.0f}")
            print(f"  rugged         {feat['rugged']}")
            print(f"  danger risks   {feat['danger_risks']}")
        else:
            print("  (no report / empty)")
