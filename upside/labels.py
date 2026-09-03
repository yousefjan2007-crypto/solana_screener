"""
First-passage labels for the upside classifier, from SANITIZED price paths.

Label = "hit TP_MULT before hitting SL_MULT, within HORIZON_S of the alert",
walked bar-by-bar with the PESSIMISTIC within-bar convention the exit backtester
already uses (a bar where both the TP and the SL are inside the range counts as
the SL — intrabar order is unknowable, and the optimistic reading was measured
to be worth a spurious +0.70 in the sibling project).

`res` (bar resolution actually fetched: 1m/15m/1h) is carried on every row and
every consumer must stratify by it: an hour bar can contain an entire pump and
dump, so only 1m rows resolve the ordering unambiguously.

Paths come through selfimprove/evaluate.load_paths — i.e. AFTER the 2026-09
integrity gates (zero-volume phantom bars dropped; mispriced-pool paths dropped).

Usage:  python3 upside/labels.py     # writes upside/labels.csv, prints base rates
"""
from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "selfimprove"))

import evaluate as EV   # noqa: E402  (selfimprove/evaluate.py — sanitized load_paths)

TP_MULT = 2.0
SL_MULT = 0.5
HORIZON_S = 12 * 3600
OUT = os.path.join(HERE, "labels.csv")


def first_passage(entry: float, bars: list, alert_ts: float) -> dict:
    """Pessimistic 'TP before SL within horizon' walk. Returns label + diagnostics."""
    tp_px, sl_px = entry * TP_MULT, entry * SL_MULT
    for b in bars:
        if b["ts"] - alert_ts > HORIZON_S:
            break
        hit_tp = b.get("h", 0) >= tp_px
        hit_sl = b.get("l", 0) > 0 and b["l"] <= sl_px
        if hit_tp and hit_sl:                 # both inside one bar: order unknowable
            return {"label": 0, "outcome": "ambiguous_bar"}
        if hit_sl:
            return {"label": 0, "outcome": "sl"}
        if hit_tp:
            return {"label": 1, "outcome": "tp",
                    "t_to_tp_s": round(b["ts"] - alert_ts, 1)}
    return {"label": 0, "outcome": "neither"}


def build(paths_file: str | None = None, ledger_entry: dict | None = None) -> list:
    rows = []
    for r in EV.load_paths(paths_file, ledger_entry=ledger_entry):
        fp = first_passage(r["entry"], r["bars"], r["alert_ts"])
        rows.append({"mint": r["mint"], "symbol": r.get("symbol"),
                     "tier": r.get("tier"), "alert_ts": r["alert_ts"],
                     "res": r.get("res"), "n_bars": len(r["bars"]),
                     "label": fp["label"], "outcome": fp["outcome"],
                     "t_to_tp_s": fp.get("t_to_tp_s", "")})
    return rows


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", default=None,
                    help="paths.jsonl location (default: this repo's cache/; a "
                         "worktree has no cache — point at the main checkout's)")
    args = ap.parse_args()
    rows = build(args.paths)
    if not rows:
        sys.exit("no sanitized paths — run selfimprove/backfill.py first")
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    n = len(rows)
    pos = sum(r["label"] for r in rows)
    print(f"wrote {n} labels -> {OUT}   base rate {pos}/{n} = {pos / n:.1%} "
          f"(TP {TP_MULT}x before SL {SL_MULT}x within {HORIZON_S // 3600}h, pessimistic)")
    for res in ("1m", "15m", "1h"):
        sub = [r for r in rows if r["res"] == res]
        if sub:
            p = sum(r["label"] for r in sub)
            print(f"  res={res:>3}: {p}/{len(sub)} = {p / len(sub):.1%}")


if __name__ == "__main__":
    main()
