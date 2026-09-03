"""
Point-in-time feature miner for the upside classifier.

THE ONLY HONEST FEATURE STORE IS GIT HISTORY. The API caches under cache/ are
TTL-overwritten (2-10 min), cover ~22% of ledger mints, and their mtimes sit hours from
the alert instant — using them would be quiet look-ahead (verified, 2026-09 audit). But
every committing run overwrites data/latest_scan.json and commits it, and run.py uses
ONE timestamp as both scan_ts and alert_ts — so the FIRST commit in which a mint appears
as a survivor is, byte for byte, what the screener knew at the alert instant (measured:
100% of ledger mints within 1 minute; 99.4% coverage; the 9 missing rows predate the
first snapshot commit).

Later commits re-emit the same mint while it keeps surviving scans. Those rows are
POST-alert observations: taking anything but the first occurrence is look-ahead.

Usage:  python3 upside/mine_features.py            # writes upside/features.csv
        python3 upside/mine_features.py --repo X   # mine a different clone/worktree

Read-only with respect to the repo (git log/show only); deterministic.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "features.csv")

# Everything present at alert time in a survivor row. `gates` is deliberately excluded:
# all 21 booleans are True on 100% of ledger rows by construction (only gate-passers are
# ledgered) — zero signal, verified. `dex` is kept as a categorical.
NUMERIC = ["price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6", "vol_h24",
           "buys_h1", "sells_h1", "price_chg_h1", "pair_age_min", "score",
           "total_holders", "top10_pct", "insider_networks_pct", "graph_insiders",
           "creator_prior_tokens", "rugcheck_score",
           "gmgn_bundler_wallets", "gmgn_sniper_wallets", "gmgn_smart_wallets"]
META = ["mint", "symbol", "tier", "dex"]


def _git(args: list, repo: str) -> str:
    return subprocess.run(["git", "-C", repo] + args, capture_output=True,
                          text=True, check=True).stdout


def commits_touching(repo: str, path: str = "data/latest_scan.json") -> list:
    """(sha) list, OLDEST first, of commits that touched the scan snapshot."""
    out = _git(["log", "--reverse", "--format=%H", "--", path], repo)
    return [l for l in out.splitlines() if l.strip()]


def mine(repo: str) -> list:
    rows: dict = {}
    shas = commits_touching(repo)
    print(f"{len(shas)} commits of data/latest_scan.json")
    n_bad = 0
    for k, sha in enumerate(shas, 1):
        try:
            blob = _git(["show", f"{sha}:data/latest_scan.json"], repo)
            scan = json.loads(blob)
        except Exception:
            n_bad += 1
            continue
        scan_ts = scan.get("scan_ts")
        for s in scan.get("survivors") or []:
            mint = s.get("mint")
            if not mint or mint in rows:        # FIRST occurrence only — later = post-alert
                continue
            r = {"mint": mint, "scan_ts": scan_ts, "sha": sha}
            for c in META[1:]:
                r[c] = s.get(c)
            for c in NUMERIC:
                r[c] = s.get(c)
            r["n_hc_misses"] = len(s.get("hc_misses") or [])
            r["gmgn_ok"] = bool(s.get("gmgn_ok"))
            rows[mint] = r
        if k % 200 == 0:
            print(f"  {k}/{len(shas)}  mints so far: {len(rows)}")
    if n_bad:
        print(f"  {n_bad} unparseable blobs skipped")
    return list(rows.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    rows = mine(args.repo)
    if not rows:
        sys.exit("no rows mined — is this a clone with data/latest_scan.json history?")
    cols = (["mint", "scan_ts", "sha", "symbol", "tier", "dex", "gmgn_ok",
             "n_hc_misses"] + NUMERIC)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x["scan_ts"] or 0, x["mint"])):
            w.writerow(r)
    tiers = {}
    for r in rows:
        tiers[r.get("tier")] = tiers.get(r.get("tier"), 0) + 1
    print(f"wrote {len(rows)} first-occurrence rows -> {args.out}  (tiers: {tiers})")


if __name__ == "__main__":
    main()
