"""
solana_screener — one-shot runner. discover → enrich → screen → rank → alert → ledger →
state → exit. No in-process loop; a scheduler (launchd) re-runs it, and anti-spam is done
by comparing against the last logged state — exactly like vrp_backtest/monitor.py and
signal_lab/deepvalue/dv_monitor.py.

Flags (mirroring dv_monitor.py):
  (none)      dry run: discover→enrich→screen and PRINT ranked survivors; send nothing,
              write nothing.
  --commit    write ledger + state (record alerts, fill matured forward returns); no send.
  --send      write AND push alerts to Telegram / ntfy / macOS.

Live "now" (time.time()) is captured ONCE here and threaded through enrichment and the
ledger — it drives data freshness and horizon bookkeeping only, never the screen's
scoring, so the screen stays deterministic and reproducible.
"""
from __future__ import annotations

import json
import os
import sys
import time

import config
import alerts
import ledger
from screen import hard_gates, soft_score
from sources import dexscreener as dex
from sources import rugcheck as rug


def _load_state() -> dict:
    if os.path.exists(config.STATE_PATH):
        try:
            return json.load(open(config.STATE_PATH))
        except Exception:
            return {}
    return {}


def run(dry_run: bool = True, send: bool = False) -> list[dict]:
    now_s = time.time()

    # 1. discover (RugCheck new_tokens is primary; Dexscreener feeds are secondary)
    mints = set(rug.discover_new_tokens())
    mints |= dex.discover_solana_profiles()
    mints |= dex.discover_solana_boosts()
    mints = list(mints)[:config.MAX_DISCOVER]
    print(f"discovered {len(mints)} candidate mint(s)")

    # 2. enrich with Dexscreener market data
    market = dex.enrich(mints, now_s=now_s)
    print(f"enriched {len(market)} with market data")

    # 3. screen: hard gates (RugCheck safety) → soft score
    survivors: list[dict] = []
    for mint, m in market.items():
        safety = rug.safety_features(rug.report(mint))
        passed, gates = hard_gates(m, safety)
        if not passed:
            continue
        score, _ = soft_score(m, safety)
        row = dict(m)
        row.update({
            "score": score,
            "total_holders": safety.get("total_holders"),
            "top10_pct": round(safety.get("top10_pct", 0.0), 1),
            "rugcheck_score": safety.get("risk_score"),
            "gates": gates,
        })
        survivors.append(row)

    survivors.sort(key=lambda r: -r["score"])
    print(f"{len(survivors)} passed the hard gates")
    for s in survivors[:config.ALERT_TOP_N]:
        print(f"  {s['symbol']:12.12s} score {s['score']:5.1f}  liq ${s['liq_usd']:>10,.0f}  "
              f"mcap ${s['mcap']:>12,.0f}  top10 {s['top10_pct']:>4}%  "
              f"age {s['pair_age_min']:>5.0f}m")

    # 4. anti-spam → alert only tokens not alerted within the cooldown window
    state = _load_state()
    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    fresh = [s for s in survivors[:config.ALERT_TOP_N]
             if now_s - float(state.get(s["mint"], 0)) >= cooldown]
    if fresh:
        title, body = alerts.format_alert(fresh)
        alerts.send_all(title, body, dry_run=not send)
    else:
        print("(no fresh survivors to alert — all within cooldown or none passed)")

    # 5. persist: record new alerts, fill matured forward returns, update state
    if not dry_run:
        ledger.record_alerts(
            [{"mint": s["mint"], "symbol": s["symbol"], "price": s["price_usd"],
              "mcap": s["mcap"], "liq": s["liq_usd"], "score": s["score"],
              "gates": s["gates"], "rugcheck_score": s.get("rugcheck_score")}
             for s in fresh],
            alert_ts=now_s,
        )
        filled = ledger.update_forward(
            now_s,
            snapshot_fn=lambda mint: dex.forward_snapshot(mint, now_s=now_s),
            report_fn=rug.report,
        )
        for s in fresh:
            state[s["mint"]] = now_s
        json.dump(state, open(config.STATE_PATH, "w"), indent=2)
        print(f"committed: {len(fresh)} new ledger row(s), {filled} forward cell(s) filled")

    return survivors


if __name__ == "__main__":
    _send = "--send" in sys.argv
    _commit = "--commit" in sys.argv or _send
    run(dry_run=not _commit, send=_send)
