"""
Price PATHS for ledger rows — the data the ledger has never had.

WHY THIS EXISTS. `ledger.py` samples four horizons (1h/6h/24h/7d) plus a `max_ret_seen`
high-water mark taken on the ~15-minute scan grid. That is enough to say what a position was
worth at four moments and enough to say a level was touched at *some* point, but it cannot
answer the question this project actually needs answered:

    the A-tier band's median return is +34% at 6h and -99% at 24h (cloud ledger, n=13).
    The selection works. There is no exit. WHICH exit would have kept the +34%?

Answering that requires an ordered path, because "did price fall below the stop BEFORE it rose
through the take-profit" is a statement about order, and no number of horizon snapshots contains
it. `max_ret_seen` and `min_ret_seen` are both realised for almost every row (median max +13%,
median min -97%), so without ordering every policy is simultaneously a winner and a loser.

WHY GECKOTERMINAL AND NOT swap-api. `entry_bot/candles.py` uses
`swap-api.pump.fun/v1/coins/{mint}/candles`, which is free and serves lows. Measured 2026-08-12:
it returns the most recent 500 bars **in which trades occurred**, not a contiguous window — so
for a token alerted three weeks ago the active minutes around its alert have long scrolled out.
Checked across the ledger's span, only rows from roughly the last day are covered, at any
interval. GeckoTerminal's pool OHLCV does reach back (an alert from 2026-07-03 is covered by its
1h series), so it is the only free source that can price this ledger's history.

    NOTE for anyone porting `entry_bot/candles.py`: its `ts` is WRONG BY 1000x. swap-api returns
    `timestamp` in MILLISECONDS and candles.py:66 reads it as seconds, so `path_from`'s
    `b["ts"] >= entry_ts` compares seconds against milliseconds and can never exclude a bar.
    entry_bot's verify.py misses it because its fixture uses ts=100/200 with entry=150, which is
    internally consistent. GeckoTerminal's `ohlcv_list` timestamps ARE seconds; this module keeps
    everything in seconds and says so.

RESOLUTION IS A DATA-QUALITY FIELD, NOT AN IMPLEMENTATION DETAIL. Coverage depth trades off
against granularity: 1m reaches back hours, 15m days, 1h weeks. We take the FINEST series that
covers the alert and record which one it was, because a 1h bar's high and low have no order
across a whole hour — a token that peaks four minutes after the alert is unresolvable at 1h.
Any analysis that pools resolutions without reporting the mix is lying about its precision, so
`fetch_path` returns `res` and the evaluator stratifies on it.
"""
from __future__ import annotations

import json
import os
import sys
import time

# selfimprove/ is a subpackage but its siblings live one level up and import each other flat
# (`import config`), so the repo root must be importable however this file is invoked. Same
# bootstrap idiom as signal_lab/deepvalue/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                      # noqa: E402
from http_client import get_json   # noqa: E402

GT = "https://api.geckoterminal.com/api/v2"

# Finest first. (timeframe, aggregate, label, approx_span_sec at limit=1000)
RESOLUTIONS = (
    ("minute", 1, "1m", 1000 * 60),
    ("minute", 15, "15m", 1000 * 15 * 60),
    ("hour", 1, "1h", 1000 * 3600),
)
LIMIT = 1000
CACHE_MAX_AGE_SEC = 7 * 86400          # a dead token's history is immutable; a live one still moves


def _cache(name: str) -> str:
    d = os.path.join(config.CACHE_DIR, "paths")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


DEFERRED = "deferred"    # we could not get an answer (429 / network) — retry later
ABSENT = "absent"        # GeckoTerminal answered and knows no such pool — a real, final answer


def top_pool(mint: str) -> tuple[str, str | None]:
    """(status, deepest-reserve base-leg pool address). status is "ok" | ABSENT | DEFERRED.

    THE THREE-WAY SPLIT IS LOAD-BEARING, and this function got it wrong on its first version.
    `http_client.get_json` returns None for BOTH "HTTP 429" and "no such token", so treating a
    None as "this mint has no pool" silently converts a rate limit into a fabricated outcome. On
    the very first probe run that produced 4 bogus `no_pool`/`no_cover` rows out of 6, purely
    because GeckoTerminal was throttling — the same mistake `corpus/enrich.py` documents as
    having fabricated 472 of 1,400 rows as dead tokens. A deferred row must be retried, never
    scored.

    BASE LEG ONLY. A pool where the mint is the QUOTE token reports the other asset's price —
    `robinhood_screener` measured a $1,888 MIZUKARA from exactly this, and `entry_bot/corpus/
    peaks.py` ports the same filter.
    """
    d = get_json(f"{GT}/networks/solana/tokens/{mint}/pools?page=1",
                 cache_path=_cache(f"pools_{mint}.json"), max_age_sec=CACHE_MAX_AGE_SEC)
    if d is None:
        return DEFERRED, None
    pools = d.get("data")
    if not pools:
        return ABSENT, None
    best, best_res = None, -1.0
    for p in pools:
        a = p.get("attributes") or {}
        rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        base_id = str(rel.get("id") or "")
        if base_id and not base_id.endswith(mint):
            continue                      # mint is the quote leg — its price is not this series
        try:
            res = float(a.get("reserve_in_usd") or 0.0)
        except (TypeError, ValueError):
            res = 0.0
        if res > best_res:
            best, best_res = a.get("address"), res
    return ("ok", best) if best else (ABSENT, None)


