"""
Score every pre-declared exit policy on the ledger's real price paths, with the guards.

THE DECISION CRITERION, stated before any number is computed: a policy is interesting only if its
DAY-CLUSTERED bootstrap 2.5th percentile beats `hold_to_end`'s, at >= config.MIN_BOOTSTRAP_CLUSTERS
distinct alert days, on the PESSIMISTIC within-bar reading. Means are reported for context and are
never the criterion — `~/entry_bot/CLAUDE.md` documents a rule whose mean was +0.577 and whose
clustered lower bound was -0.228 at n=10.

WHY THE CLUSTER IS THE ALERT DAY. The trade is not the independent unit. Alerts arrive in bursts
(the cloud scan fires every 15 min and 43 of 60 live-path survivors landed in a five-day window),
and every token alerted on the same day shares one market regime. An IID bootstrap over 900 rows
would treat one good afternoon as 900 independent observations. This is the same correction
`entry_bot/replay.py:_boot` applies by resampling deployers rather than trades, which moved a
bound from -0.092 to -0.126 on the same rows.

WHY RESOLUTION IS STRATIFIED. `paths.py` falls back to coarser bars for older alerts. An hour-long
bar can contain an entire pump and dump, so its pessimistic/optimistic bracket can span most of
the outcome space — the smoke test in policies.py shows a single ambiguous bar producing -0.510
vs +0.225. Pooling 1m and 1h rows without saying so would quote fake precision, so every table
here reports the resolution mix and the 1m-only subset separately.

TRIALS ACCUMULATE ACROSS RUNS. `selfimprove/trials.json` carries the total number of policies ever
scored by this module. Deflated Sharpe is computed against that cumulative count, not against
len(POLICIES), because a loop that runs weekly and proposes changes is itself a trial generator.
`signal_lab/registry.py` hardcodes `n_trials = 50` and therefore cannot see its own searching;
that is the specific failure this file is written to avoid.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# entry_bot goes at the END, never the front. Both repos have a top-level `config.py`, so
# inserting entry_bot first makes `import config` resolve to ITS config — which is the "dual
# imports" trap entry_bot/CLAUDE.md documents (two module objects for the same name). Appending
# means our config wins and entry_bot/stats.py reads OUR FDR_Q / BOOTSTRAP_REPS / SEED, which is
# exactly the intent: borrow the guards, keep our own constants.
sys.path.append("/Users/yousefjan/entry_bot")        # for stats.py only

import config          # noqa: E402
import policies as POL  # noqa: E402

PATHS = os.path.join(config.DATA_DIR, "paths.jsonl")
TRIALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trials.json")


def load_paths(path: str | None = None) -> list:
    p = path or PATHS
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("bars") and r.get("entry", 0) > 0:
                out.append(r)
    return out


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def cluster_lb(vals: np.ndarray, clusters: list, reps: int | None = None,
               seed: int | None = None) -> tuple[float, int]:
    """(2.5th percentile of the mean under a CLUSTER bootstrap, n_clusters).

    Implemented via per-cluster (sum, count) so a resample is exact and cheap: the mean of a
    cluster-resample is sum(picked sums)/sum(picked counts), no row concatenation.
    """
    reps = reps or config.BOOTSTRAP_REPS
    if vals.size == 0:
        return float("nan"), 0
    groups: dict = {}
    for v, c in zip(vals, clusters):
        groups.setdefault(c, []).append(v)
    sums = np.array([float(np.sum(g)) for g in groups.values()])
    cnts = np.array([len(g) for g in groups.values()], dtype=float)
    ng = sums.size
    if ng == 0:
        return float("nan"), 0
    rng = np.random.default_rng(config.SEED if seed is None else seed)
    parts = []
    for start in range(0, reps, 250):
        k = min(250, reps - start)
        pick = rng.integers(0, ng, size=(k, ng))
        parts.append(sums[pick].sum(axis=1) / cnts[pick].sum(axis=1))
    m = np.concatenate(parts)
    return float(np.quantile(m, config.BOOTSTRAP_ALPHA)), ng


def bump_trials(policy_names) -> int:
    """Cumulative count of DISTINCT hypotheses ever scored by this module.

    Counted per distinct policy NAME ever seen, not per run. Re-scoring the same pre-declared
    family on refreshed data is one hypothesis re-measured, not fifteen new ones — inflating the
    count every week would deflate the Sharpe toward zero and make the gate meaningless. But
    ADDING a policy is a genuinely new trial and is counted forever, including after it is later
    deleted: you cannot un-look at a result. That asymmetry is the whole point, and it is what
    `signal_lab/registry.py`'s hardcoded `n_trials = 50` cannot express.
    """
    seen = []
    if os.path.exists(TRIALS):
        try:
            seen = list(json.load(open(TRIALS)).get("policies_ever_scored", []))
        except Exception:
            seen = []
    for n in policy_names:
        if n not in seen:
            seen.append(n)
    with open(TRIALS, "w") as fh:
        json.dump({"policies_ever_scored": seen,
                   "cumulative_trials": len(seen),
                   "note": "Deflated Sharpe is deflated by cumulative_trials, not by "
                           "len(POLICIES) in the current file. Deleting a policy does NOT "
                           "decrement this — you cannot un-look at a result."}, fh, indent=2)
    return len(seen)


def score(rows: list, pessimistic: bool = True) -> dict:
    """{policy: {ret: array, days: list, res: list}} for one within-bar reading."""
    out = {}
    for name, pol in POL.POLICIES.items():
        rets, days, res = [], [], []
        for r in rows:
            v = POL.simulate(r["entry"], r["bars"], pol, pessimistic=pessimistic)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            rets.append(v)
            days.append(day_of(r["alert_ts"]))
            res.append(r.get("res"))
        out[name] = {"ret": np.array(rets, dtype=float), "days": days, "res": res}
    return out


def table(rows: list, label: str, n_trials: int) -> None:
    import stats as ST                              # entry_bot/stats.py
    if not rows:
        print(f"\n{label}: no priced rows yet")
        return
    print(f"\n{'='*104}\n{label}   n={len(rows)}   "
          f"resolution mix={dict(Counter(r.get('res') for r in rows))}   "
          f"days={len({day_of(r['alert_ts']) for r in rows})}\n{'='*104}")
    pess, opt = score(rows, True), score(rows, False)
    base_lb, _ = cluster_lb(pess["hold_to_end"]["ret"], pess["hold_to_end"]["days"])
    print(f"  {'policy':<24s} {'mean(pess)':>11s} {'mean(opt)':>10s} {'dayLB(pess)':>12s} "
          f"{'days':>5s} {'>0':>6s} {'vs hold':>9s}")
    ranked = []
    for name in POL.POLICIES:
        p, o = pess[name]["ret"], opt[name]["ret"]
        if p.size == 0:
            continue
        lb, nd = cluster_lb(p, pess[name]["days"])
        ranked.append((lb, name, p, o, nd))
    for lb, name, p, o, nd in sorted(ranked, key=lambda x: -x[0]):
        star = " *" if (lb > base_lb and nd >= config.MIN_BOOTSTRAP_CLUSTERS) else ""
        print(f"  {name:<24s} {p.mean():>+11.3f} {o.mean():>+10.3f} {lb:>+12.3f} "
              f"{nd:>5} {np.mean(p > 0):>6.1%} {lb - base_lb:>+9.3f}{star}")
    print(f"  baseline hold_to_end dayLB = {base_lb:+.3f}; "
          f"'*' = beats it with >= {config.MIN_BOOTSTRAP_CLUSTERS} day-clusters")

    # White's reality check, PAIRED. entry_bot/stats.reality_check takes {rule: row-mask} and
    # scores each rule's excess over the grand mean — correct for SELECTION rules (which rows to
    # trade), wrong here: every exit policy trades the SAME rows and differs only in P&L, so
    # all-true masks give every rule an excess of exactly zero and a trivial p=1.0000. The first
    # version of this file did exactly that. The right statistic is each policy's mean ADVANTAGE
    # over hold_to_end on the same rows: resample rows in circular blocks (alerts arrive in
    # bursts), recentre by the observed advantage to impose the null, and take the max across
    # policies within each resample, so the p-value already pays for having searched all of them.
    best = max(ranked, key=lambda x: x[0])
    base = pess["hold_to_end"]["ret"]
    names = [n for _, n, _, _, _ in ranked if n != "hold_to_end"]
    if names and base.size >= 30:
        A = np.array([pess[n]["ret"] - base for n in names])
        obs = A.mean(axis=1)
        nn = base.size
        block = max(5, min(50, nn // 20))
        rng = np.random.default_rng(config.SEED + 3)
        nblk = int(np.ceil(nn / block))
        null_max = np.empty(config.BOOTSTRAP_REPS)
        for i in range(config.BOOTSTRAP_REPS):
            starts = rng.integers(0, nn, size=nblk)
            idx = np.concatenate([(np.arange(s, s + block) % nn) for s in starts])[:nn]
            null_max[i] = float(np.max(A[:, idx].mean(axis=1) - obs))
        p_rc = float((null_max >= obs.max()).mean())
        print(f"  PAIRED reality check (advantage over hold_to_end, block={block}): best "
              f"{names[int(np.argmax(obs))]!r} +{obs.max():.3f}, p={p_rc:.4f} over "
              f"{len(names)} policies")
    else:
        print("  paired reality check skipped (need >= 30 priced rows)")
    dsr = ST.deflated_sharpe_ratio(best[2].tolist(), n_trials)
    print(f"  deflated Sharpe of {best[1]!r} at {n_trials} CUMULATIVE trials: {dsr:.3f} "
          f"(signal_lab promotes at >= 0.95)")


def main() -> None:
    rows = load_paths()
    if not rows:
        print(f"no paths yet — run `python3 selfimprove/backfill.py` first ({PATHS})")
        return
    n_trials = bump_trials(list(POL.POLICIES))
    print(f"priced rows: {len(rows)}   policies this run: {len(POL.POLICIES)}   "
          f"cumulative trials: {n_trials}")
    table(rows, "ALL priced alerts", n_trials)
    fine = [r for r in rows if r.get("res") == "1m"]
    if fine:
        table(fine, "1m-resolution subset (tightest bracket)", n_trials)
    a = [r for r in rows if (r.get("tier") or "") == "A"]
    if len(a) >= 5:
        table(a, "tier A only (the alerted band)", n_trials)


if __name__ == "__main__":
    main()
