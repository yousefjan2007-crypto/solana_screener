"""
Live multi-policy paper book — every exit policy trading the same alerts, simultaneously.

WHY THIS EXISTS, AND WHY IT IS THE RIGHT NEXT STEP RATHER THAN MORE BACKTESTING.
The backtest (selfimprove/evaluate.py over selfimprove/paths.py bars) says an exit is worth
about +0.68 per dollar against the system's current hold-to-end behaviour. But it has three
weaknesses a live book removes outright:

 1. WITHIN-BAR AMBIGUITY. An OHLC bar gives four numbers and no path, so a path-dependent exit
    (a trailing stop) has to be bracketed by assuming an ordering. That bracketing is where the
    worst bug of 2026-08-12 lived: the simulator ratcheted the high-water mark to the bar's HIGH
    before testing the same bar's LOW, which is look-ahead inside the bar. It moved the headline
    policy from mean +0.142 / LB +0.006 (appeared to clear the bar) to mean -0.164 / LB -0.184.
    A tick stream has ONE price per observation. There is no ordering to assume.
 2. COARSE BARS. 347 of 639 backfilled rows priced on 1-HOUR bars, because GeckoTerminal's 1m
    series only reaches back ~17 hours. An hour bar can contain an entire pump and dump. Live
    ticks are seconds old by construction.
 3. SEARCH CONTAMINATION. Every number so far was found by looking at history. Live rows did not
    exist when the policies were written, which is the only real cure.

...and one it removes that nothing else can: EXECUTION COST IS MEASURED, NOT ASSUMED.
`policies.round_trip_cost()` charges a flat 2% (2 x PAPER_SLIPPAGE_BPS). Measured on live
Jupiter quotes, 2026-08-12, buying $10 and immediately selling: Papoi -2.8%, TIGER -3.6%,
ETG **-27.6%** (that one quoted 100% price impact on a $10 buy). A flat 2% is therefore
optimistic, badly so on illiquid names, and only real quotes can tell you which is which.

=============================== HOW THE COMPARISON STAYS FAIR ===============================
ONE shared entry per alert. A single Jupiter buy quote is taken once, and all N policies inherit
that identical fill — same token, same moment, same cost basis. So any difference between two
policies is caused by the exit and nothing else. Then ONE sell quote per open mint per tick
feeds every policy's state machine, which is why the cost is O(open mints), not O(mints x policies).
=============================================================================================

TWO FILL CONVENTIONS, AND THEY ARE DELIBERATELY ASYMMETRIC:
  * A ladder rung is a pre-committed LIMIT: it fills at the rung level, never at the observed
    tick. If price gaps from 1.5x to 5x between ticks, a real limit at 2x fills near 2x; marking
    it at 5x would be the same class of leak as reading an eventual peak as a fill price.
  * A stop/trail is a MARKET exit and fills at the observed tick price. Between ticks price may
    have been worse, so this leg is mildly OPTIMISTIC. `gap_s` is recorded on every fill so the
    size of that optimism stays auditable rather than assumed away.

PARTIAL SELLS ARE PRO-RATED FROM A FULL-SIZE QUOTE, WHICH IS CONSERVATIVE. Quoting each policy's
partial sell separately would be N quotes per mint per tick against a ~1 req/s free tier. Instead
one full-size sell quote is taken and partial proceeds are pro-rated from it. Verified convex on
live quotes 2026-08-12 — a smaller sell always gets a BETTER per-token price (+0.09% on Papoi,
+0.30% on TIGER, **+7.3%** on illiquid ETG at half size), so pro-rating understates proceeds.

Reproducibility: `now_s` is always passed in by the caller, never read in a compute path.
No keys, no funds, no transactions — quotes only, exactly like paper_exec.py.
"""
from __future__ import annotations

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # selfimprove/ itself

import config                      # noqa: E402
import policies as POL             # noqa: E402  (selfimprove/policies.py)
from http_client import get_json   # noqa: E402

