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
from screen import hard_gates, high_conviction, soft_score
from sources import dexscreener as dex
from sources import gmgn
from sources import rugcheck as rug


def _load_state() -> dict:
    if os.path.exists(config.STATE_PATH):
        try:
            return json.load(open(config.STATE_PATH))
        except Exception:
            return {}
    return {}


def screen_token(mint: str, m: dict) -> dict | None:
    """Safety-fetch + hard gates + soft score + tier for ONE enriched token. Returns the
    survivor row (market fields + score/tier/hc_misses/safety extracts) or None if it
    fails the hard gates. Shared by the batch scan below and the live listener."""
    safety = rug.safety_features(rug.report(mint))
    passed, gates = hard_gates(m, safety)
    if not passed:
        return None
    # GMGN second opinion — spent only on tokens that already passed the free gates
    # (1 req/s budget). Behavioral wallet tags catch the wallet-farm shape RugCheck's
    # funding-graph clustering is blind to (the Cubrate failure mode).
    safety.update(gmgn.token_features(mint))
    passed, gates = hard_gates(m, safety)
    if not passed:
        return None
    score, _ = soft_score(m, safety)
    is_a, hc_misses = high_conviction(m, safety, score)
    row = dict(m)
    row.update({
        "score": score,
        "tier": "A" if is_a else "B",
        "hc_misses": hc_misses,
        "total_holders": safety.get("total_holders"),
        "top10_pct": round(safety.get("top10_pct", 0.0), 1),
        "insider_networks_pct": round(safety.get("insider_networks_pct", 0.0), 1),
        "graph_insiders": safety.get("graph_insiders", 0),
        "creator_prior_tokens": safety.get("creator_prior_tokens", 0),
        "gmgn_ok": safety.get("gmgn_ok", False),
        "gmgn_bundler_wallets": safety.get("gmgn_bundler_wallets", 0),
        "gmgn_sniper_wallets": safety.get("gmgn_sniper_wallets", 0),
        "gmgn_smart_wallets": safety.get("gmgn_smart_wallets", 0),
        "rugcheck_score": safety.get("risk_score"),
        "gates": gates,
    })
    return row


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

    # 3. screen: hard gates (RugCheck safety) → soft score → tier
    survivors: list[dict] = []
    for mint, m in market.items():
        row = screen_token(mint, m)
        if row is not None:
            survivors.append(row)

    survivors.sort(key=lambda r: -r["score"])
    a_tier = [s for s in survivors if s["tier"] == "A"]
    print(f"{len(survivors)} passed the hard gates ({len(a_tier)} A-tier)")
    for s in survivors[:max(config.ALERT_TOP_N, len(a_tier))]:
        print(f"  [{s['tier']}] {s['symbol']:12.12s} score {s['score']:5.1f}  "
              f"liq ${s['liq_usd']:>10,.0f}  mcap ${s['mcap']:>12,.0f}  "
              f"top10 {s['top10_pct']:>4}%  age {s['pair_age_min']:>5.0f}m"
              + (f"  (short: {'; '.join(s['hc_misses'][:2])})" if s["hc_misses"] else ""))

    # 4. anti-spam → ALERT ONLY A-TIER tokens not alerted within the cooldown window.
    # B-tier survivors are logged to the ledger silently, so the ledger accumulates an
    # honest A-vs-B comparison of whether the A-tier band actually earns its name.
    state = _load_state()
    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    fresh = [s for s in a_tier[:config.ALERT_TOP_N]
             if now_s - float(state.get(s["mint"], 0)) >= cooldown]
    if fresh:
        title, body = alerts.format_alert(fresh)
        alerts.send_all(title, body, dry_run=not send)
    else:
        print("(no fresh A-tier survivors to alert — B-tier is ledgered, never alerted)")

    # 5. persist: record BOTH tiers in the ledger, fill matured forward returns, update state
    if not dry_run:
        n_new = ledger.record_alerts(
            [{"mint": s["mint"], "symbol": s["symbol"], "tier": s["tier"],
              "price": s["price_usd"], "mcap": s["mcap"], "liq": s["liq_usd"],
              "score": s["score"], "gates": s["gates"],
              "rugcheck_score": s.get("rugcheck_score")}
             for s in survivors],
            alert_ts=now_s,
        )
        filled, exits = ledger.update_forward(
            now_s,
            snapshot_fn=lambda mint: dex.forward_snapshot(mint, now_s=now_s),
            report_fn=rug.report,
        )
        if exits:
            ex_title, ex_body = alerts.format_exit_alert(exits)
            alerts.send_all(ex_title, ex_body, dry_run=not send)
        for s in fresh:
            state[s["mint"]] = now_s
        json.dump(state, open(config.STATE_PATH, "w"), indent=2)

        # snapshot of this scan's full ranked list — the dashboard renders from this
        json.dump({"scan_ts": now_s, "discovered": len(mints), "enriched": len(market),
                   "survivors": survivors},
                  open(config.SCAN_PATH, "w"), indent=2)
        print(f"committed: {n_new} new ledger row(s) ({len(fresh)} alerted), "
              f"{filled} forward cell(s) filled")

    return survivors


if __name__ == "__main__":
    _send = "--send" in sys.argv
    _commit = "--commit" in sys.argv or _send
    run(dry_run=not _commit, send=_send)
