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

BASE_COLS = ["mint", "symbol", "tier", "alert_ts", "entry_price", "entry_mcap",
             "entry_liq", "entry_score", "gate_results_json", "rugcheck_score"]
FWD_COLS: list[str] = []
for _h in HORIZ:
    FWD_COLS += [f"price_{_h}", f"mcap_{_h}", f"ret_{_h}"]
TAIL_COLS = ["max_ret_seen", "min_ret_seen", "rugged_after", "status",
             "tp_alerted", "stop_alerted", "suspect_ticks"]
COLUMNS = BASE_COLS + FWD_COLS + TAIL_COLS

_EMPTY = ("", "nan", "None", "NaN")


def load(path: str | None = None) -> pd.DataFrame:
    path = path or config.LEDGER_PATH
    if os.path.exists(path):
        # astype(object): pandas >=3 makes dtype=str a STRICT string dtype that raises on
        # the float/bool cell writes update_forward() does; object accepts mixed values on
        # every pandas version (the cloud runner installs latest, the Mac runs 2.x).
        return pd.read_csv(path, dtype=str).astype(object)
    return pd.DataFrame(columns=COLUMNS)


def save(df: pd.DataFrame, path: str | None = None) -> None:
    df.reindex(columns=COLUMNS).to_csv(path or config.LEDGER_PATH, index=False)


def _num(v, default=0.0) -> float:
    try:
        if str(v) in _EMPTY:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def record_alerts(rows: list[dict], alert_ts: float, path: str | None = None) -> int:
    """rows: [{mint, symbol, price, mcap, liq, score, gates(dict), rugcheck_score}].
    One entry per token — mints already in the ledger are skipped."""
    led = load(path)
    existing = set(led["mint"].astype(str)) if len(led) else set()
    new = []
    for r in rows:
        if str(r["mint"]) in existing:
            continue
        rec = {c: "" for c in COLUMNS}
        rec.update({
            "mint": r["mint"], "symbol": r.get("symbol", "?"),
            "tier": r.get("tier", "A"),  # pre-tier rows were all alerted => A
            "alert_ts": alert_ts, "entry_price": r.get("price", 0.0),
            "entry_mcap": r.get("mcap", 0.0), "entry_liq": r.get("liq", 0.0),
            "entry_score": r.get("score", 0.0),
            "gate_results_json": json.dumps(r.get("gates", {})),
            "rugcheck_score": r.get("rugcheck_score", ""),
            "max_ret_seen": 0.0, "min_ret_seen": 0.0,
            "rugged_after": False, "status": "open",
            "tp_alerted": 0.0, "stop_alerted": False, "suspect_ticks": 0,
        })
        new.append(rec)
    if new:
        add = pd.DataFrame(new)
        led = add if len(led) == 0 else pd.concat([led, add], ignore_index=True)
        save(led, path)
    return len(new)


def update_forward(now_s: float, snapshot_fn, report_fn=None,
                   path: str | None = None) -> tuple[int, list[dict]]:
    """For each open row, fill any elapsed-but-empty horizon using
    snapshot_fn(mint) -> market dict (with 'price_usd'/'mcap'). snapshot_fn and report_fn
    are injected so this stays unit-testable. Returns (horizon cells filled, exit events).

    Exit events implement the alert's own discipline plan: fire ONCE when an ALERTED row
    (tier A/S — never the silent B control) crosses a TP-ladder multiple or breaches the
    hard stop. This is where returns get REALIZED instead of round-tripping to zero.

    A token that no longer returns a price is treated as dead → that horizon's return is
    -100% (honest: it captures the frequent "went to zero" outcome)."""
    led = load(path)
    if len(led) == 0:
        return 0, []
    filled = 0
    events: list[dict] = []
    last_h = list(HORIZ)[-1]
    for i in led.index:
        if str(led.at[i, "status"]) == "resolved":
            continue  # 7d horizon already recorded → stop spending API calls on it
        entry = _num(led.at[i, "entry_price"])
        if entry <= 0:
            continue
        alert_ts = _num(led.at[i, "alert_ts"])
        mint = str(led.at[i, "mint"])

        # One live snapshot per open row per run. Beyond filling the fixed horizons, we
        # use it to track the hourly HIGH-WATER mark, so `max_ret_seen` reflects whether a
        # coin ever ran — not just where it happened to sit at 1h/6h/24h/7d. A dead token
        # (no price) counts as -100%.
        snap = snapshot_fn(mint) or {}
        cur_price = _num(snap.get("price_usd"))

        # Quote-integrity gate (FOMO regression, 2026-09). Dexscreener re-picks the
        # deepest pair on every call; a pair-switch (or broken tick) changes the price
        # scale and once poisoned max_ret_seen with a 1283x that no horizon cell ever
        # saw. Implied supply (mcap/price) is invariant across honest quotes, so a
        # snapshot whose supply disagrees with the entry's by > SUPPLY_DRIFT_MAX is
        # rejected wholesale — no ratchet, no exit events, no horizon fills this run
        # (write-once cells stay empty and retry on the next run's quote). A dead
        # token (no price at all) is NOT suspect: -100% stays the honest outcome.
        cur_mcap = _num(snap.get("mcap"))
        entry_mcap = _num(led.at[i, "entry_mcap"])
        if cur_price > 0 and cur_mcap > 0 and entry_mcap > 0:
            supply_ratio = (cur_mcap / cur_price) / (entry_mcap / entry)
            if not (1.0 / config.SUPPLY_DRIFT_MAX <= supply_ratio
                    <= config.SUPPLY_DRIFT_MAX):
                led.at[i, "suspect_ticks"] = _num(led.at[i, "suspect_ticks"]) + 1
                continue

        cur_ret = (cur_price / entry - 1.0) if cur_price > 0 else -1.0
        led.at[i, "max_ret_seen"] = max(cur_ret, _num(led.at[i, "max_ret_seen"]))
        led.at[i, "min_ret_seen"] = min(cur_ret, _num(led.at[i, "min_ret_seen"]))

        # exit signals — alerted tiers only, each level fires exactly once
        sym = str(led.at[i, "symbol"])
        if str(led.at[i, "tier"]) != "B":
            if (str(led.at[i, "stop_alerted"]).lower() != "true"
                    and cur_ret <= -config.HARD_STOP_PCT):
                led.at[i, "stop_alerted"] = True
                events.append({"kind": "stop", "symbol": sym, "mint": mint,
                               "ret": cur_ret, "price": cur_price})
            cur_mult = (cur_price / entry) if cur_price > 0 else 0.0
            tp_done = _num(led.at[i, "tp_alerted"])
            crossed = [(m, f) for m, f in config.TP_LADDER
                       if cur_mult >= m and m > tp_done]
            if crossed:
                led.at[i, "tp_alerted"] = max(m for m, _f2 in crossed)
                events.append({"kind": "tp", "symbol": sym, "mint": mint,
                               "levels": crossed, "price": cur_price,
                               "mult": cur_mult})

        for hname, hsec in HORIZ.items():
            col = f"ret_{hname}"
            if str(led.at[i, col]) not in _EMPTY:
                continue
            if now_s - alert_ts < hsec:
                continue
            led.at[i, f"price_{hname}"] = cur_price if cur_price > 0 else 0.0
            led.at[i, f"mcap_{hname}"] = _num(snap.get("mcap")) if cur_price > 0 else 0.0
            led.at[i, col] = cur_ret
            filled += 1

        if report_fn is not None and str(led.at[i, "rugged_after"]).lower() != "true":
            rep = report_fn(mint)
            if rep and rep.get("rugged"):
                led.at[i, "rugged_after"] = True

        if str(led.at[i, f"ret_{last_h}"]) not in _EMPTY:
            led.at[i, "status"] = "resolved"

    save(led, path)
    return filled, events


