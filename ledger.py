"""
Honest track-record ledger — the anti-over-claiming centerpiece of this project.

Every ALERTED token is logged with an entry snapshot. On later runs we fill forward
returns at 1h/6h/24h/7d from live Dexscreener prices, and flag any token that rugged
AFTER passing our gates. summary() then prints YOUR screen's real hit-rate and return
distribution — so you learn whether it actually works BEFORE scaling risk, instead of
trusting a backtest fantasy.

Reproducibility: alert_ts (live epoch seconds) is passed IN by the caller and used only
for horizon bookkeeping — never in any scoring path — matching the vrp/dv monitor
precedent. The screen itself stays deterministic.
"""
from __future__ import annotations

import json
import os

import pandas as pd

import config

HORIZ = config.LEDGER_HORIZONS  # {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

BASE_COLS = ["mint", "symbol", "alert_ts", "entry_price", "entry_mcap", "entry_liq",
             "entry_score", "gate_results_json", "rugcheck_score"]
FWD_COLS: list[str] = []
for _h in HORIZ:
    FWD_COLS += [f"price_{_h}", f"mcap_{_h}", f"ret_{_h}"]
TAIL_COLS = ["max_ret_seen", "min_ret_seen", "rugged_after", "status"]
COLUMNS = BASE_COLS + FWD_COLS + TAIL_COLS

_EMPTY = ("", "nan", "None", "NaN")


def load() -> pd.DataFrame:
    if os.path.exists(config.LEDGER_PATH):
        return pd.read_csv(config.LEDGER_PATH, dtype=str)
    return pd.DataFrame(columns=COLUMNS)


def save(df: pd.DataFrame) -> None:
    df.reindex(columns=COLUMNS).to_csv(config.LEDGER_PATH, index=False)


def _num(v, default=0.0) -> float:
    try:
        if str(v) in _EMPTY:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def record_alerts(rows: list[dict], alert_ts: float) -> int:
    """rows: [{mint, symbol, price, mcap, liq, score, gates(dict), rugcheck_score}].
    One entry per token — mints already in the ledger are skipped."""
    led = load()
    existing = set(led["mint"].astype(str)) if len(led) else set()
    new = []
    for r in rows:
        if str(r["mint"]) in existing:
            continue
        rec = {c: "" for c in COLUMNS}
        rec.update({
            "mint": r["mint"], "symbol": r.get("symbol", "?"),
            "alert_ts": alert_ts, "entry_price": r.get("price", 0.0),
            "entry_mcap": r.get("mcap", 0.0), "entry_liq": r.get("liq", 0.0),
            "entry_score": r.get("score", 0.0),
            "gate_results_json": json.dumps(r.get("gates", {})),
            "rugcheck_score": r.get("rugcheck_score", ""),
            "max_ret_seen": 0.0, "min_ret_seen": 0.0,
            "rugged_after": False, "status": "open",
        })
        new.append(rec)
    if new:
        add = pd.DataFrame(new)
        led = add if len(led) == 0 else pd.concat([led, add], ignore_index=True)
        save(led)
    return len(new)


def update_forward(now_s: float, snapshot_fn, report_fn=None) -> int:
    """For each open row, fill any elapsed-but-empty horizon using
    snapshot_fn(mint) -> market dict (with 'price_usd'/'mcap'). snapshot_fn and report_fn
    are injected so this stays unit-testable. Returns the number of horizon cells filled.

    A token that no longer returns a price is treated as dead → that horizon's return is
    -100% (honest: it captures the frequent "went to zero" outcome)."""
    led = load()
    if len(led) == 0:
        return 0
    filled = 0
    last_h = list(HORIZ)[-1]
    for i in led.index:
        entry = _num(led.at[i, "entry_price"])
        if entry <= 0:
            continue
        alert_ts = _num(led.at[i, "alert_ts"])
        mint = str(led.at[i, "mint"])
        snap = None
        for hname, hsec in HORIZ.items():
            col = f"ret_{hname}"
            if str(led.at[i, col]) not in _EMPTY:
                continue
            if now_s - alert_ts < hsec:
                continue
            if snap is None:
                snap = snapshot_fn(mint) or {}
            price = _num(snap.get("price_usd"))
            if price <= 0:
                led.at[i, f"price_{hname}"] = 0.0
                led.at[i, f"mcap_{hname}"] = 0.0
                led.at[i, col] = -1.0
            else:
                led.at[i, f"price_{hname}"] = price
                led.at[i, f"mcap_{hname}"] = _num(snap.get("mcap"))
                led.at[i, col] = price / entry - 1.0
            filled += 1

        rets = [_num(led.at[i, f"ret_{h}"]) for h in HORIZ
                if str(led.at[i, f"ret_{h}"]) not in _EMPTY]
        if rets:
            led.at[i, "max_ret_seen"] = max(rets + [_num(led.at[i, "max_ret_seen"])])
            led.at[i, "min_ret_seen"] = min(rets + [_num(led.at[i, "min_ret_seen"])])

        if report_fn is not None and str(led.at[i, "rugged_after"]).lower() != "true":
            rep = report_fn(mint)
            if rep and rep.get("rugged"):
                led.at[i, "rugged_after"] = True

        if str(led.at[i, f"ret_{last_h}"]) not in _EMPTY:
            led.at[i, "status"] = "resolved"

    save(led)
    return filled


def summary() -> None:
    led = load()
    n = len(led)
    print(f"ledger: {n} alerted token(s)")
    if n == 0:
        print("  (nothing logged yet — run `python3 run.py --commit` to start tracking)")
        return
    for h in HORIZ:
        r = pd.to_numeric(led[f"ret_{h}"], errors="coerce").dropna()
        if len(r):
            print(f"  {h:>3s}: n={len(r):3d}  hit-rate={ (r > 0).mean()*100:4.0f}%  "
                  f"median={r.median()*100:+6.1f}%  best={r.max()*100:+.0f}%  "
                  f"worst={r.min()*100:+.0f}%")
    ra = led["rugged_after"].astype(str).str.lower().eq("true").mean()
    print(f"  rugged-after-passing-gates: {ra*100:.0f}%")
    print("  Reminder: negative expectancy is the base rate. A losing scorecard here is "
          "the screen doing its job — telling you not to scale.")


if __name__ == "__main__":
    summary()