BOOK_PATH = os.path.join(config.DATA_DIR, "livebook.json")
FILLS_PATH = os.path.join(config.DATA_DIR, "livebook_fills.csv")
TICKS_PATH = os.path.join(config.DATA_DIR, "livebook_ticks.jsonl")
FEED_STATE_PATH = os.path.join(config.DATA_DIR, "livebook_feed.json")
MISSED_PATH = os.path.join(config.DATA_DIR, "livebook_missed.jsonl")

FILL_COLUMNS = ["ts", "mint", "symbol", "tier", "policy", "side", "frac_of_original",
                "px", "usd_proceeds", "gap_s", "note"]

# A position is dropped once every policy has closed it, or after this long. The longest
# max_hold_s in the family is 6h, so past that no policy has a decision left and the only
# reason to keep quoting is to give hold_to_end and the no-stop ladder an honest terminal
# mark. That does not need minute resolution, so the cadence drops — otherwise one immortal
# policy would cost 1,440 quotes per position against a ~1 req/s budget shared with three
# other jobs.
MAX_TRACK_S = 12 * 3600
DECISION_HORIZON_S = 6 * 3600         # past this, no policy can still act
TICK_INTERVAL_S = 60.0                # while decisions are live
TICK_INTERVAL_LATE_S = 900.0          # after the decision horizon, mark-to-market only


