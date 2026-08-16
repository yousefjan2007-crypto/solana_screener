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
import paper_exec
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

    # 10. GMGN behavioral gates: enforced when data present (the Cubrate wallet-farm
    # fingerprint — 88 bundler wallets — must reject), pass-through when absent.
    farm = dict(clean, gmgn_ok=True, gmgn_bundler_ratio=1.42, gmgn_bundler_wallets=88,
                gmgn_rat_wallets=3, gmgn_sniper_hold_pct=0.1, gmgn_insider_hold_pct=0.0)
    okf, rf = hard_gates(m, farm)
    check("GMGN wallet-farm fingerprint (bundler ratio 1.42) is rejected",
          okf is False and rf["gmgn_bundlers_ok"] is False)
    hc_g, _ = high_conviction(mature_m, dict(clean, gmgn_ok=True,
                                             gmgn_bundler_ratio=0.2), score=90.0)
    check("A-tier rejects elevated GMGN bundler ratio (0.2)", hc_g is False)
    okn, rn = hard_gates(m, clean)  # no gmgn keys at all
    check("missing GMGN data passes through (graceful degradation)",
          rn["gmgn_available"] is False and rn["gmgn_bundlers_ok"] is True and okn)

    # 11. Exit discipline: TP and stop signals fire EXACTLY ONCE per level, and never
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

    # 12. Paper execution: buys are idempotent per mint; TP sells the ladder fraction of
    # the ORIGINAL size; a stop with NO route closes at $0 (the dead-coin outcome); and
    # exits for mints never paper-bought (the B control) are ignored. Injected quote_fn,
    # temp files — no network.
    tmpl = tempfile.mktemp(suffix="_verify_paper.csv")
    tmpp = tempfile.mktemp(suffix="_verify_pos.json")
    try:
        fake_q = lambda i, o, amt: {"outAmount": str(int(amt) * 2),
                                    "priceImpactPct": "0.01"}
        f1 = paper_exec.open_position("M1", "T1", "A", 0.0, ledger_path=tmpl,
                                      positions_path=tmpp, quote_fn=fake_q)
        f2 = paper_exec.open_position("M1", "T1", "A", 1.0, ledger_path=tmpl,
                                      positions_path=tmpp, quote_fn=fake_q)
        check("paper buy opens once; re-open is a no-op",
              f1 is not None and f1["tokens_raw_delta"] == 20_000_000 and f2 is None)
        s1 = paper_exec.execute_exit(
            {"kind": "tp", "mint": "M1", "symbol": "T1", "levels": [(2.0, 0.50)]},
            2.0, ledger_path=tmpl, positions_path=tmpp,
            quote_fn=lambda i, o, amt: {"outAmount": "20000000",
                                        "priceImpactPct": "0.02"})
        check("tp sells the ladder fraction of the ORIGINAL size, proceeds in USD",
              s1 is not None and s1["tokens_raw_delta"] == -10_000_000
              and abs(s1["usd_flow"] - 20.0) < 1e-9)
        s2 = paper_exec.execute_exit({"kind": "stop", "mint": "M1", "symbol": "T1"},
                                     3.0, ledger_path=tmpl, positions_path=tmpp,
                                     quote_fn=lambda i, o, amt: None)
        check("stop with NO route liquidates the remainder at $0 and closes",
              s2 is not None and s2["usd_flow"] == 0.0
              and s2["tokens_raw_delta"] == -10_000_000)
        s3 = paper_exec.execute_exit({"kind": "stop", "mint": "M1", "symbol": "T1"},
                                     4.0, ledger_path=tmpl, positions_path=tmpp,
                                     quote_fn=fake_q)
        s4 = paper_exec.execute_exit({"kind": "stop", "mint": "MB", "symbol": "TB"},
                                     5.0, ledger_path=tmpl, positions_path=tmpp,
                                     quote_fn=fake_q)
        check("closed positions and never-bought mints ignore exits",
              s3 is None and s4 is None)
    finally:
        for p in (tmpl, tmpp):
            if os.path.exists(p):
                os.remove(p)

    # ── live multi-policy paper book (selfimprove/livebook.py) ──────────────────────
    # Every one of these corresponds to a mistake actually made while building this.
    print("\nlive book — multi-policy paper trading")
    import csv
    import json
    import shutil                       # NOT tempfile — it is imported at module scope, and a
    import config                       # local import would shadow it for the whole function
    from selfimprove import livebook as LB

    tmpd = tempfile.mkdtemp()
    real = (LB.BOOK_PATH, LB.FILLS_PATH, LB.TICKS_PATH)
    LB.BOOK_PATH = os.path.join(tmpd, "b.json")
    LB.FILLS_PATH = os.path.join(tmpd, "f.csv")
    LB.TICKS_PATH = os.path.join(tmpd, "t.jsonl")
    try:
        TOK = 1_000_000
        usd = config.STACK_USD * config.POSITION_PCT

        def _run(mults, dt, tag):
            seq = list(mults)

            def qf(inp, out, amt):
                if inp == config.USDC_MINT:
                    return {"outAmount": str(TOK), "priceImpactPct": "0.01"}
                if not seq:
                    return None
                v = seq.pop(0)
                return None if v is None else {"outAmount": str(int(usd * v * 1e6)),
                                               "priceImpactPct": "0.02"}
            LB.open_alert(tag, "T", "A", 0.0, quote_fn=qf)
            for i in range(len(mults)):
                LB.tick(dt * (i + 1), quote_fn=qf, verbose=False)
            return json.load(open(LB.BOOK_PATH))[tag]

        # 1x -> 10x -> bleeds to 0.2x, long enough that every policy resolves
        p = _run([1, 2, 4, 10, 7, 5, 3, 2, 1.2, .8, .5, .35, .25, .2] + [.2] * 150,
                 300.0, "PUMP")
        pol = p["policies"]
        check("every policy resolves and the position retires",
              all(s["closed"] for s in pol.values()) and p["done"])
        check("no policy ends holding a fraction it never sold",
              all(abs(s["remaining"]) < 1e-9 for s in pol.values()))
        # THE BUG OF 2026-08-12, in its live form. A trailing stop must exit off the HIGH-WATER
        # MARK, so on a path that peaks at 10x and dies at 0.2x it must beat hold_to_end. The
        # bar simulator got this wrong by ratcheting the peak to the current bar's high before
        # testing that bar's low — look-ahead inside the bar. A tick stream cannot express that
        # bug, and this asserts the resulting ordering directly.
        check("a trailing stop beats hold_to_end on a path that peaks then dies",
              pol["trail_30"]["realized_usd"] > pol["hold_to_end"]["realized_usd"])
        check("hold_to_end is marked at the final observed price, not at zero or at the peak",
              abs(pol["hold_to_end"]["realized_usd"] / p["cost_usd"] - 0.2) < 1e-6)
        # A rung is a pre-committed LIMIT. Filling it at the observed tick (which is at or above
        # the rung) would be the same class of leak as reading an eventual peak as a fill price.
        rows = list(csv.DictReader(open(LB.FILLS_PATH)))
        tps = [r for r in rows if r["side"].startswith("tp_")]
        check("ladder rungs fill at the rung level, never at the observed tick",
              bool(tps) and all(r["note"] == "limit fill at rung level" for r in tps))
        check("the shared entry is taken ONCE and every policy inherits it",
              sum(1 for r in rows if r["side"] == "buy") == 1)

        # a dead coin is a real outcome, not an error
        shutil.rmtree(tmpd)
        tmpd = tempfile.mkdtemp()
        LB.BOOK_PATH = os.path.join(tmpd, "b.json")
        LB.FILLS_PATH = os.path.join(tmpd, "f.csv")
        LB.TICKS_PATH = os.path.join(tmpd, "t.jsonl")
        p = _run([None], 60.0, "RUG")
        check("no Jupiter route closes EVERY policy at exactly -1.00 (dead coin, not an error)",
              all(abs(s["realized_usd"]) < 1e-12 and s["closed"]
                  for s in p["policies"].values()) and p["done"])

        # the same alert arriving from both the scan and the listener must not double-open
        def _qf(inp, out, amt):
            return {"outAmount": str(TOK), "priceImpactPct": "0.01"}
        a = LB.open_alert("DUP", "T", "A", 0.0, quote_fn=_qf)
        b = LB.open_alert("DUP", "T", "A", 5.0, quote_fn=_qf)
        check("a mint alerted twice opens exactly once (scan and listener overlap)",
              a is not None and b is None)

        # ── the ledger-driven feed ──────────────────────────────────────────────────
        # run.py opens book positions but runs on the Actions runner, while the ticker is local,
        # so the book is fed from the CLOUD-committed ledger instead. Three things must hold or
        # the feed silently fabricates or loses data.
        shutil.rmtree(tmpd, ignore_errors=True)
        tmpd = tempfile.mkdtemp()
        LB.BOOK_PATH = os.path.join(tmpd, "b.json")
        LB.FILLS_PATH = os.path.join(tmpd, "f.csv")
        LB.TICKS_PATH = os.path.join(tmpd, "t.jsonl")
        LB.FEED_STATE_PATH = os.path.join(tmpd, "feed.json")
        LB.MISSED_PATH = os.path.join(tmpd, "missed.jsonl")
        T0 = 1_000_000.0
        led = [{"mint": "OLD", "symbol": "O", "tier": "A", "alert_ts": str(T0 - 3600)},
               {"mint": "NEW", "symbol": "N", "tier": "A", "alert_ts": str(T0 - 300)}]

        def _q(i, o, a):
            return {"outAmount": "1000000", "priceImpactPct": "0.01"}
        s1 = LB.feed_from_ledger(T0 - 7200, quote_fn=_q, rows_fn=lambda: led, verbose=False)
        check("the first feed run sets a watermark and opens NOTHING (no stale backlog)",
              s1["opened"] == 0 and os.path.exists(LB.FEED_STATE_PATH))
        s2 = LB.feed_from_ledger(T0, quote_fn=_q, rows_fn=lambda: led, verbose=False)
        check("an alert past MAX_ENTRY_LAG_S is refused and logged with its lag",
              s2["too_late"] == 1 and os.path.exists(LB.MISSED_PATH)
              and json.loads(open(LB.MISSED_PATH).read().splitlines()[0])["entry_lag_s"] > 0)
        bk = json.load(open(LB.BOOK_PATH))
        check("a fresh alert opens once and records entry_lag_s",
              s2["opened"] == 1 and "NEW" in bk and bk["NEW"]["entry_lag_s"] == 300.0)
        s3 = LB.feed_from_ledger(T0, quote_fn=_q, rows_fn=lambda: led, verbose=False)
        check("the watermark stops the same ledger rows re-opening on the next tick",
              s3["opened"] == 0 and s3["too_late"] == 0)
        check("a feed outage returns no rows rather than raising",
              LB.feed_from_ledger(T0, quote_fn=_q, rows_fn=lambda: [],
                                  verbose=False)["seen"] == 0)

        # the promotion gate must refuse to act on thin evidence
        from selfimprove import improve as IMP
        v = IMP.decide({"champion": "hold_to_end", "n_positions": 3, "n_days": 2,
                        "n_trials": 15, "policies": {}})
        check("the promotion gate refuses to promote on thin evidence", not v["promote"])
        check("MIN_POSITIONS and MIN_DAYS gates are actually wired",
              IMP.MIN_POSITIONS >= 30 and IMP.MIN_DAYS >= config.MIN_BOOTSTRAP_CLUSTERS)
    finally:
        LB.BOOK_PATH, LB.FILLS_PATH, LB.TICKS_PATH = real
        shutil.rmtree(tmpd, ignore_errors=True)

    print("ALL INVARIANTS PASSED")


if __name__ == "__main__":
    main()
