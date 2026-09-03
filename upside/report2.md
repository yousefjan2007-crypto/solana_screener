# Upside classifier — round 2 (momentum + regime)

**Disclosure: second pre-registered look at the same labels** (round 1: safety features, top-decile 29.0% CI90 [19.8%, 37.6%], AUC 0.546). One primary model per round; any positive must survive that framing.

**Verdict: the primary top-decile operating point does NOT clear the 33.3% breakeven.** Hit 33.3% (day-clustered CI90 [24.5%, 41.6%]) vs pooled OOS base 24.8%; pooled OOS AUC 0.541 (n=906).

Gross of all costs; livebook remains the promotion authority. Not financial advice.

## Models

| model | AUC | top-decile hit | CI90 | top-quintile hit | CI90 |
|---|---|---|---|---|---|
| PRIMARY: safety + momentum + regime | 0.541 | 33.3% | [24.5%, 41.6%] | 23.9% | [17.7%, 30.0%] |
| exploratory: momentum + regime only | 0.531 | 25.5% | [17.2%, 33.8%] | 29.9% | [23.6%, 36.0%] |

## Primary folds

| fold | train n | test n | test base | AUC |
|---|---|---|---|---|
| 0 | 423 | 131 | 21.4% | 0.521 |
| 1 | 571 | 149 | 24.2% | 0.615 |
| 2 | 708 | 213 | 24.9% | 0.598 |
| 3 | 876 | 316 | 27.8% | 0.462 |
| 4 | 1200 | 97 | 20.6% | 0.532 |

## Top coefficients (full-sample, descriptive only)

- `sells_h1`: +0.452
- `buys_h1`: -0.434
- `creator_prior_tokens`: -0.409
- `pair_age_min`: -0.364
- `vol_h1`: +0.230
- `n_hc_misses`: -0.199
- `disc_6h`: +0.192
- `disc_accel`: -0.164
- `graph_insiders`: -0.147
- `r_15m`: -0.143
- `total_holders`: -0.139
- `vol1_liq`: -0.133
- `liq_mcap`: +0.131
- `regime_scans_24h`: -0.131

## Caveats carried from round 1

- momentum coverage is partial (48% of rows; older alerts' fine bars don't reach back) — `has_momentum` is itself a feature so the model can't silently exploit the gap;
- regime windows before 2026-07-04 have thin scan history;
- CI90 holds the flagged set fixed per resample; 61 days = one regime;
- labels pessimistic; 1h-resolution rows bound, not measure.

## Provenance

- features2.csv: pre-alert bars from cached GeckoTerminal blobs (strictly ts < alert_ts) + per-scan counts mined from git history;
- same CV/model/thresholds as round 1; seed 42; no wall-clock; folds assert strict train<test.
