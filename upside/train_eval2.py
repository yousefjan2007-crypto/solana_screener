"""
Round 2 of the upside experiment: does PRE-ALERT MOMENTUM + TRENCH REGIME predict
"2x before -50% within 12h" where the safety feature set (round 1: OOS AUC 0.546,
top-decile 29.0% vs 33.3% breakeven) could not?

MULTIPLE-TESTING DISCLOSURE, up front: this is the SECOND pre-registered look at
the same labels. One primary model per round; a positive result here must be read
against two rounds of testing (and the report says so in its verdict). Ablations
(momentum-only) are exploratory and can never support a claim.

Primary (pre-registered before running): round-1 features + momentum + regime,
same pipeline (impute -> scale -> elastic-net logistic), same PurgedWalkForwardCV
folds, same two per-fold operating points, same 33.3% breakeven bar.

Usage:  python3 upside/train_eval2.py    # needs features.csv/features2.csv/labels.csv
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/signal_lab"))

from cv import PurgedWalkForwardCV                       # noqa: E402
from train_eval import (BREAKEVEN, HORIZON_S, SEED,      # noqa: E402
                        day_boot_precision, estimator)
from sklearn.metrics import roc_auc_score                # noqa: E402

from train_eval import build_xy as build_round1          # noqa: E402

# r_5m is computed by features2.py but excluded here: no historical row has a
# pre-alert 1-minute window (the cached minute1 blobs span ~16h back from fetch
# time, weeks after most alerts) — it becomes usable once live collection runs.
NEW = ["disc_6h", "disc_24h", "surv_24h", "disc_accel", "regime_scans_24h",
       "pre_bars", "r_15m", "r_30m", "r_60m", "range_30m", "vol_accel",
       "above_first"]


def build_xy2():
    X1, y, t1, res, day = build_round1()
    f2 = pd.read_csv(os.path.join(HERE, "features2.csv"))
    f2 = f2.set_index("mint")
    mints = X1.index.get_level_values("symbol")          # level named symbol == mint
    add = f2.reindex(mints)[NEW].apply(pd.to_numeric, errors="coerce")
    add.index = X1.index
    add["has_momentum"] = add["pre_bars"].notna().astype(float)
    for c in ("disc_6h", "disc_24h", "surv_24h"):        # heavy-tailed counts
        add[c] = np.log10(np.clip(add[c].astype(float), 1.0, None))
    X = pd.concat([X1, add], axis=1)
    return X, y, t1, res, day, list(X1.columns), list(add.columns)


def run(X, y, t1, day, label):
    cv = PurgedWalkForwardCV(t1=t1, n_splits=5, test_size=7,
                             embargo_frac=0.02, min_train_size=21)
    oos = pd.Series(np.nan, index=X.index)
    fold_id = pd.Series(-1, index=X.index)
    fold_lines = []
    for k, (tr, te) in enumerate(cv.split(X)):
        tr_d, te_d = day.iloc[tr], day.iloc[te]
        assert tr_d.max() < te_d.min(), "fold not strictly forward"
        if y.iloc[tr].nunique() < 2 or y.iloc[te].nunique() < 2:
            continue
        est = estimator().fit(X.iloc[tr], y.iloc[tr])
        p = est.predict_proba(X.iloc[te])[:, 1]
        oos.iloc[te] = p
        fold_id.iloc[te] = k
        fold_lines.append((k, len(tr), len(te), float(y.iloc[te].mean()),
                           float(roc_auc_score(y.iloc[te], p))))
    ok = oos.notna()
    po, yo, do = oos[ok].values, y[ok].values, day[ok].values
    fo = fold_id[ok].values
    out = {"label": label, "n": int(ok.sum()),
           "auc": float(roc_auc_score(yo, po)), "base": float(yo.mean()),
           "folds": fold_lines, "ops": {}}
    for op, frac in (("top-decile", 0.10), ("top-quintile", 0.20)):
        sel = np.zeros(len(po), dtype=bool)
        for k in np.unique(fo):
            m = fo == k
            sel |= m & (po >= np.quantile(po[m], 1 - frac))
        lo, hi = day_boot_precision(yo[sel].astype(float), do[sel])
        out["ops"][op] = {"n": int(sel.sum()), "hit": float(yo[sel].mean()),
                          "lo": lo, "hi": hi}
    return out


def main() -> None:
    X, y, t1, res, day, old_cols, new_cols = build_xy2()
    print(f"n={len(y)}  base={y.mean():.1%}  features: {len(old_cols)} old + "
          f"{len(new_cols)} new  momentum coverage="
          f"{X['has_momentum'].mean():.1%}")

    primary = run(X, y, t1, day, "PRIMARY: safety + momentum + regime")
    momo = run(X[new_cols], y, t1, day, "exploratory: momentum + regime only")

    dec = primary["ops"]["top-decile"]
    beats_ci = dec["lo"] > BREAKEVEN
    beats_pt = dec["hit"] > BREAKEVEN
    verdict = ("CLEARS the 33.3% breakeven with CI90 — but this is round 2 of 2 "
               "looks at these labels; treat as promising, demand live shadow "
               "confirmation before believing it" if beats_ci else
               "clears breakeven at the point estimate only — NOT a claim"
               if beats_pt else "does NOT clear the 33.3% breakeven")

    est = estimator().fit(X, y)
    coefs = sorted(zip(X.columns, est.named_steps["clf"].coef_[0]),
                   key=lambda kv: -abs(kv[1]))

    lines = [
        "# Upside classifier — round 2 (momentum + regime)", "",
        "**Disclosure: second pre-registered look at the same labels** (round 1: "
        "safety features, top-decile 29.0% CI90 [19.8%, 37.6%], AUC 0.546). One "
        "primary model per round; any positive must survive that framing.", "",
        f"**Verdict: the primary top-decile operating point {verdict}.** "
        f"Hit {dec['hit']:.1%} (day-clustered CI90 [{dec['lo']:.1%}, {dec['hi']:.1%}]) "
        f"vs pooled OOS base {primary['base']:.1%}; pooled OOS AUC "
        f"{primary['auc']:.3f} (n={primary['n']}).", "",
        "Gross of all costs; livebook remains the promotion authority. "
        "Not financial advice.", "",
        "## Models", "",
        "| model | AUC | top-decile hit | CI90 | top-quintile hit | CI90 |",
        "|---|---|---|---|---|---|"]
    for m in (primary, momo):
        d, q = m["ops"]["top-decile"], m["ops"]["top-quintile"]
        lines.append(f"| {m['label']} | {m['auc']:.3f} | {d['hit']:.1%} | "
                     f"[{d['lo']:.1%}, {d['hi']:.1%}] | {q['hit']:.1%} | "
                     f"[{q['lo']:.1%}, {q['hi']:.1%}] |")
    lines += ["", "## Primary folds", "", "| fold | train n | test n | test base | AUC |",
              "|---|---|---|---|---|"]
    for k, ntr, nte, b, a in primary["folds"]:
        lines.append(f"| {k} | {ntr} | {nte} | {b:.1%} | {a:.3f} |")
    lines += ["", "## Top coefficients (full-sample, descriptive only)", ""]
    for c, w in coefs[:14]:
        lines.append(f"- `{c}`: {w:+.3f}")
    lines += ["", "## Caveats carried from round 1", "",
              "- momentum coverage is partial "
              f"({X['has_momentum'].mean():.0%} of rows; older alerts' fine bars "
              "don't reach back) — `has_momentum` is itself a feature so the model "
              "can't silently exploit the gap;",
              "- regime windows before 2026-07-04 have thin scan history;",
              "- CI90 holds the flagged set fixed per resample; 61 days = one regime;",
              "- labels pessimistic; 1h-resolution rows bound, not measure.", "",
              "## Provenance",
              "", "- features2.csv: pre-alert bars from cached GeckoTerminal blobs "
              "(strictly ts < alert_ts) + per-scan counts mined from git history;",
              f"- same CV/model/thresholds as round 1; seed {SEED}; no wall-clock; "
              "folds assert strict train<test."]
    with open(os.path.join(HERE, "report2.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nPRIMARY: AUC {primary['auc']:.3f}; top-decile {dec['hit']:.1%} "
          f"CI90 [{dec['lo']:.1%},{dec['hi']:.1%}] vs 33.3% -> upside/report2.md")
    print(f"exploratory momentum-only: AUC {momo['auc']:.3f}, "
          f"top-decile {momo['ops']['top-decile']['hit']:.1%}")


if __name__ == "__main__":
    main()