def _ohlcv(pool: str, timeframe: str, aggregate: int) -> tuple[str, list]:
    """(status, one OHLCV series) oldest-first, timestamps in SECONDS. Same three-way split."""
    d = get_json(f"{GT}/networks/solana/pools/{pool}/ohlcv/{timeframe}"
                 f"?aggregate={aggregate}&limit={LIMIT}&currency=usd",
                 cache_path=_cache(f"ohlcv_{pool}_{timeframe}{aggregate}.json"),
                 max_age_sec=CACHE_MAX_AGE_SEC)
    if d is None:
        return DEFERRED, []
    lst = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    out = []
    for row in lst:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            out.append({"ts": float(row[0]), "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]),
                        # None when the feed omits volume — distinct from a real 0.0:
                        # the sanitizer drops v==0 bars as phantom prints, and a bar
                        # whose volume is merely UNREPORTED must not be conflated in.
                        "v": float(row[5]) if len(row) > 5 else None})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["ts"])
    return ("ok" if out else ABSENT), out


def fetch_path(mint: str, alert_ts: float) -> dict:
    """Bars at or after `alert_ts` for `mint`, at the finest resolution that covers the alert.

    Returns {"status", "res", "pool", "entry", "bars", "n_before"}.

    `status` is one of:
      ok        — a covering series was found and there is at least one bar at/after the alert
      absent    — GeckoTerminal answered and knows no base-leg pool for this mint
      no_cover  — pools exist and every series answered, but none reaches back to the alert
                  (the active bars around it have scrolled out of the 1000-bar window)
      deferred  — we could not get an answer (429 / network). RETRY; never score.

    `deferred` is separated from the two real answers for the reason `corpus/enrich.py` documents
    at length: conflating "we were rate limited" with "this token has no price" fabricated 472 of
    1,400 rows as dead tokens on an earlier build of the sibling corpus. A row that cannot be
    priced must be dropped from a policy comparison and COUNTED, never treated as a loss.

    `entry` is the OPEN of the first bar at or after the alert — the first price actually
    transactable then. Using the bar's close would be later information.
    """
    st, pool = top_pool(mint)
    if st != "ok":
        return {"status": st, "res": None, "pool": None, "entry": 0.0,
                "bars": [], "n_before": 0}
    # SKIP RESOLUTIONS THAT CANNOT REACH BACK. A 1000-bar 1m series spans ~17 hours, so asking
    # for it on a three-week-old alert spends a call to learn something arithmetic. Start at the
    # finest resolution whose span could plausibly cover the alert's age, and keep one finer as a
    # safety margin (bars are emitted only when trades occur, so a quiet token's series reaches
    # back further in wall-clock than its nominal span). Measured 2026-08-12: this was ~2 wasted
    # calls per row out of ~3, against a hard 30/min shared budget.
    age = max(0.0, time.time() - alert_ts)
    start = 0
    for i, (_tf, _ag, _lb, span) in enumerate(RESOLUTIONS):
        if span >= age:
            start = i
            break
    else:
        start = len(RESOLUTIONS) - 1
    start = max(0, start - 1)              # one finer than needed, as the margin
    deferred_any = False
    for timeframe, agg, label, _span in RESOLUTIONS[start:]:
        bst, bars = _ohlcv(pool, timeframe, agg)
        if bst == DEFERRED:
            deferred_any = True
            continue
        if not bars or bars[0]["ts"] > alert_ts:
            continue                       # does not reach back far enough — try coarser
        after = [b for b in bars if b["ts"] >= alert_ts]
        if not after:
            continue
        return {"status": "ok", "res": label, "pool": pool, "entry": after[0]["o"],
                "bars": after, "n_before": len(bars) - len(after)}
    # every resolution either failed to answer or failed to cover. If ANY was unanswered we
    # cannot claim "no_cover" — that would be the fabrication above.
    return {"status": DEFERRED if deferred_any else "no_cover", "res": None, "pool": pool,
            "entry": 0.0, "bars": [], "n_before": 0}


if __name__ == "__main__":
    import csv
    led = [r for r in csv.DictReader(open(config.LEDGER_PATH)) if r.get("alert_ts")]
    led.sort(key=lambda r: float(r["alert_ts"]))
    print(f"ledger rows: {len(led)}   probing 6 spread across the span\n")
    for r in (led[0], led[len(led) // 4], led[len(led) // 2],
              led[3 * len(led) // 4], led[-20], led[-2]):
        a = float(r["alert_ts"])
        p = fetch_path(r["mint"], a)
        print(f"  {r['symbol'][:12]:<12s} {time.strftime('%m-%d %H:%M', time.gmtime(a))}  "
              f"{p['status']:<9s} res={str(p['res']):<4s} bars={len(p['bars']):>4}  "
              f"entry={p['entry']:.3g}")
