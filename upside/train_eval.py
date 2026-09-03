"""
The upside-classifier experiment: does ANYTHING known at alert time predict
"2x before -50% within 12h"?

Pre-registered, single shot (multiple-testing honesty):
  - ONE model: impute-median -> standardize -> elastic-net logistic regression
    (mirrors signal_lab/models.py's small-n branch; n~1.4k is far too small for
    the gradient-boosting branch's min_samples_leaf=200).
  - ONE feature set: every alert-time survivor field + pre-declared ratios.
  - ONE label: upside/labels.py (pessimistic first-passage, sanitized paths).
  - TWO pre-declared operating points: top-decile and top-quintile of predicted
    probability, judged against the 33% breakeven hit rate (the rate at which a
    2.0x/0.5x bracket's gross EV crosses zero) and the pooled base rate.
Anything else that gets computed is EXPLORATORY and labeled so in the report.

CV: signal_lab's PurgedWalkForwardCV (label-window purge + embargo), date level =
alert DAY (alerts cluster by scan; the honest cluster is the day), t1 = alert+12h.
Uncertainty on the operating points: day-clustered bootstrap over pooled
out-of-sample predictions. Deterministic: seeded rng, no wall-clock.

Usage:  python3 upside/train_eval.py     # reads features.csv + labels.csv,
                                         # writes upside/report.md
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# signal_lab FIRST and the screener root NOT on the path: both projects use a bare
# `import config`, and cv.py must resolve signal_lab's own config (module-name clash
# found the hard way — the screener's config has no CV_* constants).
sys.path.insert(0, os.path.expanduser("~/signal_lab"))

from cv import PurgedWalkForwardCV  # noqa: E402  (the leakage firewall)

from sklearn.impute import SimpleImputer          # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score         # noqa: E402
from sklearn.pipeline import Pipeline             # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

SEED = 42                 # == screener config.SEED (not imported: module-name clash)
BREAKEVEN = 1.0 / 3.0     # P(2x first) where a 2.0x/0.5x bracket's gross EV = 0
HORIZON_S = 12 * 3600
RAW = ["price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6", "vol_h24",
       "buys_h1", "sells_h1", "price_chg_h1", "pair_age_min", "score",
       "total_holders", "top10_pct", "insider_networks_pct", "graph_insiders",
       "creator_prior_tokens", "rugcheck_score",
       "gmgn_bundler_wallets", "gmgn_sniper_wallets", "gmgn_smart_wallets",
       "n_hc_misses"]


def build_xy() -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series, pd.Series]:
    f = pd.read_csv(os.path.join(HERE, "features.csv"))
    l = pd.read_csv(os.path.join(HERE, "labels.csv"))
    df = f.merge(l[["mint", "alert_ts", "res", "label"]], on="mint", how="inner")
    df = df.sort_values("alert_ts").reset_index(drop=True)

    X = df[RAW].apply(pd.to_numeric, errors="coerce").copy()
    # pre-declared ratios (guarded denominators; NaN is fine — the imputer owns it)
    def _r(a, b):
        return np.where(X[b] > 0, X[a] / X[b], np.nan)
    X["vol1_liq"] = _r("vol_h1", "liq_usd")
    X["vol1_vol6"] = _r("vol_h1", "vol_h6")
    X["vol6_vol24"] = _r("vol_h6", "vol_h24")
    X["liq_mcap"] = _r("liq_usd", "mcap")
    X["mcap_fdv"] = _r("mcap", "fdv")
    X["holders_per_min"] = _r("total_holders", "pair_age_min")
    tx = X["buys_h1"] + X["sells_h1"]
    X["tx_per_holder"] = np.where(X["total_holders"] > 0,
                                  tx / X["total_holders"], np.nan)
    X["buy_ratio"] = np.where(tx > 0, X["buys_h1"] / tx, np.nan)
    X["gmgn_missing"] = (~df["gmgn_ok"].astype(bool)).astype(float)
    for d in ("pumpswap", "raydium"):          # venue, few categories
        X[f"dex_{d}"] = (df["dex"].astype(str) == d).astype(float)
    # heavy-tailed sizes -> logs (pre-declared, standard)
    for c in ("price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6", "vol_h24",
              "total_holders"):
        X[c] = np.log10(np.clip(X[c].astype(float), 1e-12, None))

    dates = pd.to_datetime(df["alert_ts"], unit="s").dt.floor("D")
    X.index = pd.MultiIndex.from_arrays([dates, df["mint"]], names=["date", "symbol"])
    y = pd.Series(df["label"].values, index=X.index)
    t1 = pd.Series(pd.to_datetime(df["alert_ts"] + HORIZON_S, unit="s").values,
                   index=X.index)
    res = pd.Series(df["res"].values, index=X.index)
    day = pd.Series(dates.values, index=X.index)
    return X, y, t1, res, day


def estimator() -> Pipeline:
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(penalty="elasticnet", solver="saga",
                                   l1_ratio=0.5, C=0.5, max_iter=4000, tol=1e-4,
                                   random_state=SEED)),
    ])


def day_boot_precision(hit: np.ndarray, days: np.ndarray, reps: int = 4000,
                       seed: int = SEED) -> tuple[float, float]:
    """(p05, p95) of the mean hit rate under a day-clustered bootstrap."""
    ud = pd.unique(days)
    sums = np.array([hit[days == d].sum() for d in ud], dtype=float)
    cnts = np.array([(days == d).sum() for d in ud], dtype=float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(ud), size=(reps, len(ud)))
    means = sums[pick].sum(axis=1) / cnts[pick].sum(axis=1)
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def main() -> None:
    X, y, t1, res, day = build_xy()
    n, base = len(y), float(y.mean())
    print(f"n={n}  base rate={base:.1%}  days={day.nunique()}")

    cv = PurgedWalkForwardCV(t1=t1, n_splits=5, test_size=7,
                             embargo_frac=0.02, min_train_size=21)
    oos = pd.Series(np.nan, index=X.index)
    fold_id = pd.Series(-1, index=X.index)
    fold_lines = []
    for k, (tr, te) in enumerate(cv.split(X)):
        tr_d, te_d = day.iloc[tr], day.iloc[te]
        assert tr_d.max() < te_d.min(), "fold not strictly forward"   # hard guarantee
        if y.iloc[tr].nunique() < 2 or y.iloc[te].nunique() < 2:
            continue
        est = estimator().fit(X.iloc[tr], y.iloc[tr])
        p = est.predict_proba(X.iloc[te])[:, 1]
        oos.iloc[te] = p
        fold_id.iloc[te] = k
        auc = roc_auc_score(y.iloc[te], p)
        fold_lines.append(
            f"| {k} | {tr_d.min():%m-%d}→{tr_d.max():%m-%d} ({len(tr)}) "
            f"| {te_d.min():%m-%d}→{te_d.max():%m-%d} ({len(te)}) "
            f"| {float(y.iloc[te].mean()):.1%} | {auc:.3f} |")
        print(f"fold {k}: train {len(tr)} rows to {tr_d.max():%m-%d}, "
              f"test {len(te)} rows {te_d.min():%m-%d}..{te_d.max():%m-%d}, "
              f"AUC {auc:.3f}")

    ok = oos.notna()
    po, yo, do, ro = oos[ok].values, y[ok].values, day[ok].values, res[ok].values
    fo = fold_id[ok].values
    pooled_auc = roc_auc_score(yo, po)
    pooled_base = float(yo.mean())

    # Operating points are PER-FOLD: the cutoff applied to a fold's rows is a quantile
    # of that fold's own predictions only. A pooled-quantile threshold would let later
    # folds' score levels set earlier folds' cutoffs (future information, and no
    # deployable rule could reproduce it at alert time) and would concentrate the
    # selection in whichever folds score high on an uncalibrated scale.
    ops = {}
    for label, frac in (("top-decile", 0.10), ("top-quintile", 0.20)):
        sel = np.zeros(len(po), dtype=bool)
        for k in np.unique(fo):
            m = fo == k
            sel |= m & (po >= np.quantile(po[m], 1 - frac))
        hit = float(yo[sel].mean())
        lo, hi = day_boot_precision(yo[sel].astype(float), do[sel])
        ops[label] = {"n": int(sel.sum()), "hit": hit, "lo": lo, "hi": hi}

    expl = {r: float(roc_auc_score(yo[ro == r], po[ro == r]))
            for r in ("1m", "15m", "1h")
            if (ro == r).sum() > 30 and len(set(yo[ro == r])) > 1}

    # full-sample coefficients — DESCRIPTIVE ONLY (in-sample), for the report
    est = estimator().fit(X, y)
    coefs = sorted(zip(X.columns, est.named_steps["clf"].coef_[0]),
                   key=lambda kv: -abs(kv[1]))

    dec = ops["top-decile"]
    beats = dec["lo"] > BREAKEVEN
    verdict = (
        "CLEARS the 33% breakeven with CI" if beats else
        ("clears breakeven at the point estimate but NOT with CI"
         if dec["hit"] > BREAKEVEN else "does NOT clear the 33% breakeven"))

    lines = [
        "# Upside classifier — pre-registered experiment report", "",
        "**Question.** Does anything the screener knows at alert time predict "
        "*2.0x before 0.5x within 12h* well enough to clear the 33% gross-EV "
        "breakeven of a 2x/0.5x bracket?", "",
        f"**Verdict: the top-decile operating point {verdict}.** "
        f"Hit rate {dec['hit']:.1%} (day-clustered CI90 "
        f"[{dec['lo']:.1%}, {dec['hi']:.1%}]) vs pooled OOS base rate "
        f"{pooled_base:.1%} and the {BREAKEVEN:.1%} breakeven line. "
        f"Pooled out-of-sample AUC {pooled_auc:.3f} on n={ok.sum()} "
        f"(of {n} labeled alerts).", "",
        "REMINDER: gross of ALL execution costs; the livebook (which measures net "
        "of real quotes) remains the promotion authority. Not financial advice.", "",
        "## Operating points (pre-registered)", "",
        "| point | n flagged | hit rate | day-clustered CI90 | base | breakeven |",
        "|---|---|---|---|---|---|"]
    for label, o in ops.items():
        lines.append(f"| {label} | {o['n']} | {o['hit']:.1%} | "
                     f"[{o['lo']:.1%}, {o['hi']:.1%}] | {pooled_base:.1%} | 33.3% |")
    lines += ["", "## Folds (PurgedWalkForwardCV, day-level, 12h label purge + embargo)",
              "", "| fold | train | test | test base | AUC |", "|---|---|---|---|---|"]
    lines += fold_lines
    lines += ["", "## Exploratory (labeled as such)", "",
              "Per-resolution pooled AUC (1h bars cannot order TP-vs-SL within a bar; "
              "1m is the only unambiguous stratum):", ""]
    for r, a in expl.items():
        lines.append(f"- res={r}: AUC {a:.3f} (n={int((ro == r).sum())})")
    lines += ["", "Top coefficients (FULL-SAMPLE fit — descriptive, in-sample, not "
              "evidence):", ""]
    for c, w in coefs[:12]:
        lines.append(f"- `{c}`: {w:+.3f}")
    lines += ["", "## Refused to claim", "",
              "- Any net-of-costs profitability (this is gross; slippage/impact on "
              "these pools measured -2%+ per round trip).",
              "- Any per-feature causal story (coefficients are descriptive).",
              "- Stability across regimes: 61 days of one summer is one regime.",
              "- The 1h-resolution rows' labels resolve within-bar ordering "
              "pessimistically; the true rate there is bounded, not measured — and a "
              "coarse bar that OPENS inside the horizon can carry a TP that actually "
              "occurred slightly past it.",
              "- The CI90 resamples days with the flagged set held fixed: threshold/"
              "selection variability is not in the interval.", "",
              "## Provenance", "",
              "- features: `upside/features.csv` (git-history first-occurrence rows "
              "of `data/latest_scan.json`; exact alert-time snapshots).",
              "- labels: `upside/labels.csv` (sanitized paths via "
              "`selfimprove/evaluate.load_paths`; pessimistic first-passage).",
              f"- model/CV/thresholds pre-registered in this file; seed {SEED}; "
              "no wall-clock; folds assert strict train<test ordering."]
    with open(os.path.join(HERE, "report.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\npooled OOS AUC {pooled_auc:.3f}; top-decile hit {dec['hit']:.1%} "
          f"CI90 [{dec['lo']:.1%},{dec['hi']:.1%}] vs breakeven 33.3% -> "
          f"report at upside/report.md")


if __name__ == "__main__":
    main()
