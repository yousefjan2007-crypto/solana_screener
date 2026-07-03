"""
Screening logic — PURE and deterministic. No network, no wall-clock, no RNG; identical
inputs always produce identical outputs (verify.py asserts this). Two stages:

  • hard_gates()  — reject a token outright if any rug/honeypot/insider gate fails.
  • soft_score()  — rank the survivors 0-100.

Every threshold and weight comes from config.py, so the entire screen re-tunes from one
file with no magic numbers here.
"""
from __future__ import annotations

import config


def hard_gates(market: dict, safety: dict) -> tuple[bool, dict]:
    """Return (passed, results). `results` records every individual check so the alert /
    ledger can show exactly WHY a token passed or was rejected."""
    r: dict = {}
    r["has_safety"] = bool(safety)
    r["mint_revoked"] = not safety.get("mint_authority_active", True)
    r["freeze_revoked"] = not safety.get("freeze_authority_active", True)
    r["lp_locked"] = safety.get("lp_locked_pct", 0.0) >= config.LP_LOCKED_MIN_PCT
    r["not_rugged"] = not safety.get("rugged", False)
    r["liq_ok"] = market.get("liq_usd", 0.0) >= config.LIQ_FLOOR_USD
    r["vol_ok"] = market.get("vol_h24", 0.0) >= config.MIN_VOL_H24_USD
    r["top10_ok"] = safety.get("top10_pct", 100.0) <= config.TOP10_MAX_PCT
    r["insider_ok"] = safety.get("insider_pct", 100.0) <= config.INSIDER_MAX_PCT
    r["dev_ok"] = safety.get("dev_pct", 100.0) <= config.DEV_MAX_PCT
    r["risk_ok"] = safety.get("risk_score", 999.0) <= config.RISK_SCORE_MAX
    danger_hits = [d for d in safety.get("danger_risks", [])
                   if any(b in d.lower() for b in config.DANGER_RISK_BLOCKLIST)]
    r["no_danger_risk"] = len(danger_hits) == 0
    r["danger_hits"] = danger_hits

    passed = (
        r["has_safety"]
        and (r["mint_revoked"] or not config.REQUIRE_MINT_REVOKED)
        and (r["freeze_revoked"] or not config.REQUIRE_FREEZE_REVOKED)
        and r["lp_locked"]
        and (r["not_rugged"] or not config.REJECT_IF_RUGGED)
        and r["liq_ok"] and r["vol_ok"]
        and r["top10_ok"] and r["insider_ok"] and r["dev_ok"] and r["risk_ok"]
        and r["no_danger_risk"]
    )
    return passed, r


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _age_component(age_min: float) -> float:
    """Reward the age sweet-spot: penalize too-new (unreliable data) and old (decaying)."""
    if age_min != age_min:  # NaN
        return 0.3
    if age_min < config.AGE_MIN_MINUTES:
        return _clip01(age_min / config.AGE_MIN_MINUTES) * 0.5
    if age_min <= config.AGE_SWEET_MINUTES:
        return 1.0
    if age_min >= config.AGE_MAX_MINUTES:
        return 0.1
    span = config.AGE_MAX_MINUTES - config.AGE_SWEET_MINUTES
    return _clip01(1.0 - (age_min - config.AGE_SWEET_MINUTES) / span * 0.9)


def soft_score(market: dict, safety: dict) -> tuple[float, dict]:
    """Rank a survivor 0-100 from already-fetched fields (no extra API calls)."""
    mcap = market.get("mcap", 0.0)
    vmc = (market.get("vol_h24", 0.0) / mcap) if mcap > 0 else 0.0
    c_vmc = _clip01(vmc / config.VMC_CAP)

    buys, sells = market.get("buys_h1", 0), market.get("sells_h1", 0)
    tot = buys + sells
    c_bs = _clip01((buys / tot - 0.5) * 2.0) if tot > 0 else 0.0  # 0 at <=50% buys, 1 at 100%

    h = safety.get("total_holders", 0)
    if h >= config.HOLDERS_SAFE:
        c_h = 1.0
    elif h <= config.HOLDERS_DANGER:
        c_h = 0.0
    else:
        c_h = (h - config.HOLDERS_DANGER) / (config.HOLDERS_SAFE - config.HOLDERS_DANGER)

    top10 = safety.get("top10_pct", config.TOP10_MAX_PCT)
    c_conc = _clip01(1.0 - top10 / config.TOP10_MAX_PCT)

    c_age = _age_component(market.get("pair_age_min", float("nan")))

    w = config.SOFT_WEIGHTS
    score = 100.0 * (w["vol_mcap"] * c_vmc + w["buy_sell"] * c_bs + w["holders"] * c_h
                     + w["concentration"] * c_conc + w["age"] * c_age)
    comps = {"vol_mcap": c_vmc, "buy_sell": c_bs, "holders": c_h,
             "concentration": c_conc, "age": c_age}
    return round(score, 1), comps


if __name__ == "__main__":
    # Fixture A — an obvious rug: mint+freeze live, 85% top-10, dust liquidity.
    rug_m = {"liq_usd": 500, "vol_h24": 100, "mcap": 20_000,
             "buys_h1": 0, "sells_h1": 5, "pair_age_min": 3}
    rug_s = {"mint_authority_active": True, "freeze_authority_active": True,
             "lp_locked_pct": 0, "top10_pct": 85, "insider_pct": 60, "dev_pct": 40,
             "total_holders": 30, "risk_score": 95, "rugged": False,
             "danger_risks": ["Mint Authority still enabled"]}
    ok, r = hard_gates(rug_m, rug_s)
    print(f"rug fixture passes hard gates? {ok}   (expect False)")

    # Fixture B — a clean survivor.
    clean_m = {"liq_usd": 45_000, "vol_h24": 120_000, "mcap": 250_000,
               "buys_h1": 320, "sells_h1": 180, "pair_age_min": 180}
    clean_s = {"mint_authority_active": False, "freeze_authority_active": False,
               "lp_locked_pct": 100, "top10_pct": 18, "insider_pct": 5, "dev_pct": 1.2,
               "total_holders": 1400, "risk_score": 25, "rugged": False,
               "danger_risks": []}
    ok2, _ = hard_gates(clean_m, clean_s)
    s, comps = soft_score(clean_m, clean_s)
    print(f"clean fixture passes hard gates? {ok2}   (expect True)   score {s}/100")
    print("  components:", {k: round(v, 2) for k, v in comps.items()})
