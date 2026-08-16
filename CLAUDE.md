# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **alert-only** Solana memecoin screener: discover new tokens → reject rugs/honeypots/insider
launches → rank survivors → push a pre-computed size/stop/TP plan to Telegram/ntfy/macOS → log
forward returns so the screen's real hit-rate is measurable.

**It never touches keys or funds.** That boundary is restated verbatim at seven separate places
across `config.py` (×3), `alerts.py`, `paper_exec.py`, `selfimprove/livebook.py` and `README.md`
— treat it as an invariant, not a preference. The enforcement mechanism is *not* "never automate"; it is a
pre-committed condition, stated in `README.md` and `paper_exec.py`:

> Real automated trading is justified ONLY if the **paper** scorecard is repeatedly positive —
> never the frictionless ledger's. If the edge doesn't survive quoted slippage, an execution
> layer would just automate losses faster.

`README.md` has the file-by-file table and the v1→v4 history; it is not repeated here. Note it is
now **stale in two places**: it calls the PumpPortal WebSocket "deliberately out of scope" (the
listener has run on it for weeks) and claims `data/paper_ledger.csv` is cloud-committed (it
cannot be — see Gotchas).

## Commands

No build system, no pytest, no venv. Python 3.9.5, deps are only `pandas>=2,<4` and `certifi`.
**Every module has a `__main__` smoke test**, so `python3 <module>.py` is the fastest way to
inspect any single piece.

```bash
python3 verify.py                     # THE test suite — 41 invariants, offline, fail-fast
python3 run.py                        # dry run: print ranked survivors, send/write nothing
python3 run.py --commit               # write ledger + state, still no alerts
python3 run.py --send                 # write AND push (what the cloud cron runs)
python3 ledger.py                     # the honest A-vs-B scorecard
python3 paper_exec.py                 # slippage-adjusted paper scorecard (A book + local S book)
python3 listener.py --test            # 60s WebSocket probe, evaluates nothing
```

The self-improvement layer (all local-only, see Gotchas):

```bash
python3 selfimprove/backfill.py       # price real paths for ledger rows (GeckoTerminal, slow)
python3 selfimprove/evaluate.py       # score the pre-declared exit-policy family on those paths
python3 selfimprove/livebook.py --tick        # one live cycle (launchd runs this every 60s)
python3 selfimprove/livebook.py --scorecard   # per-policy live P&L
python3 selfimprove/improve.py        # re-score + apply the promotion gate + write a proposal
```

Three launchd agents in `~/Library/LaunchAgents/`: `com.yousefjan.solana-listener` (resident
WebSocket, KeepAlive), `com.yousefjan.solana-livebook` (60s), `com.yousefjan.solana-improve`
(Sun 10:00).

## Architecture

Four layers. Only the first is deployed to the cloud.

### 1. Discovery → screen → alert

`run.py:screen_token()` is the shared brain — the 15-minute batch scan and the 24/7 listener both
call it. The non-obvious part is that it runs **`hard_gates` twice**:

1. free RugCheck report → `hard_gates` → bail immediately on failure
2. GMGN (keyed, 1 req/s) **only on survivors** → `hard_gates` again → `soft_score` → `high_conviction`

Two independent reasons, both worth preserving: throughput (`MAX_DISCOVER = 60` candidates at
1 req/s would be a minute of wall-clock spent mostly on tokens that die on free gates) and
epistemics — GMGN detects a *different failure mode*, not a stricter version of the same one.

- **`hard_gates`** is boolean reject/accept but returns *every* individual check, so the alert and
  ledger can show exactly why a token passed. That dict is persisted as `gates`.
- **`soft_score`** ranks 0–100 from already-fetched fields — no extra API calls.
- **`high_conviction`** is the A-tier test, applied only to tokens that already passed. It returns
  `(is_a, hc_misses)` where **`hc_misses` is a list of human-readable strings**, not a count.
- Tier is decided in exactly one line (`run.py`): `"tier": "A" if is_a else "B"`.

**A-tier means best *survival* odds, not predicted ROI.** Stated identically in `screen.py`,
`config.py` and `README.md`. Insider presence predicts dumps, not pumps.

