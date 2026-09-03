# Upside classifier — pre-registered experiment report

**Question.** Does anything the screener knows at alert time predict *2.0x before 0.5x within 12h* well enough to clear the 33% gross-EV breakeven of a 2x/0.5x bracket?

**Verdict: the top-decile operating point does NOT clear the 33% breakeven.** Hit rate 29.0% (day-clustered CI90 [19.8%, 37.6%]) vs pooled OOS base rate 24.8% and the 33.3% breakeven line. Pooled out-of-sample AUC 0.546 on n=906 (of 1378 labeled alerts).

REMINDER: gross of ALL execution costs; the livebook (which measures net of real quotes) remains the promotion authority. Not financial advice.

## Operating points (pre-registered)

| point | n flagged | hit rate | day-clustered CI90 | base | breakeven |
|---|---|---|---|---|---|
| top-decile | 93 | 29.0% | [19.8%, 37.6%] | 24.8% | 33.3% |
| top-quintile | 184 | 27.2% | [21.0%, 33.0%] | 24.8% | 33.3% |

## Folds (PurgedWalkForwardCV, day-level, 12h label purge + embargo)

| fold | train | test | test base | AUC |
|---|---|---|---|---|
| 0 | 07-04→07-27 (423) | 07-30→08-05 (131) | 21.4% | 0.460 |
| 1 | 07-04→08-03 (571) | 08-06→08-12 (149) | 24.2% | 0.563 |
| 2 | 07-04→08-10 (708) | 08-13→08-19 (213) | 24.9% | 0.648 |
| 3 | 07-04→08-17 (876) | 08-20→08-26 (316) | 27.8% | 0.498 |
| 4 | 07-04→08-24 (1200) | 08-27→09-02 (97) | 20.6% | 0.560 |

## Exploratory (labeled as such)

Per-resolution pooled AUC (1h bars cannot order TP-vs-SL within a bar; 1m is the only unambiguous stratum):

- res=1m: AUC 0.516 (n=190)
- res=15m: AUC 0.547 (n=587)
- res=1h: AUC 0.591 (n=129)

Top coefficients (FULL-SAMPLE fit — descriptive, in-sample, not evidence):

- `buys_h1`: -0.436
- `sells_h1`: +0.426
- `creator_prior_tokens`: -0.403
- `pair_age_min`: -0.370
- `n_hc_misses`: -0.220
- `vol_h1`: +0.217
- `vol1_vol6`: +0.165
- `total_holders`: -0.147
- `graph_insiders`: -0.141
- `liq_mcap`: +0.135
- `vol1_liq`: -0.112
- `score`: +0.108

## Refused to claim

- Any net-of-costs profitability (this is gross; slippage/impact on these pools measured -2%+ per round trip).
- Any per-feature causal story (coefficients are descriptive).
- Stability across regimes: 61 days of one summer is one regime.
- The 1h-resolution rows' labels resolve within-bar ordering pessimistically; the true rate there is bounded, not measured — and a coarse bar that OPENS inside the horizon can carry a TP that actually occurred slightly past it.
- The CI90 resamples days with the flagged set held fixed: threshold/selection variability is not in the interval.

## Provenance

- features: `upside/features.csv` (git-history first-occurrence rows of `data/latest_scan.json`; exact alert-time snapshots).
- labels: `upside/labels.csv` (sanitized paths via `selfimprove/evaluate.load_paths`; pessimistic first-passage).
- model/CV/thresholds pre-registered in this file; seed 42; no wall-clock; folds assert strict train<test ordering.
