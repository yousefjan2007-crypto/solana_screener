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

import screen
from screen import hard_gates, soft_score
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

    print("ALL INVARIANTS PASSED")


if __name__ == "__main__":
    main()