The three sources are not interchangeable:

| source | auth | what only it can do |
|---|---|---|
| `sources/rugcheck.py` | free | `/stats/new_tokens` is the primary new-launch feed; authorities, LP lock, insider networks, creator history |
| `sources/dexscreener.py` | free | liquidity/volume/age/tx, and the **only** source of forward-return fills |
| `sources/gmgn.py` | key | behavioural wallet tags (bundler/sniper/rat/fresh) — catches wallet farms that funding-graph clustering is blind to |

GMGN degradation is **mandatory, not incidental**: no key or a failed call returns `{}` and every
gate keyed on those fields passes through. The screener must never go dark because one vendor did,
and `alerts.py` prints "GMGN: unavailable (gates passed through)" so a degraded run is visible.

### 2. The honest measurement spine

`ledger.py` is one CSV, one row per mint, with four horizons (1h/6h/24h/7d).

- `record_alerts` writes an **immutable entry snapshot**, once, and skips mints already present —
  so an entry price can never be back-edited.
- `update_forward` is the only writer of forward data. Each horizon cell is **write-once and
  time-gated**; a later pump cannot rewrite the 1h number. When the 7d cell fills the row goes
  `resolved` and stops costing API calls.
- **Dead tokens are recorded as −100%, never dropped.** That is the anti-survivorship rule.
- Exit signals (`tp`/`stop`) come from the same snapshot and are idempotent per level, so a TP
  cannot double-count across runs.

**The silent B-tier control is the core design idea.** Both tiers are ledgered; only A is alerted.
B is a control group for the A-tier band *itself* — the band is a claim, the ledger is its
falsification test. `ledger.py` prints the verdict rule in its own output: *"If tier A does not
clearly beat tier B here, the A-tier band is NOT adding signal — loosen/rethink it rather than
trusting the label."* `ledger.py` also guards the exit-signal block on `tier != "B"` so the
control never generates alerts.

### 3. Paper execution

`paper_exec.py` simulates the **one shipped strategy** at real Jupiter routing quotes — no keys,
no funds, quotes only. A stop on a dead coin with no route fills at **$0**, recorded honestly.

`selfimprove/livebook.py` runs **all 17 policies simultaneously off ONE shared entry fill**, so
any difference between two policies is caused by the exit and nothing else. One sell quote per
open mint per tick feeds every policy's state machine, so cost is O(open mints), not
O(mints × policies). Two deliberately asymmetric fill conventions: a ladder rung is a
pre-committed **limit** and fills at the rung level; a stop/trail is a **market** exit and fills at
the observed tick (mildly optimistic — `gap_s` is recorded on every fill so that stays auditable).

### 4. The self-improvement loop

`livebook --tick` feeds itself from the cloud-committed ledger (see Gotchas) and accumulates
out-of-sample rows 24/7 → `improve.py` re-scores weekly, applies the promotion gate, and writes a
proposal to `data/proposals/`. **It never edits config.** The champion changes only when a human
edits `champion.json`.

The gate is built against one specific failure: re-scoring a correlated family and adopting
whichever currently leads is a maximum over correlated series, which drifts upward under a pure
null. Measured on the real 639 rows with every policy's advantage recentred to exactly zero mean:
that procedure crosses zero **33.3% of the time at 12 alert-days, 20.0% at 41**. So the gate
requires a **paired** bound, a **forward-only** test (nomination records `alert_seq`; only later
rows may be scored), a 40-cluster floor, DSR on day means, and — most importantly — that the
**negative controls fail**. If a control passes, no proposal is emitted at all.

## Measured state — read the sample sizes, they are the story

- **The A arm has 13 rows, ever.** On the ledger as of 2026-08-16 (1,007 rows) A medians
  **+34.0% @6h** against B's −81.1% — the band looks like it is working. Hold that loosely: it is
  13 observations, and as recently as 2026-07-27 the same ledger at n=6 said the *opposite*
  (A −89.1% @6h, worse than B at every horizon). Anything resting on "+34% @6h" rests on 13 rows,
  and the sign of that comparison has already flipped once.
