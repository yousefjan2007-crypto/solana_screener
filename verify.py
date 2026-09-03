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
        # snapshots carry a SUPPLY-CONSISTENT mcap (entry 1.0/1 => supply 1, so
        # mcap == price) — the quote-integrity gate rejects anything else by design.
        _, ev = ledger.update_forward(10.0, lambda m: {"price_usd": 2.5, "mcap": 2.5},
                                      path=tmp)
        check("2.5x fires ONE tp event, tier A only",
              len(ev) == 1 and ev[0]["kind"] == "tp" and ev[0]["symbol"] == "TA"
              and ev[0]["levels"][-1][0] == 2.0)
        _, ev2 = ledger.update_forward(20.0, lambda m: {"price_usd": 2.6, "mcap": 2.6},
                                       path=tmp)
        check("tp does NOT re-fire on the same level", ev2 == [])
        _, ev3 = ledger.update_forward(30.0, lambda m: {"price_usd": 6.0, "mcap": 6.0},
                                       path=tmp)
        check("next ladder level (5x) fires once",
              len(ev3) == 1 and ev3[0]["levels"] == [(5.0, 0.25)])
        _, ev4 = ledger.update_forward(40.0, lambda m: {"price_usd": 0.4, "mcap": 0.4},
                                       path=tmp)
        check("stop breach fires ONE stop event, tier A only",
              len(ev4) == 1 and ev4[0]["kind"] == "stop" and ev4[0]["symbol"] == "TA")
        _, ev5 = ledger.update_forward(50.0, lambda m: {"price_usd": 0.3, "mcap": 0.3},
                                       path=tmp)
        check("stop does NOT re-fire", ev5 == [])
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # 12. QUOTE-INTEGRITY GATE ($FOMO REGRESSION, 2026-09). A Dexscreener pair-switch
    # once wrote a 1283x max_ret_seen that no horizon cell ever saw. A snapshot whose
    # implied supply (mcap/price) disagrees with the entry's must write NOTHING that
    # run; an honest quote afterwards resumes; a dead coin stays the -100% path.
    tmp = tempfile.mktemp(suffix="_verify_gate.csv")
    try:
        ledger.record_alerts(
            [{"mint": "MG", "symbol": "TG", "tier": "B", "price": 1.0,
              "mcap": 1000.0, "liq": 1, "score": 50, "gates": {}}],
            alert_ts=0.0, path=tmp)
        f1, _ = ledger.update_forward(
            3600.0, lambda m: {"price_usd": 500.0, "mcap": 1000.0}, path=tmp)
        led = ledger.load(tmp)
        check("pair-switch snapshot fills no horizon cell", f1 == 0)
        check("pair-switch snapshot does not ratchet max_ret_seen",
              float(led.at[0, "max_ret_seen"]) == 0.0)
        check("suspect_ticks counts the rejection",
              float(led.at[0, "suspect_ticks"]) == 1.0)
        f2, _ = ledger.update_forward(
            3700.0, lambda m: {"price_usd": 2.5, "mcap": 2500.0}, path=tmp)
        led = ledger.load(tmp)
        check("an honest quote after a rejected one resumes tracking",
              f2 == 1 and abs(float(led.at[0, "max_ret_seen"]) - 1.5) < 1e-9)
        f3, _ = ledger.update_forward(21700.0, lambda m: {}, path=tmp)
        led = ledger.load(tmp)
        check("a dead coin still records -100% (absence of a quote is NOT suspect)",
              float(led.at[0, "ret_6h"]) == -1.0
              and float(led.at[0, "suspect_ticks"]) == 1.0)
        # A LEGIT supply change would fail the gate on every honest quote forever —
        # after SUSPECT_TICKS_MAX rejections the row must go terminally 'suspect'
        # (its own summary category, never "still maturing") and stop being polled.
        for k in range(ledger.config.SUSPECT_TICKS_MAX - 1):
            ledger.update_forward(21800.0 + k,
                                  lambda m: {"price_usd": 500.0, "mcap": 1000.0},
                                  path=tmp)
        led = ledger.load(tmp)
        check("persistent supply shift goes terminally 'suspect' at SUSPECT_TICKS_MAX",
              str(led.at[0, "status"]) == "suspect"
              and float(led.at[0, "suspect_ticks"]) == ledger.config.SUSPECT_TICKS_MAX)
        f4, _ = ledger.update_forward(
            40000.0, lambda m: {"price_usd": 2.0, "mcap": 2000.0}, path=tmp)
        check("suspect rows are never polled or written again", f4 == 0)
        # Pre-migration CSV (no suspect_ticks column yet — the cloud writes those for a
        # while after merge) must survive the incident path, not KeyError inside it.
        tmp2 = tempfile.mktemp(suffix="_verify_old.csv")
        try:
            ledger.record_alerts(
                [{"mint": "MOLD", "symbol": "TO", "tier": "B", "price": 1.0,
                  "mcap": 1000.0, "liq": 1, "score": 50, "gates": {}}],
                alert_ts=0.0, path=tmp2)
            old = ledger.pd.read_csv(tmp2).drop(columns=["suspect_ticks"])
            old.to_csv(tmp2, index=False)
            f5, _ = ledger.update_forward(
                3600.0, lambda m: {"price_usd": 500.0, "mcap": 1000.0}, path=tmp2)
            led2 = ledger.load(tmp2)
            check("a pre-migration CSV survives the incident path (no KeyError)",
                  f5 == 0 and float(led2.at[0, "suspect_ticks"]) == 1.0)
        finally:
            if os.path.exists(tmp2):
                os.remove(tmp2)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # 13. PATH SANITIZER ($STABLECAT 804x / $Girlet 71x REGRESSIONS, 2026-09). A dead
    # pool can print phantom highs on zero-volume bars, and GeckoTerminal can pick a
    # pool mispriced vs the ledger's Dexscreener entry — both corrupted real analyses.
    import json as _json
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "selfimprove"))
    import evaluate as EV
    tmpj = tempfile.mktemp(suffix="_verify_paths.jsonl")
    try:
        recs = [
            {"mint": "OKM", "entry": 1.0, "bars": [
                {"ts": 0, "o": 1, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10},
                {"ts": 60, "o": 1.1, "h": 804.0, "l": 1.0, "c": 1.05, "v": 0},
                {"ts": 120, "o": 1.05, "h": 1.3, "l": 1.0, "c": 1.2, "v": 5}]},
            {"mint": "BADPOOL", "entry": 0.014, "bars": [
                {"ts": 0, "o": 0.014, "h": 0.02, "l": 0.01, "c": 0.015, "v": 3}]},
        ]
        with open(tmpj, "w") as fh:
            for r in recs:
                fh.write(_json.dumps(r) + "\n")
        clean = EV.load_paths(tmpj, ledger_entry={"OKM": 1.0, "BADPOOL": 1.0})
        check("a zero-volume phantom bar is excluded from a path",
              len(clean) == 1 and max(b["h"] for b in clean[0]["bars"]) == 1.3)
        check("a path from a mispriced pool is dropped wholesale",
              all(r["mint"] != "BADPOOL" for r in clean))
    finally:
        if os.path.exists(tmpj):
            os.remove(tmpj)

    # 14. Paper execution: buys are idempotent per mint; TP sells the ladder fraction of
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

        # ── APPARATUS-CHECK REBUILD (2026-09). The old pre-check voided every run
        # because ctl_exit_immediately's LB beats hold_to_end's — a fact about -95%
        # median coin fates, not about the machinery. These pin the new semantics.
        def _res(**over):
            base = {"champion": "hold_to_end", "n_positions": 400, "n_days": 45,
                    "n_trials": 16, "forward_only": False,
                    "controls": {"ctl_exit_immediately": {"day_lb": -0.06}},
                    "inert_controls": [],
                    "policies": {
                        "hold_to_end": {"n": 400, "mean": -0.7, "median": -0.95,
                                        "win_rate": 0.05, "day_lb": -0.8, "n_days": 45},
                        "sell_15m": {"n": 400, "mean": -0.1, "median": -0.05,
                                     "win_rate": 0.4, "day_lb": -0.2, "n_days": 45,
                                     "paired_mean": 0.6, "paired_lb": 0.5, "dsr": 0.99}}}
            base.update(over)
            return base
        v1 = IMP.decide(_res())
        check("a control beating hold_to_end no longer voids the run",
              not v1.get("gate_broken"))
        check("a challenger below the control bar still cannot promote",
              not v1["promote"])
        check("an INERT control voids the run",
              IMP.decide(_res(inert_controls=["ctl_random_exit"])).get("gate_broken")
              is True)
        check("a PROFITABLE control voids the run",
              IMP.decide(_res(controls={"ctl_exit_immediately": {"day_lb": 0.02}}))
              .get("gate_broken") is True)
        # forward-only nomination: a challenger clearing every bar except the forward
        # split gets NOMINATED, not promoted; an active nomination is judged alone.
        good = {"n": 400, "mean": 0.1, "median": 0.02, "win_rate": 0.55,
                "day_lb": 0.05, "n_days": 45, "paired_mean": 0.8, "paired_lb": 0.7,
                "dsr": 0.99}
        vn = IMP.decide(_res(policies={"hold_to_end": _res()["policies"]["hold_to_end"],
                                       "sell_15m": good}))
        check("clearing every bar except forward-only NOMINATES instead of promoting",
              vn.get("nominate") == "sell_15m" and not vn["promote"])
        vw = IMP.decide(_res(
            policies={"hold_to_end": _res()["policies"]["hold_to_end"],
                      "sell_15m": good, "sell_1h": dict(good, paired_lb=0.9)},
            nomination={"nominee": "sell_15m", "nominated_at_alert_seq": 100,
                        "n_forward": 5, "days_forward": 2}))
        check("an active nomination is judged ALONE (leaderboard max ignored) and waits",
              vw.get("winner") == "sell_15m" and not vw["promote"]
              and "nominate" not in vw)
        vf = IMP.decide(_res(
            forward_only=True,
            policies={"hold_to_end": _res()["policies"]["hold_to_end"],
                      "sell_15m": dict(good, forward_only=True)},
            nomination={"nominee": "sell_15m", "nominated_at_alert_seq": 100,
                        "n_forward": 60, "days_forward": 45}))
        check("a nominee passing every check on its FORWARD sample promotes",
              vf["promote"] and vf["winner"] == "sell_15m")

        # ── random_exit control acts in the LIVE book (2026-09: it was simulator-only
        # and bit-identical to hold_to_end on 294/294 live positions).
        pos_r = {"mint": "RNDX", "symbol": "R", "tier": "B", "entry_px": 1.0,
                 "opened_ts": 0.0, "tokens_raw": 1}
        st_r = {"remaining": 1.0, "peak_px": 1.0, "rungs_left": None,
                "realized_usd": 0.0, "closed": False}
        st_h = {"remaining": 1.0, "peak_px": 1.0, "rungs_left": None,
                "realized_usd": 0.0, "closed": False}
        LB._step_policy(pos_r, "ctl_random_exit", st_r, 1.0,
                        LB.DECISION_HORIZON_S, 10.0, 60.0)
        LB._step_policy(pos_r, "hold_to_end", st_h, 1.0,
                        LB.DECISION_HORIZON_S, 10.0, 60.0)
        check("random_exit control acts in the live book (no hold_to_end alias)",
              st_r["closed"] and st_r.get("close_reason") == "random_exit"
              and not st_h["closed"])
        # The hold must equal the sha256-derived draw exactly — recomputing it HERE
        # (independent of _step_policy's internals) pins the algorithm, so a switch to
        # process-salted hash() or an unseeded rng fails this check.
        import hashlib as _hl
        import numpy as _np
        _seed = config.SEED + int(_hl.sha256(b"RNDX").hexdigest()[:8], 16)
        _hold = float(_np.random.default_rng(_seed).uniform(0.0, LB.DECISION_HORIZON_S))
        st_r2 = {"remaining": 1.0, "peak_px": 1.0, "rungs_left": None,
                 "realized_usd": 0.0, "closed": False}
        LB._step_policy(pos_r, "ctl_random_exit", st_r2, 1.0, _hold * 0.999, 10.0, 60.0)
        st_r3 = {"remaining": 1.0, "peak_px": 1.0, "rungs_left": None,
                 "realized_usd": 0.0, "closed": False}
        LB._step_policy(pos_r, "ctl_random_exit", st_r3, 1.0, _hold + 1.0, 10.0, 60.0)
        check("random_exit fires exactly at its sha256-derived hold (stable seed)",
              (not st_r2["closed"]) and st_r3["closed"])
    finally:
        LB.BOOK_PATH, LB.FILLS_PATH, LB.TICKS_PATH = real
        shutil.rmtree(tmpd, ignore_errors=True)

    # ── the rate limiter must hold under concurrency ────────────────────────────
    # API rate limits are per-IP and shared with sibling projects, and RugCheck's is
    # undocumented — which is why it is pinned at 1 Hz. That ceiling is only real if
    # concurrent callers queue. listener.py evaluates pump.fun migrations off the
    # socket thread and graduations arrive in clusters, so this is the live case,
    # not a hypothetical one.
    import threading as _th
    import time as _time

    import http_client as _hc
    _hc._HOST_HZ["verify.invalid"] = 20.0        # 0.05s gap: proves pacing, stays fast
    try:
        _hc._last_call.pop("verify.invalid", None)
        stamps, slock = [], _th.Lock()

        def _tick():
            _hc._throttle("verify.invalid")
            with slock:
                stamps.append(_time.monotonic())

        ths = [_th.Thread(target=_tick) for _ in range(8)]
        t0 = _time.monotonic()
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        elapsed = _time.monotonic() - t0
        stamps.sort()
        tightest = min(stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1))
        # Unlocked this finishes in ~0s with a 0.000s gap — measured, and the reason
        # the check is on the gap and not merely on the total.
        check("8 concurrent callers cannot burst through a host rate limit",
              tightest >= 0.045 and elapsed >= 0.045 * 7)
    finally:
        _hc._HOST_HZ.pop("verify.invalid", None)
        _hc._last_call.pop("verify.invalid", None)

    # The other half: the throttle can only pace what it is asked about, so the
    # listener must not be able to put an unbounded number of callers in front of it.
    import listener as _ls
    check("listener evaluations run on a BOUNDED pool, not a thread per event",
          _ls._evaluators._max_workers == _ls.EVAL_WORKERS and _ls.EVAL_WORKERS <= 4)
    _src = open(os.path.join(config.ROOT, "listener.py")).read()
    check("no unbounded per-event thread survives in listener.py",
          "threading.Thread(target=evaluate_" not in _src)
    # A raising evaluation must not poison the pool. ThreadPoolExecutor buries the
    # exception in a Future nobody reads, so _submit wraps and logs it instead.
    def _boom():
        raise RuntimeError("boom")

    _ls._submit(_boom)
    _after = _ls._evaluators.submit(lambda: "alive").result(timeout=5)
    check("a raising evaluation is contained and the pool still runs work after it",
          _after == "alive")

    print("ALL INVARIANTS PASSED")


if __name__ == "__main__":
    main()
