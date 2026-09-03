"""
Round-2 features: PRE-alert momentum from the cached GeckoTerminal blobs, plus
trench-regime windows from the mined scan series. Both are point-in-time by
construction: momentum uses only bars whose timestamp is STRICTLY before the
alert, and regime windows end at the alert's own scan.

Zero network: this reads the raw cache/paths/ blobs directly (never through
http_client, whose 7-day max_age would silently refetch stale blobs at 0.25 Hz).
Pool selection replicates selfimprove/paths.top_pool: deepest-reserve pool whose
BASE leg is the mint — quote-leg pools price the wrong token.

Usage:
  python3 upside/features2.py --cache ~/solana_screener/cache/paths
      # reads regime.csv + the ledger, writes upside/features2.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import ledger  # noqa: E402

# (blob suffix, seconds per bar), finest first
RESES = [("minute1", 60), ("minute15", 900), ("hour1", 3600)]
MIN_PRE_BARS = 5


def top_pool_from_blob(cache_dir: str, mint: str) -> str | None:
    fp = os.path.join(cache_dir, f"pools_{mint}.json")
    if not os.path.exists(fp):
        return None
    try:
        data = (json.load(open(fp)) or {}).get("data") or []
    except Exception:
        return None
    best, best_res = None, -1.0
    for p in data:
        a = p.get("attributes") or {}
        rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        if not str(rel.get("id", "")).endswith(mint):
            continue
        try:
            r = float(a.get("reserve_in_usd") or 0.0)
        except (TypeError, ValueError):
            r = 0.0
        if r > best_res:
            best, best_res = a.get("address"), r
    return best


def bars_from_blob(cache_dir: str, pool: str, suffix: str) -> list:
    fp = os.path.join(cache_dir, f"ohlcv_{pool}_{suffix}.json")
    if not os.path.exists(fp):
        return []
    try:
        lst = ((json.load(open(fp)) or {}).get("data") or {}) \
            .get("attributes", {}).get("ohlcv_list") or []
    except Exception:
        return []
    out = []
    for row in lst:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            out.append({"ts": float(row[0]), "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]),
                        "v": float(row[5]) if len(row) > 5 else None})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["ts"])
    return out


def _win(pre: list, sec_per_bar: int, seconds: float) -> list:
    n = max(1, int(round(seconds / sec_per_bar)))
    return pre[-n:]


def momentum(pre: list, sec_per_bar: int) -> dict:
    """Features from the pre-alert bars (last bar = latest info before the alert)."""
    last_c = pre[-1]["c"]
    f: dict = {"pre_bars": len(pre), "pre_res_s": sec_per_bar}
    if last_c <= 0:
        return f
    for name, sec in (("r_5m", 300), ("r_15m", 900), ("r_30m", 1800), ("r_60m", 3600)):
        if sec < sec_per_bar:                      # window finer than the bar: unknowable
            f[name] = float("nan")
            continue
        w = _win(pre, sec_per_bar, sec)
        f[name] = last_c / w[0]["o"] - 1.0 if w[0]["o"] > 0 else float("nan")
    w30 = _win(pre, sec_per_bar, 1800)
    hi, lo = max(b["h"] for b in w30), min(b["l"] for b in w30 if b["l"] > 0) \
        if any(b["l"] > 0 for b in w30) else (0, 0)
    f["range_30m"] = (hi - lo) / last_c if lo and lo > 0 else float("nan")
    v_recent = sum(b["v"] or 0.0 for b in _win(pre, sec_per_bar, 900))
    v_prior = sum(b["v"] or 0.0 for b in pre[:-max(1, int(round(900 / sec_per_bar)))]
                  [-max(1, int(round(2700 / sec_per_bar))):])
    f["vol_accel"] = v_recent / v_prior if v_prior > 0 else float("nan")
    f["above_first"] = last_c / pre[0]["o"] - 1.0 if pre[0]["o"] > 0 else float("nan")
    return f


def regime_features(scans: list, alert_ts: float) -> dict:
    """Trailing trench-activity windows ending at the alert (scan counts are computed
    before the same run alerts, so <= alert_ts is point-in-time)."""
    past = [s for s in scans if s["scan_ts"] <= alert_ts]
    def w(sec):
        return [s for s in past if alert_ts - s["scan_ts"] <= sec]
    d6 = sum(s["discovered"] for s in w(6 * 3600))
    d24 = sum(s["discovered"] for s in w(24 * 3600))
    return {"disc_6h": d6, "disc_24h": d24,
            "surv_24h": sum(s["n_survivors"] for s in w(24 * 3600)),
            "disc_accel": (d6 / (d24 - d6)) if (d24 - d6) > 0 else float("nan"),
            "regime_scans_24h": len(w(24 * 3600))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/solana_screener/cache/paths"))
    ap.add_argument("--out", default=os.path.join(HERE, "features2.csv"))
    args = ap.parse_args()

    scans = []
    with open(os.path.join(HERE, "regime.csv")) as fh:
        for r in csv.DictReader(fh):
            scans.append({"scan_ts": float(r["scan_ts"]),
                          "discovered": int(float(r["discovered"] or 0)),
                          "n_survivors": int(float(r["n_survivors"] or 0))})
    scans.sort(key=lambda s: s["scan_ts"])

    led = ledger.load(os.path.join(REPO, "data", "ledger.csv"))
    rows, n_mom = [], 0
    for _, r in led.iterrows():
        mint = str(r["mint"])
        alert_ts = ledger._num(r["alert_ts"])
        if alert_ts <= 0:
            continue
        row = {"mint": mint}
        row.update(regime_features(scans, alert_ts))
        pool = top_pool_from_blob(args.cache, mint)
        got = None
        if pool:
            for suffix, spb in RESES:            # finest res with enough pre-alert bars
                pre = [b for b in bars_from_blob(args.cache, pool, suffix)
                       if b["ts"] < alert_ts]
                if len(pre) >= MIN_PRE_BARS:
                    got = momentum(pre[-120:], spb)
                    break
        if got:
            row.update(got)
            n_mom += 1
        rows.append(row)

    cols = ["mint", "disc_6h", "disc_24h", "surv_24h", "disc_accel",
            "regime_scans_24h", "pre_bars", "pre_res_s", "r_5m", "r_15m", "r_30m",
            "r_60m", "range_30m", "vol_accel", "above_first"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {args.out}  (pre-alert momentum for {n_mom}; "
          f"the rest carry regime features + NaN momentum)")


if __name__ == "__main__":
    main()