- **The exit backtest measures the control, not the product.** Of 639 priced rows, ~632 are
  B-tier. `hold_to_end` −0.771 (day-clustered LB −0.824); best exit −0.111. **Nothing clears zero.**
- **The negative controls are the headline finding.** `ctl_exit_immediately` (buy and sell in the
  same instant — pure cost, zero information) scores day-LB −0.117, and `ctl_random_exit` scores
  −0.649. Both beat `hold_to_end`. So *"every exit beats holding, p=0.0000"* means **holding to
  zero is catastrophic**, not that any exit is skilful. No policy has been shown to beat a coin flip.
- **Live execution cost ranges −2.7% to −27.5%** (2026-08-12/15). One token round-tripped at
  −27.5% in 15 minutes *with no price movement*. The backtest charges a flat 2%, so treat backtest
  P&L as optimistic until the live book has the rows to replace it.

## Load-bearing invariants

`verify.py` is the entire harness — no pytest, no `tests/`. It is offline (injected fakes, temp
paths), fail-fast, and every check corresponds to a mistake actually made.

- **No wall-clock in any scoring path.** Machine-enforced: `verify.py` greps `screen.py` source
  for `datetime`, `time.time`, `.now(`. `run.py` captures `time.time()` exactly once and threads
  it through; it drives freshness and horizon bookkeeping only.
- **No magic numbers outside `config.py`** — the whole screen re-tunes from one file.
- **`null` authority means REVOKED means good.** An active mint authority is the #1 rug vector and
  is always rejected.
- **A-tier can never be laxer than the hard gates.**
- **The $Cubrate replay must never be A-tier again** (see below).
- **A trailing stop exits off the high-water mark**, never off a price that hasn't happened yet.
- **The promotion gate refuses thin evidence.**

## Named incidents

**$Cubrate (2026-07-05)** — the important one. An A-tier alert at score 80.5 went −99% in ~15
minutes. At alert: age 19.9 min, 1,344 holders (67/min), 5,649 tx/hr (4.2 per holder), top-10
4.4%. RugCheck `insiderNetworks` showed 0 *before and after* the rug. This was **not a loose gate —
it was the soft score being fooled**: a wallet farm manufactures exactly the metrics the score
rewards (holders, volume, buy pressure, low concentration). Produced `HC_MIN_AGE_MINUTES = 90`,
`HC_MAX_HOLDERS_PER_MIN = 8`, `HC_MAX_TX_PER_HOLDER_H1 = 3`, `HC_GMGN_BUNDLER_RATIO_MAX = 0.10`,
the hard `GMGN_BUNDLER_RATIO_MAX = 0.5`, and a permanent replay test with the literal at-alert
numbers. Design principle, from `screen.py`: *metrics a bot farm inflates look impossibly good on
a young coin; require organic-plausible rates.*

**$ITSY / $BULLION (v1→v2)** — bundled launches spread supply across 10–25 fresh wallets, so they
look clean on top-10 concentration. Produced the three free insider gates (`insiderNetworks`,
`graphInsidersDetected`, `creatorTokens`) from report fields v1 was already fetching but ignoring.

**pandas 3 (silent, ~3 weeks)** — `dtype=str` became a strict string dtype that raised on the
mixed cell writes `update_forward` does. Every cloud run crashed; **alerts were still sent but
never recorded, and cooldown state reset.** Hence the `pandas>=2,<4` pin and `astype(object)`.

**Within-bar look-ahead (2026-08-12)** — the exit simulator ratcheted the trailing stop's
high-water mark to the current bar's high *before* testing that bar's low. It moved the headline
policy from mean +0.142 / LB +0.006 (appeared to clear the bar) to −0.164 / −0.184. Note the
related lesson: `pessimistic <= optimistic` holds for *fixed-level* exits but **not** for
path-dependent ones like a trailing stop, because the ordering changes the state variable itself.

## Gotchas

- **The cloud is authoritative for `data/`.** The GitHub Action commits `data/ledger.csv`,
  `data/alert_state.json`, `data/latest_scan.json` and `docs/index.html` back every 15 minutes as
  `solana-screener[bot]`. Never hand-edit those. **`git pull` before any analysis** — this repo
  sat 260 commits behind for weeks, and the stale local ledger disagreed with the cloud's about
  whether A-tier beats B (see Measured state).