def _fmt_ret(v) -> str:
    """A return cell: signed % if filled, or '·' if that horizon hasn't matured yet."""
    if str(v) in _EMPTY:
        return "    ·"
    return f"{float(v) * 100:+5.0f}%"


TIER_LABEL = {"A": "alerted", "B": "silent control", "S": "smart-money experiment"}


def summary(path: str | None = None) -> None:
    led = load(path)
    n = len(led)
    print(f"ledger{' (S experiment, local-only)' if path else ''}: {n} token(s)")
    if n == 0:
        print("  (nothing logged yet — run `python3 run.py --commit` to start tracking)")
        return

    # ── per-token results (winners first) ────────────────────────────────────────
    led = led.copy()
    led["_mx"] = pd.to_numeric(led["max_ret_seen"], errors="coerce").fillna(-9.0)
    led["_tier"] = led["tier"].astype(str).where(
        led["tier"].astype(str).isin(["A", "B", "S"]), "A")
    led = led.sort_values("_mx", ascending=False)
    print(f"  {'':2s}{'symbol':12s} {'1h':>6s} {'6h':>6s} {'24h':>6s} {'7d':>6s}  {'best':>6s}  status")
    for _, r in led.iterrows():
        flag = "  RUGGED" if str(r["rugged_after"]).lower() == "true" else ""
        print(f"  {r['_tier']:2s}{str(r['symbol'])[:12]:12s} "
              f"{_fmt_ret(r['ret_1h'])} {_fmt_ret(r['ret_6h'])} "
              f"{_fmt_ret(r['ret_24h'])} {_fmt_ret(r['ret_7d'])}  "
              f"{_num(r['max_ret_seen'])*100:+5.0f}%  {r['status']}{flag}")
    print()

    # ── aggregate scorecard, split by tier (A = alerted, B = silent control) ─────
    for tier in ("A", "B", "S"):
        sub = led[led["_tier"] == tier]
        if len(sub) == 0:
            continue
        print(f"  tier {tier} ({TIER_LABEL[tier]}), n={len(sub)}:")
        for h in HORIZ:
            r = pd.to_numeric(sub[f"ret_{h}"], errors="coerce").dropna()
            if len(r):
                print(f"    {h:>3s}: n={len(r):3d}  hit-rate={ (r > 0).mean()*100:4.0f}%  "
                      f"median={r.median()*100:+6.1f}%  best={r.max()*100:+.0f}%  "
                      f"worst={r.min()*100:+.0f}%")
        ra = sub["rugged_after"].astype(str).str.lower().eq("true").mean()
        print(f"    rugged-after-passing-gates: {ra*100:.0f}%")
    print("  If tier A does not clearly beat tier B here, the A-tier band is NOT adding "
          "signal — loosen/rethink it rather than trusting the label.")
    print("  Reminder: negative expectancy is the base rate. A losing scorecard here is "
          "the screen doing its job — telling you not to scale.")


if __name__ == "__main__":
    summary()
    if os.path.exists(config.LEDGER_S_PATH):
        print()
        summary(config.LEDGER_S_PATH)