# ── atomic state, because a crash mid-write must not corrupt the book ───────────────
def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_atomic(obj, path: str) -> None:
    """Write via a temp file + os.replace. paper_exec.py rewrites its positions JSON in place,
    so a crash between truncate and write loses the whole book; os.replace is atomic on POSIX."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def _append_fill(row: dict) -> None:
    new = not os.path.exists(FILLS_PATH)
    with open(FILLS_PATH, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FILL_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in FILL_COLUMNS})


# ── quotes ─────────────────────────────────────────────────────────────────────────
def quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    """One live Jupiter routing quote. Never cached — a stale quote is a fabricated fill."""
    url = (f"{config.JUP_QUOTE_URL}?inputMint={input_mint}&outputMint={output_mint}"
           f"&amount={int(amount_raw)}&slippageBps={config.PAPER_SLIPPAGE_BPS}")
    d = get_json(url)
    return d if isinstance(d, dict) and d.get("outAmount") else None


def _new_policy_state() -> dict:
    return {"remaining": 1.0, "peak_px": 0.0, "rungs_left": None,
            "realized_usd": 0.0, "closed": False, "closed_ts": None, "close_reason": ""}


def open_alert(mint: str, symbol: str, tier: str, now_s: float, *, quote_fn=quote) -> dict | None:
    """Take the ONE shared entry fill and seed a state machine per policy.

    Idempotent per mint: the 15-minute cloud scan and the live listener both surface alerts, so
    a mint can arrive twice. Re-entering would double-count it in every policy at once.
    """
    book = _load(BOOK_PATH, {})
    if mint in book:
        return None
    usd = config.STACK_USD * config.POSITION_PCT
    q = quote_fn(config.USDC_MINT, mint, round(usd * 1e6))
    if q is None:
        _append_fill({"ts": now_s, "mint": mint, "symbol": symbol, "tier": tier,
                      "policy": "*", "side": "buy_failed", "usd_proceeds": 0.0,
                      "note": "no route at alert — not opened"})
        return None
    tokens = int(q["outAmount"])
    if tokens <= 0:
        return None
    entry_px = usd / tokens                     # USD per raw token, the basis for every multiple
    # ALERT_SEQ — a strictly increasing integer, never reused, never renumbered. It is what makes
    # "this policy was nominated BEFORE these rows existed" a checkable fact rather than a claim.
    # Timestamps cannot do this job: they are editable, they collide, and a clock change reorders
    # them. improve.py's promotion test reads only rows with alert_seq > nominated_at_alert_seq,
    # so this is the single most load-bearing field in the whole design.
    seq = 1 + max([p.get("alert_seq", 0) for p in book.values()] or [0])
    pos = {"mint": mint, "symbol": symbol, "tier": tier, "opened_ts": now_s, "alert_seq": seq,
           "cost_usd": usd, "tokens_raw": tokens, "entry_px": entry_px,
           "entry_impact_pct": float(q.get("priceImpactPct") or 0.0),
           "last_tick_ts": now_s, "n_ticks": 0, "done": False,
           "policies": {name: _new_policy_state()
                        for name in list(POL.POLICIES) + list(getattr(POL, "CONTROLS", {}))}}
    for st in pos["policies"].values():
        st["peak_px"] = entry_px
    book[mint] = pos
    _save_atomic(book, BOOK_PATH)
    _append_fill({"ts": now_s, "mint": mint, "symbol": symbol, "tier": tier, "policy": "*",
                  "side": "buy", "frac_of_original": 1.0, "px": entry_px,
                  "usd_proceeds": -usd, "gap_s": 0,
                  "note": f"shared entry, impact {pos['entry_impact_pct']:.4f}"})
    return pos


def _step_policy(pos: dict, name: str, st: dict, px: float, now_s: float,
                 usd_full: float, gap_s: float) -> list:
    """Advance ONE policy by one observed price. Returns the fills it produced.

    A tick is a single price, so unlike the bar simulator there is no ordering to assume. The
    one genuine ambiguity left is that both a rung and a protective exit can be satisfied by the
    same tick (peak 100x, price now 5x: the 2x rung is below us AND the 30% trail is broken).
    That means the move happened BETWEEN ticks and we never observed the intervening prices, so
    the protective exit wins and the remainder closes at the observed price. Crediting the rung
    as well would be claiming a fill at a price we never saw.
    """
    # Explicit membership test, NOT `POLICIES.get(name) or CONTROLS[name]`: hold_to_end's policy
    # is the empty dict, which is falsy, so `or` fell through and raised KeyError on the one
    # policy that matters most as the baseline.
    pol = POL.POLICIES[name] if name in POL.POLICIES else getattr(POL, "CONTROLS", {})[name]
    if st["closed"]:
        return []
    if st["rungs_left"] is None:
        st["rungs_left"] = [list(x) for x in (pol.get("ladder") or [])]
    entry_px = pos["entry_px"]
    fills = []
    if px > st["peak_px"]:
        st["peak_px"] = px

    stop_frac, trail_frac = pol.get("stop"), pol.get("trail")
    levels = []
    if stop_frac is not None:
        levels.append(entry_px * (1.0 - stop_frac))
    if trail_frac is not None:
        levels.append(st["peak_px"] * (1.0 - trail_frac))
    exit_level = max(levels) if levels else None

    def _close(reason, fill_px):
        frac = st["remaining"]
        if frac <= 1e-9:
            st["closed"] = True
            return
        usd = usd_full * frac * (fill_px / px if px > 0 else 0.0)
        st["realized_usd"] += usd
        st["remaining"] = 0.0
        st["closed"] = True
        st["closed_ts"] = now_s
        st["close_reason"] = reason
        fills.append({"ts": now_s, "mint": pos["mint"], "symbol": pos["symbol"],
                      "tier": pos["tier"], "policy": name, "side": reason,
                      "frac_of_original": round(frac, 6), "px": fill_px,
                      "usd_proceeds": round(usd, 6), "gap_s": round(gap_s, 1), "note": ""})

    if exit_level is not None and px <= exit_level:
        _close("trail" if (trail_frac is not None and
                           st["peak_px"] * (1.0 - trail_frac) >= (levels[0] if stop_frac is not None else -1))
               else "stop", px)
        return fills

    # ladder rungs: pre-committed LIMIT levels, filled AT the level, never at the observed tick
    still = []
    for lv, fr in st["rungs_left"]:
        if px >= entry_px * lv and st["remaining"] > 1e-9:
            frac = min(fr, st["remaining"])
            fill_px = entry_px * lv
            usd = usd_full * frac * (fill_px / px if px > 0 else 0.0)
            st["realized_usd"] += usd
            st["remaining"] -= frac
            fills.append({"ts": now_s, "mint": pos["mint"], "symbol": pos["symbol"],
                          "tier": pos["tier"], "policy": name, "side": f"tp_{lv:g}x",
                          "frac_of_original": round(frac, 6), "px": fill_px,
                          "usd_proceeds": round(usd, 6), "gap_s": round(gap_s, 1),
                          "note": "limit fill at rung level"})
        else:
            still.append([lv, fr])
    st["rungs_left"] = still
    if st["remaining"] <= 1e-9:
        st["closed"] = True
        st["closed_ts"] = now_s
        st["close_reason"] = "ladder_complete"
        return fills

    mh = pol.get("max_hold_s")
    if mh is not None and (now_s - pos["opened_ts"]) >= mh:
        _close("time_exit", px)
    return fills


def _cloud_ledger_rows() -> list:
    """The cloud-committed ledger, read WITHOUT touching the working tree.

    `run.py` is what normally opens positions, but it runs on the GitHub Actions runner while this
    ticker is launchd-local — so left alone the book never receives an alert. The cloud is the
    system of record, so the book reads it: `git fetch` (incremental, no auth needed even if the
    repo were private) then `git show origin/main:data/ledger.csv`, which reads the blob straight
    out of the object store. Deliberately NOT `git pull`: a data-collection job must never be able
    to create a merge conflict or move the user's HEAD.
    """
    import csv as _csv
    import io
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=root, timeout=60,
                       capture_output=True)
        out = subprocess.run(["git", "show", "origin/main:data/ledger.csv"], cwd=root,
                             timeout=60, capture_output=True)
        if out.returncode != 0:
            return []
        return list(_csv.DictReader(io.StringIO(out.stdout.decode("utf-8", "replace"))))
    except Exception:
        return []                      # a feed outage must never kill the tick loop


def feed_from_ledger(now_s: float, *, quote_fn=quote, rows_fn=_cloud_ledger_rows,
                     verbose: bool = True) -> dict:
    """Open a book position for each new ledger alert. Returns counts.

    A WATERMARK, not a scan of the whole file: the first run sets it to `now` so the 1,000-row
    historical backlog is skipped rather than opened at prices that are weeks stale (and rather
    than re-logging 1,000 'too old' misses on every tick, forever).
    """
    book = _load(BOOK_PATH, {})
    state = _load(FEED_STATE_PATH, {})
    mark = state.get("watermark_alert_ts")
    rows = rows_fn()
    stats = {"seen": len(rows), "opened": 0, "too_late": 0, "already": 0}
    if not rows:
        return stats
    if mark is None:                   # first run — start from now, skip the backlog
        state["watermark_alert_ts"] = now_s
        _save_atomic(state, FEED_STATE_PATH)
        if verbose:
            print(f"feed: watermark initialised at {now_s:.0f}; "
                  f"{len(rows)} historical rows skipped")
        return stats
    newest = mark
    for r in rows:
        try:
            ats = float(r.get("alert_ts") or 0)
        except (TypeError, ValueError):
            continue
        if ats <= mark or not r.get("mint"):
            continue
        newest = max(newest, ats)
        if r["mint"] in book:
            stats["already"] += 1
            continue
        lag = now_s - ats
        if lag > config.MAX_ENTRY_LAG_S:
            stats["too_late"] += 1
            with open(MISSED_PATH, "a") as fh:
                fh.write(json.dumps({"ts": now_s, "mint": r["mint"],
                                     "symbol": r.get("symbol", ""), "tier": r.get("tier", ""),
                                     "alert_ts": ats, "entry_lag_s": round(lag, 1),
                                     "reason": "entry lag exceeds MAX_ENTRY_LAG_S"}) + "\n")
            continue
        pos = open_alert(r["mint"], r.get("symbol", "?"), r.get("tier", "B"), now_s,
                         quote_fn=quote_fn)
        if pos is not None:
            pos_book = _load(BOOK_PATH, {})
            if r["mint"] in pos_book:          # stamp the lag on the position we just opened
                pos_book[r["mint"]]["entry_lag_s"] = round(lag, 1)
                pos_book[r["mint"]]["alert_ts"] = ats
                _save_atomic(pos_book, BOOK_PATH)
            stats["opened"] += 1
            book[r["mint"]] = pos
    state["watermark_alert_ts"] = newest
    _save_atomic(state, FEED_STATE_PATH)
    if verbose and (stats["opened"] or stats["too_late"]):
        print(f"feed: opened {stats['opened']}, {stats['too_late']} past the "
              f"{config.MAX_ENTRY_LAG_S/60:.0f}min entry-lag cap")
    return stats


def tick(now_s: float, *, quote_fn=quote, verbose: bool = True) -> dict:
    """One cycle: price every open mint once, advance every policy, persist.

    FILLS ARE BUFFERED AND FLUSHED ONLY AFTER THE STATE WRITE SUCCEEDS. The first version
    appended each fill to the CSV the instant it happened and saved state at the end of the
    cycle. On 2026-08-15 a KeyError mid-loop meant `_save_atomic` never ran, so every policy
    re-fired on the next tick and appended again — `sell_30m` logged 199 fills from ONE position
    before the pattern was noticed. Ordering the durable write first makes a crash cost one
    cycle instead of corrupting the ledger without bound.
    """
    book = _load(BOOK_PATH, {})
    pending_fills: list = []
    stats = {"open": 0, "quoted": 0, "no_route": 0, "fills": 0, "retired": 0}
    for mint, pos in list(book.items()):
        if pos.get("done"):
            continue
        stats["open"] += 1
        gap_s = now_s - pos.get("last_tick_ts", pos["opened_ts"])
        age = now_s - pos["opened_ts"]
        want = TICK_INTERVAL_S if age < DECISION_HORIZON_S else TICK_INTERVAL_LATE_S
        if gap_s < want * 0.9:            # 0.9 so launchd jitter never skips a whole cycle
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        q = quote_fn(mint, config.USDC_MINT, pos["tokens_raw"])
        if q is None:
            # No route on a memecoin means no liquidity. That is a real outcome, not an error:
            # every still-open policy closes at zero, which is exactly "could not exit a dead coin".
            stats["no_route"] += 1
            for name, st in pos["policies"].items():
                if not st["closed"]:
                    st.update(remaining=0.0, closed=True, closed_ts=now_s,
                              close_reason="no_route")
                    pending_fills.append({"ts": now_s, "mint": mint, "symbol": pos["symbol"],
                                          "tier": pos["tier"], "policy": name,
                                          "side": "no_route", "frac_of_original": 0.0,
                                          "px": 0.0, "usd_proceeds": 0.0,
                                          "gap_s": round(gap_s, 1),
                                          "note": "NO ROUTE — dead coin, proceeds $0"})
                    stats["fills"] += 1
            pos["done"] = True
            stats["retired"] += 1
            continue
        stats["quoted"] += 1
        # A position opened before a policy existed has no state for it. Backfill rather than
        # KeyError: the family grows over time (negative controls were added after this book had
        # live positions), and a missing key must never be able to halt the tick loop.
        for name in list(POL.POLICIES) + list(getattr(POL, "CONTROLS", {})):
            pos["policies"].setdefault(name, _new_policy_state())
        usd_full = int(q["outAmount"]) / 1e6
        px = usd_full / pos["tokens_raw"]
        pos["n_ticks"] += 1
        with open(TICKS_PATH, "a") as fh:
            fh.write(json.dumps({"ts": now_s, "mint": mint, "px": px, "usd_full": usd_full,
                                 "impact": float(q.get("priceImpactPct") or 0.0),
                                 "gap_s": round(gap_s, 1),
                                 "mult": px / pos["entry_px"] if pos["entry_px"] else 0.0}) + "\n")
        for name in list(POL.POLICIES) + list(getattr(POL, "CONTROLS", {})):
            for f in _step_policy(pos, name, pos["policies"][name], px, now_s, usd_full, gap_s):
                pending_fills.append(f)
                stats["fills"] += 1
        pos["last_tick_ts"] = now_s
        if all(s["closed"] for s in pos["policies"].values()) or \
                (now_s - pos["opened_ts"]) > MAX_TRACK_S:
            # mark anything still open at the last observed price, then retire
            for name, st in pos["policies"].items():
                if not st["closed"]:
                    st["realized_usd"] += usd_full * st["remaining"]
                    st.update(remaining=0.0, closed=True, closed_ts=now_s,
                              close_reason="tracking_window_end")
            pos["done"] = True
            stats["retired"] += 1
    _save_atomic(book, BOOK_PATH)     # durable FIRST...
    for f in pending_fills:           # ...then the ledger, so a crash cannot duplicate
        _append_fill(f)
    if verbose:
        print(f"tick: {stats['open']} open, {stats['quoted']} quoted, "
              f"{stats.get('skipped', 0)} not-yet-due, {stats['no_route']} no-route, "
              f"{stats['fills']} fills, {stats['retired']} retired")
    return stats


def scorecard() -> None:
    """Per-policy realised P&L on the live book. This is the number that matters — it is net of
    REAL quoted execution cost, out of sample, and free of any within-bar assumption."""
    book = _load(BOOK_PATH, {})
    if not book:
        print("livebook: empty — no alerts tracked yet")
        return
    done = [p for p in book.values() if p.get("done")]
    print(f"livebook: {len(book)} position(s), {len(done)} complete, "
          f"{len(book) - len(done)} still tracking")
    if not done:
        print("  (no completed positions yet — policies are still running)")
        return
    print(f"\n  {'policy':<24s} {'n':>4s} {'mean ret':>9s} {'median':>8s} {'win%':>6s} {'vs hold':>8s}")
    base = None
    rows = []
    for name in POL.POLICIES:
        rets = []
        for p in done:
            st = p["policies"].get(name)
            if st and p["cost_usd"] > 0:
                rets.append(st["realized_usd"] / p["cost_usd"] - 1.0)
        if not rets:
            continue
        m = sum(rets) / len(rets)
        s = sorted(rets)
        rows.append((m, name, len(rets), s[len(s) // 2], sum(1 for r in rets if r > 0) / len(rets)))
        if name == "hold_to_end":
            base = m
    for m, name, n, med, win in sorted(rows, reverse=True):
        vs = f"{m - base:+8.3f}" if base is not None else "       -"
        print(f"  {name:<24s} {n:>4} {m:>+9.3f} {med:>+8.3f} {win:>6.1%} {vs}")
    print("\n  net of REAL quoted execution cost. The backtest charges a flat 2%; live round")
    print("  trips measured -2.8% to -27.6% depending on liquidity, so treat backtest P&L as")
    print("  optimistic until this table has enough rows to replace it.")


def main() -> None:
    import time as _t
    now_s = _t.time()                      # captured ONCE at the entry point, then threaded
    if "--scorecard" in sys.argv:
        scorecard()
        return
    if "--tick" in sys.argv:
        feed_from_ledger(now_s)      # pull new alerts from the cloud ledger first...
        tick(now_s)                  # ...then advance every open position
        return
    print(__doc__.strip().split("\n")[0])
    print("\nusage:")
    print("  python3 selfimprove/livebook.py --tick        # one cycle (launchd runs this)")
    print("  python3 selfimprove/livebook.py --scorecard   # per-policy live P&L")
    scorecard()


if __name__ == "__main__":
    main()