- **The livebook feeds itself from the cloud ledger, and pays an entry lag for it.** `run.py`
  opens positions but runs on the Actions runner while the ticker is launchd-local, so
  `LIVEBOOK_ENABLED` is env-gated (`export SOLANA_LIVEBOOK=1`) and **off by default** — otherwise
  the cloud would spend a Jupiter quote per survivor filling a book it discards on exit. Instead
  `livebook --tick` reads `git show origin/main:data/ledger.csv` after a `git fetch` (never a
  `git pull` — a collection job must not be able to conflict or move HEAD). The cost: the cloud
  commits every 15 min, so entries land **15–25 minutes after `alert_ts`**. Every position records
  `entry_lag_s`; anything past `MAX_ENTRY_LAG_S` (30 min) is refused into
  `data/livebook_missed.jsonl` with its lag, so the kept set's bias is checkable rather than
  invisible. **Read `entry_lag_s` before trusting any live number** — it is not a t=0 entry.
- **The workflow does `git add data/`, a directory add.** Anything new and non-ignored under
  `data/` is committed automatically, and `.gitignore` does not cover `data/livebook*`,
  `data/paths.jsonl` (~30 MB), `data/proposals/` or `data/corrupt/`.
- **API rate limits are per-IP and shared with sibling projects.** GeckoTerminal is a hard 30/min
  shared with `~/robinhood_screener`'s every-2-min job. Measured 2026-08-12: 0.4 Hz gave ~47%
  429s, so 0.25 Hz is *faster as well as politer* — a 429 costs a call and buys nothing. Do not
  raise it to "speed things up".
- **`sys.path` order when borrowing `entry_bot/stats.py`.** Both repos have a top-level
  `config.py`, so entry_bot must be **appended**, never inserted — otherwise `import config`
  resolves to *its* config. See the comment in `selfimprove/evaluate.py`.
- **`listener.py` prunes launch logs older than 7 days at startup.** It runs once, from `main()`,
  so retention is "7 days since the last restart", not rolling — the process has been up for
  weeks and much older logs survive. **Restarting deletes them.** Copy first.
- **The Mac sleeps** (`pmset` shows `sleep 1` on both AC and battery, and nothing holds a
  caffeinate assertion). The livebook's 60-second cadence is therefore nominal, not real; `gap_s`
  is recorded on every tick and fill so the damage stays measurable rather than assumed.
- **Ledger horizon cells carry drift.** A cell is stamped at the price of the run that *first
  observes* maturity, not at the horizon — a 1h cell filled at t+70min carries 10 minutes of
  drift, unbounded if the cloud job is down. `max_ret_seen`/`min_ret_seen` are sampled at run
  cadence, not true extremes.
- **Two independent cooldown stores.** The scan uses `data/alert_state.json` (cloud-committed),
  the listener uses `data/listener_state.json` (gitignored). They do not share state, so a mint
  can alert once from each path within the 24h window. Dedup happens downstream instead —
  `paper_exec.open_position` and `livebook.open_alert` are both idempotent per mint.
- **`sources/__init__.py` is stale** — its docstring omits `gmgn` entirely.
- Everything is **stdlib urllib + certifi, no `requests`**. System certs fail with
  `CERTIFICATE_VERIFY_FAILED`; the same idiom applies to the WebSocket (`ca_certs=certifi.where()`).

## Ethos

Inherited from the workspace and non-negotiable: the decision criterion is a **clustered bootstrap
lower bound, never a mean**; multiple-testing correction uses Benjamini–**Yekutieli** (valid under
dependence — the exit policies are correlated restatements of each other); a bound built from
fewer than `MIN_BOOTSTRAP_CLUSTERS` clusters is not a bound; and every experiment ships with a
kill condition (A-vs-B, the S book, the promotion gate). The honest prior is that this class of
trade is −EV. `ledger.py` says it in its own output: *"negative expectancy is the base rate. A
losing scorecard here is the screen doing its job — telling you not to scale."*
