"""
Fetch a price path for every ledger row. Resumable, cache-backed, safe to re-run.

RUN IT REPEATEDLY. GeckoTerminal's free tier is a hard 30 calls/min and
`com.yousefjan.robinhood-screener` polls the same IP every 2 minutes, so a single pass will
leave rows `deferred` (rate-limited). Deferred rows are NOT written as failures — they are simply
absent from the output, so the next pass retries exactly them and nothing else. Two or three
passes converge. Never interpret a missing row as a dead token; see paths.fetch_path.

Output: data/paths.jsonl, one record per priced row:
    {mint, symbol, tier, alert_ts, res, entry, n_bars, bars:[{ts,o,h,l,c,v}, ...]}
`res` is the bar resolution actually used (1m / 15m / 1h) and MUST be carried into any analysis —
an hour-long bar cannot resolve a token that peaks four minutes after the alert.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import paths as P      # noqa: E402  (selfimprove/paths.py — flat import, see its docstring)

OUT = os.path.join(config.DATA_DIR, "paths.jsonl")


def _done() -> set:
    if not os.path.exists(OUT):
        return set()
    got = set()
    with open(OUT) as fh:
        for line in fh:
            try:
                got.add(json.loads(line)["mint"])
            except Exception:
                continue
    return got


def run(ledger_path: str | None = None, limit: int | None = None) -> dict:
    ledger_path = ledger_path or config.LEDGER_PATH
    rows = [r for r in csv.DictReader(open(ledger_path)) if r.get("alert_ts")]
    rows.sort(key=lambda r: -float(r["alert_ts"]))      # newest first: finest resolution available
    done = _done()
    todo = [r for r in rows if r["mint"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"ledger {len(rows)} rows; already priced {len(done)}; attempting {len(todo)}",
          flush=True)
    stats = {"ok": 0, "absent": 0, "no_cover": 0, "deferred": 0}
    by_res: dict = {}
    t0 = time.time()
    with open(OUT, "a") as fh:
        for i, r in enumerate(todo, 1):
            a = float(r["alert_ts"])
            try:
                p = P.fetch_path(r["mint"], a)
            except Exception as e:                      # a bad row is data, not a crash
                print(f"  !! {r['mint'][:12]}: {type(e).__name__}: {e}", flush=True)
                stats["deferred"] += 1
                continue
            stats[p["status"]] = stats.get(p["status"], 0) + 1
            if p["status"] != "ok":
                continue                                # deferred rows stay absent -> retried
            by_res[p["res"]] = by_res.get(p["res"], 0) + 1
            fh.write(json.dumps({
                "mint": r["mint"], "symbol": r.get("symbol", ""), "tier": r.get("tier", ""),
                "alert_ts": a, "res": p["res"], "entry": p["entry"],
                "n_bars": len(p["bars"]), "bars": p["bars"]}) + "\n")
            fh.flush()
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}  {time.time()-t0:.0f}s  {stats}  res={by_res}",
                      flush=True)
    print(f"done in {time.time()-t0:.0f}s  {stats}  resolutions={by_res}", flush=True)
    print(f"  -> {OUT} now holds {len(_done())} priced rows", flush=True)
    if stats.get("deferred"):
        print(f"  {stats['deferred']} rows were RATE-LIMITED, not absent. Re-run to retry them.",
              flush=True)
    return stats


if __name__ == "__main__":
    lim = None
    src = None
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            lim = int(a.split("=")[1])
        if a.startswith("--ledger="):
            src = a.split("=")[1]
    run(src, lim)
