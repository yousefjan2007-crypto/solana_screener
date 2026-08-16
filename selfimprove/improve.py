"""
The self-improvement loop: re-score every policy on LIVE evidence, propose, never auto-apply.

WHAT "SELF-IMPROVING" CAN HONESTLY MEAN HERE, because the phrase invites the exact failure this
workspace exists to avoid. It does NOT mean an agent that searches harder until it finds alpha —
on the sibling screener's ledger, 0 of 1,089 bucket rules survived multiple-testing correction at
any q. It means a loop that:

  1. accumulates genuinely OUT-OF-SAMPLE evidence 24/7 (selfimprove/livebook.py),
  2. re-scores a PRE-DECLARED family on it,
  3. proposes a champion change only through a gate that prices in every hypothesis ever tried,
  4. and writes a proposal a human reads — it never edits config.

The improvement is in the ESTIMATE, and in the system's willingness to be proven wrong. That is
the only kind that compounds instead of overfitting.

=========================== THE FAILURE MODE THIS FILE IS BUILT AGAINST ===========================
A loop that re-scores 15 correlated policies every week and adopts whichever currently leads is
running a maximum over correlated series. Under a PURE NULL that maximum drifts upward forever,
and the loop will confidently converge on noise. Three guards, and none is optional:

  * The champion changes only on a PAIRED test, never on a leaderboard position. Every policy
    trades the same entries by construction (livebook takes ONE shared buy fill), so
    challenger-minus-champion is a paired difference with far lower variance than either series
    — that pairing is the live book's real statistical advantage and it is what makes a decision
    possible at n in the hundreds rather than the tens of thousands.
  * `n_leads` is reported for every policy precisely so a lead can be seen for what it is. A
    policy that has led 6 weeks running out of noise looks identical to one that leads for a
    reason; only the paired bound separates them.
  * Trials accumulate FOREVER via selfimprove/trials.json, including for policies later deleted.
    You cannot un-look at a result. signal_lab/registry.py hardcodes n_trials = 50 and therefore
    cannot see its own searching; that is the specific bug this file refuses to inherit.
==================================================================================================

THE GATE IS DELIBERATELY HARDER THAN "BEATS THE CHAMPION". A policy that merely loses less than
hold_to_end does not justify automating anything — the honest prior is that this whole class of
trade is -EV, and the backtest agrees (best policy -0.111, nothing above zero). So promotion also
requires the challenger to be profitable ON ITS OWN. The loop is allowed to promote; it is simply
not allowed to promote something that loses money slowly.

    python3 selfimprove/improve.py              # score + write a proposal
    python3 selfimprove/improve.py --quiet      # same, no stdout (for launchd)
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append("/Users/yousefjan/entry_bot")          # stats.py only

import config                       # noqa: E402
import livebook as LB               # noqa: E402
import policies as POL              # noqa: E402
from evaluate import bump_trials    # noqa: E402

CHAMPION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "champion.json")
PROPOSAL_DIR = os.path.join(config.DATA_DIR, "proposals")
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "improve_history.jsonl")

# The system's behaviour today: it alerts and never exits. Anything that replaces it must beat
# it on a paired test, not merely outrank it on a table.
DEFAULT_CHAMPION = "hold_to_end"

MIN_POSITIONS = 300       # completed live positions before any promotion is even considered
# 12 clusters was measured to give 6.3% actual coverage against a nominal 2.5% on returns this
# skewed (and 33.3% once a maximum over the family is taken). config.MIN_BOOTSTRAP_CLUSTERS stays
# 12 as the floor below which nothing may even be PRINTED; promotion needs far more.
MIN_DAYS = config.MIN_BOOTSTRAP_CLUSTERS      # print floor
PROMOTE_MIN_CLUSTERS = 40                     # promotion floor, from the coverage measurement
DSR_GATE = 0.95           # signal_lab's promote gate, reused deliberately


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def live_returns() -> tuple[dict, list, list]:
    """{policy: array of per-position returns}, aligned alert-days, aligned mints.

    Only COMPLETED positions count. An open position has no return yet, and marking it at the
    current price would let a policy's score drift with the market between runs.
    """
    book = LB._load(LB.BOOK_PATH, {})
    done = [p for p in book.values() if p.get("done") and p.get("cost_usd", 0) > 0]
    done.sort(key=lambda p: p["opened_ts"])
    days = [_day(p["opened_ts"]) for p in done]
    mints = [p["mint"] for p in done]
    out = {}
    for name in POL.POLICIES:
        rets = []
        for p in done:
            st = p["policies"].get(name)
            rets.append(st["realized_usd"] / p["cost_usd"] - 1.0 if st else np.nan)
        out[name] = np.array(rets, dtype=float)
    return out, days, mints


def cluster_boot(vals: np.ndarray, clusters: list, reps: int | None = None,
                 seed: int | None = None) -> tuple[float, int]:
    """Day-clustered bootstrap 2.5th percentile of the mean, and the cluster count."""
    reps = reps or config.BOOTSTRAP_REPS
    ok = ~np.isnan(vals)
    vals, clusters = vals[ok], [c for c, k in zip(clusters, ok) if k]
    if vals.size == 0:
        return float("nan"), 0
    g = {}
    for v, c in zip(vals, clusters):
        g.setdefault(c, []).append(v)
    s = np.array([float(np.sum(x)) for x in g.values()])
    n = np.array([len(x) for x in g.values()], dtype=float)
    rng = np.random.default_rng(config.SEED if seed is None else seed)
    parts = []
    for st in range(0, reps, 250):
        k = min(250, reps - st)
        pick = rng.integers(0, s.size, size=(k, s.size))
        parts.append(s[pick].sum(axis=1) / n[pick].sum(axis=1))
    return float(np.quantile(np.concatenate(parts), config.BOOTSTRAP_ALPHA)), int(s.size)


def paired_lb(challenger: np.ndarray, champion: np.ndarray, days: list,
              seed: int | None = None) -> float:
    """Day-clustered lower bound on the PAIRED difference (challenger - champion).

    Paired because both policies traded the same entries at the same instants, so the
    position-level difference removes all the token-selection variance the two series share.
    This is the whole reason a live book can decide anything at n in the hundreds.
    """
    d = challenger - champion
    lb, _ = cluster_boot(d, days, seed=(config.SEED + 17 if seed is None else seed))
    return lb


def load_champion() -> str:
    if os.path.exists(CHAMPION_PATH):
        try:
            return json.load(open(CHAMPION_PATH)).get("champion", DEFAULT_CHAMPION)
        except Exception:
            pass
    return DEFAULT_CHAMPION


def evaluate_all() -> dict:
    rets, days, mints = live_returns()
    champ = load_champion()
    n_trials = bump_trials(list(POL.POLICIES))
    res = {"ts": time.time(), "champion": champ, "n_positions": len(mints),
           "n_days": len(set(days)), "n_trials": n_trials, "policies": {}}
    if not mints:
        return res
    base = rets.get(champ)
    import stats as ST                                   # entry_bot/stats.py
    if base is not None:
        res["champ_lb"] = cluster_boot(base, days)[0]
    res["forward_only"] = False      # set True only by the forward-only promotion path
    # Score the negative controls on the SAME rows. If one of these clears the gate, the gate is
    # measuring its own machinery and decide() refuses to emit anything. They are scored but are
    # NOT eligible to be promoted, which is why they live in POL.CONTROLS rather than POL.POLICIES.
    res["controls"] = {}
    book = LB._load(LB.BOOK_PATH, {})
    done = sorted([p for p in book.values() if p.get("done") and p.get("cost_usd", 0) > 0],
                  key=lambda p: p["opened_ts"])
    for cname, cpol in getattr(POL, "CONTROLS", {}).items():
        cr = []
        for p in done:
            st = p["policies"].get(cname)
            cr.append(st["realized_usd"] / p["cost_usd"] - 1.0 if st else np.nan)
        cr = np.array(cr, dtype=float)
        if cr.size and not np.all(np.isnan(cr)):
            clb, cnd = cluster_boot(cr, days)
            res["controls"][cname] = {"n": int(np.sum(~np.isnan(cr))),
                                      "mean": float(np.nanmean(cr)), "day_lb": clb,
                                      "n_days": cnd}
    for name, r in rets.items():
        lb, nd = cluster_boot(r, days)
        row = {"n": int(np.sum(~np.isnan(r))), "mean": float(np.nanmean(r)),
               "median": float(np.nanmedian(r)),
               "win_rate": float(np.nanmean(r > 0)), "day_lb": lb, "n_days": nd}
        if base is not None and name != champ:
            row["paired_mean"] = float(np.nanmean(r - base))
            row["paired_lb"] = paired_lb(r, base, days)
        try:
            # DAY MEANS, not rows. entry_bot/stats.py scales the z-score by sqrt(n-1), so
            # feeding 639 rows where the honest cluster count is 41 days inflates the statistic
            # ~4x. Measured false-pass rate of DSR >= 0.95 under a zero-mean day-clustered null:
            # 17.0% computed IID over rows, 0.0% computed over day means.
            dm = {}
            for v, d in zip(r, days):
                if not np.isnan(v):
                    dm.setdefault(d, []).append(v)
            means = [float(np.mean(v)) for v in dm.values()]
            row["dsr"] = float(ST.deflated_sharpe_ratio(means, n_trials))
        except Exception:
            row["dsr"] = float("nan")
        res["policies"][name] = row
    return res


def decide(res: dict) -> dict:
    """Apply the gate. Returns {promote, winner, reasons} — advisory only, never applied.

    REBUILT 2026-08-15, because the first version could not fail. It picked the challenger with
    the highest paired lower bound and then tested whether that bound exceeded zero — a maximum
    over a correlated family, tested as though it were a single hypothesis. Measured on the real
    639 rows, with every policy's advantage recentred to exactly zero mean (preserving the 0.561
    cross-policy correlation, the day structure and the skew, removing only the edge): that
    procedure crosses zero **33.3% of the time at 12 alert-days and 20.0% at 41**. More data does
    not fix it — it is a selection bias, not a variance problem. A single PRE-PICKED policy's
    nominal 2.5% bound also really crosses 6.3% of the time at 12 clusters, so
    config.MIN_BOOTSTRAP_CLUSTERS = 12 is too lenient for returns this skewed.

    Three changes, and the third is the one that matters:

    1. THE BASELINE IS THE BEST NEGATIVE CONTROL, NOT hold_to_end. Measured: buying and selling
       in the same instant (`ctl_exit_immediately`, pure round-trip cost and zero information)
       scores a day-clustered LB of -0.117 against hold_to_end's -0.824, and exiting at a
       uniformly RANDOM bar scores -0.649. Both clear a gate set at hold_to_end. So "every exit
       beats holding, p=0.0000" was never evidence that any exit is skilful — it is evidence
       that holding to zero is catastrophic and anything else beats it. A policy earns nothing
       until it beats not-holding.
    2. IF ANY CONTROL PASSES, NO PROPOSAL IS EMITTED AT ALL. A control clearing the gate is proof
       the apparatus is measuring itself, and no number from that run may be quoted.
    3. PROMOTION IS FORWARD-ONLY. The winner is NOMINATED on today's data (free — a nomination is
       not a claim) and tested only on positions whose `alert_seq` exceeds the one recorded at
       nomination. That converts a family-wise maximum into one pre-registered hypothesis, which
       is the only version of this the arithmetic supports.
    """
    champ = res["champion"]
    reasons, best, best_lb = [], None, -1e9
    ctl = res.get("controls", {})

    # (2) — the apparatus check comes first and is disqualifying
    passing_ctl = [n for n, r in ctl.items() if r.get("day_lb", -9) > res.get("champ_lb", -9)]
    if passing_ctl:
        return {"promote": False, "winner": None, "gate_broken": True,
                "reasons": [f"NEGATIVE CONTROL CLEARED THE GATE: {', '.join(passing_ctl)}. "
                            f"The gate is measuring its own machinery; no number from this run "
                            f"may be quoted."]}

    if res["n_positions"] < MIN_POSITIONS:
        reasons.append(f"only {res['n_positions']} completed positions (needs {MIN_POSITIONS})")
    if res["n_days"] < PROMOTE_MIN_CLUSTERS:
        reasons.append(f"only {res['n_days']} alert-days (needs {PROMOTE_MIN_CLUSTERS}; the "
                       f"12-cluster floor measured 6.3% actual coverage against a nominal 2.5%)")
    if not res.get("forward_only"):
        reasons.append("scored on rows that already existed when the policy was named — this is "
                       "a RANKING, not a test. Nominate first, then test forward-only.")

    # (1) — the bar is the best control, not the champion
    ctl_lb = max([r.get("day_lb", -1e9) for r in ctl.values()] or [-1e9])
    ctl_name = max(ctl, key=lambda n: ctl[n].get("day_lb", -1e9)) if ctl else None
    for name, row in res["policies"].items():
        if name == champ or "paired_lb" not in row:
            continue
        if row["paired_lb"] > best_lb:
            best, best_lb = name, row["paired_lb"]
    if best is None:
        reasons.append("no challenger scored")
        return {"promote": False, "winner": None, "reasons": reasons}
    row = res["policies"][best]
    checks = [
        ("beats the champion on a PAIRED day-clustered bound", row["paired_lb"] > 0,
         f"paired LB {row['paired_lb']:+.3f}"),
        (f"beats the best NEGATIVE CONTROL ({ctl_name}) on its own bound",
         row["day_lb"] > ctl_lb, f"own LB {row['day_lb']:+.3f} vs control {ctl_lb:+.3f}"),
        ("is profitable on its OWN day-clustered bound", row["day_lb"] > 0,
         f"own LB {row['day_lb']:+.3f}"),
        (f"deflated Sharpe >= {DSR_GATE} on DAY MEANS at {res['n_trials']} cumulative trials",
         row.get("dsr", float("nan")) >= DSR_GATE, f"DSR {row.get('dsr', float('nan')):.3f}"),
        (f">= {PROMOTE_MIN_CLUSTERS} alert-day clusters", row["n_days"] >= PROMOTE_MIN_CLUSTERS,
         f"{row['n_days']} days"),
        (f">= {MIN_POSITIONS} completed positions", res["n_positions"] >= MIN_POSITIONS,
         f"{res['n_positions']} positions"),
        ("tested FORWARD-ONLY on rows postdating its nomination",
         bool(res.get("forward_only")), "nomination/test split"),
    ]
    failed = [f"{lbl} — {detail}" for lbl, ok, detail in checks if not ok]
    return {"promote": not failed, "winner": best, "checks": checks,
            "reasons": reasons + failed}


def write_proposal(res: dict, verdict: dict) -> str:
    os.makedirs(PROPOSAL_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(res["ts"]))
    path = os.path.join(PROPOSAL_DIR, f"proposal-{stamp}.md")
    champ = res["champion"]
    lines = [f"# livebook proposal — {stamp} UTC", "",
             f"- champion: `{champ}`",
             f"- completed live positions: **{res['n_positions']}** across "
             f"**{res['n_days']}** alert-days",
             f"- cumulative distinct hypotheses ever scored: **{res['n_trials']}**", ""]
    if not res["policies"]:
        lines += ["No completed positions yet. The book is still filling.", ""]
    else:
        lines += ["| policy | n | mean | day LB | paired vs champ | paired LB | DSR |",
                  "|---|---|---|---|---|---|---|"]
        order = sorted(res["policies"].items(),
                       key=lambda kv: -(kv[1].get("paired_lb", -1e9)))
        for name, r in order:
            mark = " **(champion)**" if name == champ else ""
            lines.append(
                f"| `{name}`{mark} | {r['n']} | {r['mean']:+.3f} | {r['day_lb']:+.3f} | "
                f"{r.get('paired_mean', float('nan')):+.3f} | "
                f"{r.get('paired_lb', float('nan')):+.3f} | {r.get('dsr', float('nan')):.3f} |")
        lines.append("")
    if verdict["promote"]:
        lines += [f"## PROMOTE → `{verdict['winner']}`", "",
                  "Every gate passed:", ""]
        for lbl, ok, detail in verdict.get("checks", []):
            lines.append(f"- [x] {lbl} ({detail})")
        lines += ["", "This is a PROPOSAL. Nothing has been changed. Edit `champion.json` "
                      "and the live config by hand if you accept it."]
    else:
        lines += ["## NO CHANGE", "",
                  f"Best challenger by paired bound: "
                  f"`{verdict.get('winner')}`" if verdict.get("winner") else "No challenger.",
                  "", "Blocking:", ""]
        for r in verdict["reasons"]:
            lines.append(f"- {r}")
    lines += ["", "---", "",
              "Generated by `selfimprove/improve.py`. It proposes; it never edits config. "
              "Trials accumulate forever in `selfimprove/trials.json`, including for policies "
              "later deleted — you cannot un-look at a result."]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def main() -> None:
    quiet = "--quiet" in sys.argv
    res = evaluate_all()
    verdict = decide(res)
    path = write_proposal(res, verdict)
    with open(HISTORY_PATH, "a") as fh:
        fh.write(json.dumps({"ts": res["ts"], "n": res["n_positions"],
                             "days": res["n_days"], "champion": res["champion"],
                             "winner": verdict.get("winner"),
                             "promote": verdict["promote"]}) + "\n")
    if quiet:
        return
    print(f"champion `{res['champion']}`  |  {res['n_positions']} completed positions across "
          f"{res['n_days']} alert-days  |  {res['n_trials']} cumulative trials")
    if res["policies"]:
        print(f"\n  {'policy':<24s} {'n':>4s} {'mean':>8s} {'day LB':>8s} "
              f"{'vs champ':>9s} {'paired LB':>10s} {'DSR':>6s}")
        for name, r in sorted(res["policies"].items(),
                              key=lambda kv: -(kv[1].get("paired_lb", -1e9))):
            mark = "*" if name == res["champion"] else " "
            print(f" {mark}{name:<23s} {r['n']:>4} {r['mean']:>+8.3f} {r['day_lb']:>+8.3f} "
                  f"{r.get('paired_mean', float('nan')):>+9.3f} "
                  f"{r.get('paired_lb', float('nan')):>+10.3f} "
                  f"{r.get('dsr', float('nan')):>6.3f}")
    if verdict["promote"]:
        print(f"\n  PROMOTE -> {verdict['winner']}  (all gates passed)")
    else:
        print("\n  NO CHANGE. blocking:")
        for r in verdict["reasons"]:
            print(f"    - {r}")
    # A lead is not evidence. Show how often each policy has topped the table across runs, so a
    # six-week winning streak out of pure noise is visible as such rather than persuasive.
    if os.path.exists(HISTORY_PATH):
        hist = [json.loads(l) for l in open(HISTORY_PATH) if l.strip()]
        leads = Counter(h.get("winner") for h in hist if h.get("winner"))
        if leads:
            print(f"\n  times led across {len(hist)} runs: "
                  f"{dict(leads.most_common(5))}  (leading is not evidence — only the "
                  f"paired bound is)")
    print(f"\n  proposal written: {path}")


if __name__ == "__main__":
    main()
