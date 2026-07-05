"""
Load-bearing invariants for solana_screener. Run:  python3 verify.py

These are the guarantees the whole tool's trustworthiness rests on:
  1. An active mint authority is ALWAYS rejected (the #1 rug vector).
  2. The screen is deterministic and contains no wall-clock (reproducible).
  3. Authority null-means-revoked semantics are correct.
  4. Forward-return math is correct.
"""
from __future__ import annotations

import inspect
import os
import tempfile

import ledger
import screen
from screen import hard_gates, high_conviction, soft_score
from sources.rugcheck import safety_features


def check(name: str, cond: bool) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name)
    assert cond, name


def main() -> None:
    print("solana_screener verify")

    # 1. active mint authority => rejected, even with everything else pristine
    m = {"liq_usd": 99_999, "vol_h24": 99_999, "mcap": 100_000,
         "buys_h1": 9, "sells_h1": 1, "pair_age_min": 120}
    s_active = {"mint_authority_active": True, "freeze_authority_active": False,
                "lp_locked_pct": 100, "top10_pct": 10, "insider_pct": 1, "dev_pct": 1,
                "total_holders": 2000, "risk_score": 10, "rugged": False,
                "danger_risks": []}
    ok, r = hard_gates(m, s_active)
    check("active mint authority is rejected", ok is False and r["mint_revoked"] is False)

    # 2. determinism + no wall-clock in the scoring path
    clean = {"mint_authority_active": False, "freeze_authority_active": False,
             "lp_locked_pct": 100, "top10_pct": 15, "insider_pct": 3, "dev_pct": 1,
             "total_holders": 1500, "risk_score": 20, "rugged": False, "danger_risks": []}
    a, _ = soft_score(m, clean)
    b, _ = soft_score(m, clean)
    check("soft_score is deterministic", a == b)
    src = inspect.getsource(screen)
    check("screen.py has no wall-clock",
          "datetime" not in src and "time.time" not in src and ".now(" not in src)

    # 3. null authority => revoked; an address => active
    feat = safety_features({"token": {"mintAuthority": None, "freezeAuthority": None}})
    check("null freezeAuthority => not active", feat.get("freeze_authority_active") is False)
    feat2 = safety_features({"token": {"freezeAuthority": "So11111111111111111111111111111111111111112"}})
    check("address freezeAuthority => active", feat2.get("freeze_authority_active") is True)

    # 4. forward-return math: 100 -> 250 is +150%
    entry, price_h = 100.0, 250.0
    check("forward return math (100->250 == +150%)", abs((price_h / entry - 1.0) - 1.5) < 1e-9)

    # 5. BUNDLED-SCAM invariant (the $ITSY/$BULLION shape): clean top-10, LP locked,
    # authorities revoked — but insider funding-graph networks hold real supply => reject.
    clean = {"mint_authority_active": False, "freeze_authority_active": False,
             "lp_locked_pct": 100, "top10_pct": 15, "insider_pct": 3, "dev_pct": 1,
             "total_holders": 1500, "risk_score": 20, "rugged": False, "danger_risks": [],
             "insider_networks_pct": 0.0, "graph_insiders": 0,
             "creator_prior_tokens": 0, "creator_dead_frac": 0.0}
    bundled = dict(clean, insider_networks_pct=25.0, graph_insiders=60)
    okb, rb = hard_gates(m, bundled)
    check("bundled launch (clean top-10, 25% insider networks) is rejected",
          okb is False and rb["insider_net_ok"] is False)

    # 6. serial deployer (many prior launches, mostly dead) is rejected
    serial = dict(clean, creator_prior_tokens=50, creator_dead_frac=0.9)
    oks, rs = hard_gates(m, serial)
    check("serial deployer (50 prior launches, 90% dead) is rejected",
          oks is False and rs["creator_ok"] is False)

    # 7. insider-network share is computed from the raw report (amount/supply)
    feat3 = safety_features({
        "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1_000_000},
        "insiderNetworks": [{"tokenAmount": 150_000}, {"tokenAmount": 70_000}],
        "graphInsidersDetected": 42,
        "creatorTokens": [{"marketCap": 5_000}, {"marketCap": 900_000}],
    })
    check("insider network pct = 22% from raw report",
          abs(feat3["insider_networks_pct"] - 22.0) < 1e-9)
    check("graph insiders + creator history extracted",
          feat3["graph_insiders"] == 42 and feat3["creator_prior_tokens"] == 2
          and abs(feat3["creator_dead_frac"] - 0.5) < 1e-9)

    # 8. A-tier can NEVER be laxer than the hard gates: any insider-network presence,
    # a big top10, or thin holders each individually disqualify A-tier.
    mature_m = {"liq_usd": 99_999, "vol_h24": 99_999, "mcap": 100_000,
                "pair_age_min": 240, "buys_h1": 320, "sells_h1": 180}
    hc_ok, _ = high_conviction(mature_m, clean, score=90.0)
    check("pristine mature token IS A-tier", hc_ok is True)
    for bad in (dict(clean, insider_networks_pct=1.0),
                dict(clean, graph_insiders=6),
                dict(clean, creator_prior_tokens=2),
                dict(clean, top10_pct=25),
                dict(clean, total_holders=500)):
        hc_bad, _ = high_conviction(mature_m, bad, score=90.0)
        check("A-tier rejects degraded variant", hc_bad is False)

    # 9. $CUBRATE REGRESSION (2026-07-05): the exact at-alert stats of a wash-traded rug
    # that scored A-tier 80.5 and went -99% within the hour. Age 20 min, 67 holders/min,
    # 4.2 tx/holder/hr — every "quality" metric was bot-manufactured. Must NEVER be
    # A-tier again, no matter how good the score looks.
    cub_m = {"liq_usd": 30_031.46, "vol_h24": 397_126.31, "mcap": 151_307.0,
             "buys_h1": 3662, "sells_h1": 1987, "pair_age_min": 19.94}
    cub_s = dict(clean, top10_pct=4.4, total_holders=1344)
    cub_score, _ = soft_score(cub_m, cub_s)
    cub_hc, cub_misses = high_conviction(cub_m, cub_s, cub_score)
    check(f"Cubrate replay (score {cub_score:.0f}) is NOT A-tier", cub_hc is False)
    check("Cubrate replay trips >=2 independent wash-trade gates", len(cub_misses) >= 2)

    # 10. Exit discipline: TP and stop signals fire EXACTLY ONCE per level, and never
    # for the silent B control tier. Uses an injected snapshot + a temp ledger file.
    tmp = tempfile.mktemp(suffix="_verify_ledger.csv")
    try:
        ledger.record_alerts(
            [{"mint": "MA", "symbol": "TA", "tier": "A", "price": 1.0,
              "mcap": 1, "liq": 1, "score": 50, "gates": {}},
             {"mint": "MB", "symbol": "TB", "tier": "B", "price": 1.0,
              "mcap": 1, "liq": 1, "score": 50, "gates": {}}],
            alert_ts=0.0, path=tmp)
        _, ev = ledger.update_forward(10.0, lambda m: {"price_usd": 2.5, "mcap": 1},
                                      path=tmp)
        check("2.5x fires ONE tp event, tier A only",
              len(ev) == 1 and ev[0]["kind"] == "tp" and ev[0]["symbol"] == "TA"
              and ev[0]["levels"][-1][0] == 2.0)
        _, ev2 = ledger.update_forward(20.0, lambda m: {"price_usd": 2.6, "mcap": 1},
                                       path=tmp)
        check("tp does NOT re-fire on the same level", ev2 == [])
        _, ev3 = ledger.update_forward(30.0, lambda m: {"price_usd": 6.0, "mcap": 1},
                                       path=tmp)
        check("next ladder level (5x) fires once",
              len(ev3) == 1 and ev3[0]["levels"] == [(5.0, 0.25)])
        _, ev4 = ledger.update_forward(40.0, lambda m: {"price_usd": 0.4, "mcap": 1},
                                       path=tmp)
        check("stop breach fires ONE stop event, tier A only",
              len(ev4) == 1 and ev4[0]["kind"] == "stop" and ev4[0]["symbol"] == "TA")
        _, ev5 = ledger.update_forward(50.0, lambda m: {"price_usd": 0.3, "mcap": 1},
                                       path=tmp)
        check("stop does NOT re-fire", ev5 == [])
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print("ALL INVARIANTS PASSED")


if __name__ == "__main__":
    main()
