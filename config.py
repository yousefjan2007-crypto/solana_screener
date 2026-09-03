"""
Central configuration for solana_screener — an honest Solana memecoin screener/alerter.

Self-contained sibling project (like valuation-monitor): no imports from signal_lab or
sell_in_may. It REUSES your existing push secrets by reading the very same
vrp_backtest/monitor_config.json that signal_lab borrows from — one source of truth for
the Telegram/ntfy creds, so there is no drift, and no heavy import chain to break.

Ethos (shared with the rest of the workspace): every threshold lives HERE so the whole
screen re-tunes from one file; any randomness must go through np.random.default_rng(SEED);
and no wall-clock ever enters a compute/scoring path. This tool is ALERT-ONLY — it never
touches keys or funds. Not financial advice.
"""
from __future__ import annotations

import json
import os

# ── paths ────────────────────────────────────────────────────────────────────────
HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.abspath(__file__))
VRP_BACKTEST = os.path.join(HOME, "vrp_backtest")

DATA_DIR = os.path.join(ROOT, "data")     # ledger + state — COMMITTED back on cloud runs
CACHE_DIR = os.path.join(ROOT, "cache")   # ephemeral API caches — gitignored
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.csv")
STATE_PATH = os.path.join(DATA_DIR, "alert_state.json")
SCAN_PATH = os.path.join(DATA_DIR, "latest_scan.json")
DOCS_DIR = os.path.join(ROOT, "docs")   # GitHub Pages source (the phone dashboard)

# S-tier smart-money experiment (listener-local; gitignored so it never conflicts with
# the state the cloud commits). smart_wallets.json IS committed — it's configuration.
SMART_WALLETS_PATH = os.path.join(ROOT, "smart_wallets.json")
LEDGER_S_PATH = os.path.join(DATA_DIR, "ledger_s.csv")
S_UPDATE_INTERVAL_MIN = 30   # listener maintenance thread: S-ledger forward-fill cadence
for _d in (DATA_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

SEED = 42  # any randomness → np.random.default_rng(SEED); never global np.random.*

# ── statistical guards (selfimprove/) ────────────────────────────────────────────
# Consumed by entry_bot/stats.py, which selfimprove/evaluate.py imports directly rather than
# reimplementing: it is already a decoupled port of the FDR / deflated-Sharpe / reality-check
# guards and its interface is (outcomes, {rule: mask}) with no panel assumptions.
FDR_Q = 0.10                # Benjamini-YEKUTIELI (valid under arbitrary dependence), not BH:
                            # the exit policies are heavily correlated restatements of each other
BOOTSTRAP_REPS = 4000
BOOTSTRAP_ALPHA = 0.025     # the decision criterion is the 2.5th percentile, never the mean
MIN_BOOTSTRAP_CLUSTERS = 12  # a bound built from fewer clusters than this is not a bound

# ── HTTP tunables ────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
DEXSCREENER_RATE_HZ = 4.0   # docs allow ~300/min; stay well under
RUGCHECK_RATE_HZ = 1.0      # free limit undocumented → conservative
GECKOTERMINAL_RATE_HZ = 0.25  # free tier is a hard 30 calls/min PER IP, shared with
                              # ~/robinhood_screener's every-2-min job. Measured 2026-08-12:
                              # 0.4 Hz (24/min) left too little headroom and ~47% of calls came
                              # back 429, so effective throughput was 0.4*0.53 = 0.21/s. Backing
                              # off to 0.25 Hz is FASTER as well as politer — a 429 costs a call
                              # and buys nothing. Do not raise it to "speed things up".
USER_AGENT = "solana_screener research (personal use)"
ENRICH_CACHE_MIN = 2        # cache dexscreener enrichment 2 min
REPORT_CACHE_MIN = 10       # cache rugcheck reports 10 min

# ── discovery ─────────────────────────────────────────────────────────────────────
CHAIN = "solana"
MAX_DISCOVER = 60           # cap tokens enriched per run (rate-limit friendly)

# ── HARD GATES (reject a token outright) ──────────────────────────────────────────
REQUIRE_MINT_REVOKED = True
REQUIRE_FREEZE_REVOKED = True
LP_LOCKED_MIN_PCT = 90.0
REJECT_IF_RUGGED = True
LIQ_FLOOR_USD = 10_000.0     # DEXTools: <$10k = brutal slippage / hard exit
MIN_VOL_H24_USD = 20_000.0   # need real flow to be able to sell
TOP10_MAX_PCT = 40.0         # DEXTools danger line
INSIDER_MAX_PCT = 25.0       # bundle/sniper/insider supply
DEV_MAX_PCT = 5.0            # DEXTools danger line
RISK_SCORE_MAX = 60.0        # rugcheck score_normalised (higher = riskier)
# any DANGER-level rugcheck risk whose name contains one of these → reject
DANGER_RISK_BLOCKLIST = [
    "mint authority", "freeze authority", "top 10 holders", "single holder",
    "low liquidity", "lp unlocked", "honeypot",
]
# Insider/bundle gates (research-backed: funding-graph clustering is the #1 validated
# bundled-scam signal; RugCheck computes the networks for us in the report we already
# fetch — insiderNetworks / graphInsidersDetected / creatorTokens).
INSIDER_NETWORK_MAX_PCT = 10.0   # max % of supply held by clustered insider networks
GRAPH_INSIDERS_MAX = 30          # max wallets RugCheck's graph clustering flags as insiders
CREATOR_MAX_PRIOR_TOKENS = 3     # serial deployers = scam factories (Solidus: 18% of
                                 # creators produce most rugs; one creator seen with 50)
CREATOR_DEAD_MCAP_USD = 30_000.0 # a prior launch below this mcap counts as dead
CREATOR_MAX_DEAD_FRAC = 0.5      # reject if > half of prior launches are dead (min 2 prior)
# GMGN behavioral-tag gates (second opinion; applied ONLY when GMGN data is available —
# no key / failed call degrades gracefully). Raw bundler COUNT scales with volume (WIF
# has 1,000), so the gate is the bundler-to-holder RATIO. Measured calibration
# 2026-07-05: WIF 0.002, trumplet 0.01 (organic) vs Cubrate rug 1.42, LISA 0.71.
GMGN_BUNDLER_RATIO_MAX = 0.5     # hard gate: more bundlers than half the holder count
GMGN_RAT_WALLETS_MAX = 10        # "rat trader" = GMGN's insider/sneak-trading tag
GMGN_SNIPER_HOLD_MAX_PCT = 10.0  # supply still held by launch snipers
GMGN_INSIDER_HOLD_MAX_PCT = 15.0 # suspected-insider hold rate, when reported

# ── SOFT SCORE (rank survivors 0-100) ─────────────────────────────────────────────
HOLDERS_SAFE = 1000          # DEXTools: >1000 safe
HOLDERS_DANGER = 200         # DEXTools: <200 danger
VMC_CAP = 3.0                # cap volume-to-mcap contribution
AGE_MIN_MINUTES = 15         # penalize < this (too raw, data unreliable)
AGE_SWEET_MINUTES = 360      # ~6h peak of the age band
AGE_MAX_MINUTES = 4320       # ~3d; older decays toward 0
SOFT_WEIGHTS = {
    "vol_mcap": 0.30, "buy_sell": 0.20, "holders": 0.20,
    "concentration": 0.15, "age": 0.15,
}
# DEXTools reference rules-of-thumb (informational; some feed the soft score)
TOP_HOLDER_SAFE_PCT = 5.0
TOP_HOLDER_DANGER_PCT = 15.0
TOP10_SAFE_PCT = 20.0

# ── risk / exit discipline shipped inside each alert (alert-only) ─────────────────
STACK_USD = 500.0            # the small memecoin stack — SEPARATE from the options stack
POSITION_PCT = 0.02          # 2% of stack per alert (~$10) — tiny by design
MAX_CONCURRENT = 10          # don't let alerts imply >100% deployed
HARD_STOP_PCT = 0.50         # -50% hard stop (memecoins gap; wider than equities)
TP_LADDER = [(2.0, 0.50), (5.0, 0.25), (10.0, 0.15)]  # (multiple, fraction to sell)
MOONBAG_PCT = 0.10           # remainder rides

# ── A-TIER (high-conviction) alert band ──────────────────────────────────────────
# HONESTY NOTE: "A-tier" means best SURVIVAL odds under every gate + top-band quality
# signals — not predicted ROI. Nothing predicts the next 100x; insider presence predicts
# dumps, not pumps. B-tier survivors are logged to the ledger but NOT alerted, so the
# ledger accumulates an honest A-vs-B test of whether this tier earns its name.
HC_MIN_SCORE = 70.0              # soft score floor for A-tier
HC_INSIDER_NETWORK_MAX_PCT = 0.0 # A-tier tolerates NO detected insider-network supply
HC_GRAPH_INSIDERS_MAX = 5        # ...and at most a handful of graph-flagged wallets
HC_CREATOR_MAX_PRIOR = 1         # first or second launch only
HC_TOP10_MAX_PCT = 20.0          # DEXTools "safe" line, tighter than the 40% hard gate
HC_MIN_HOLDERS = 1000            # DEXTools "safe" line
HC_MIN_LIQ_USD = 25_000.0        # deeper exit pool than the $10k hard floor
# Post-mortem gates from the $Cubrate rug (2026-07-05: A-tier at age 20 min with 1,344
# "holders" and $397k "volume" — all bot-manufactured; -99% within the hour):
HC_MIN_AGE_MINUTES = 90          # 60% of rugs collapse <20 min post-graduation; a real
                                 # coin is still a real coin at 90 min — a scam isn't
HC_MAX_HOLDERS_PER_MIN = 8.0     # organic-growth cap (Cubrate: 67/min = wallet farm)
HC_MAX_TX_PER_HOLDER_H1 = 3.0    # bot-churn cap (Cubrate: 4.2 buys+sells/holder/hour)
HC_GMGN_BUNDLER_RATIO_MAX = 0.10 # A-tier band: organic coins measure ~0.00-0.01

# ── alerting ──────────────────────────────────────────────────────────────────────
ALERT_TOP_N = 5
ALERT_COOLDOWN_HOURS = 24
FOOTER = ("Probabilistic screen, NOT a guarantee. Solana memecoins are near-100%-loss-"
          "prone (~98.6% go to ~0). Risk only what you can lose. These are minutes-to-"
          "hours old, not first-block. Not financial advice.")

# ── ledger forward horizons (seconds) ─────────────────────────────────────────────
LEDGER_HORIZONS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

# ── paper execution (Jupiter quotes only — NO keys, NO funds, NO transactions) ─────
# Measures the number the frictionless ledger can't: EXECUTION COST. A $10 round trip
# through a ~$80k-liquidity pump.fun pool measured ~-2% before the price moved
# (2026-07-05). Real automation is justified ONLY if the PAPER scorecard
# (python3 paper_exec.py) is repeatedly positive — never the ledger's alone.
PAPER_EXEC = True
JUP_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"   # free tier ≈ 1 req/s
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAPER_SLIPPAGE_BPS = 100
PAPER_LEDGER_PATH = os.path.join(DATA_DIR, "paper_ledger.csv")        # A book (cloud-committed)
PAPER_POSITIONS_PATH = os.path.join(DATA_DIR, "paper_positions.json")
PAPER_LEDGER_S_PATH = os.path.join(DATA_DIR, "paper_ledger_s.csv")    # S book (local-only)
PAPER_POSITIONS_S_PATH = os.path.join(DATA_DIR, "paper_positions_s.json")

# ── live multi-policy paper book (selfimprove/livebook.py) ───────────────────────────
# Every pre-declared exit policy trades every alert simultaneously off one shared entry fill.
# It exists because the backtest cannot settle three things and this can: within-bar ordering
# (a tick stream has no ordering to assume), bar coarseness (347 of 639 backfilled rows priced
# on 1-HOUR bars), and execution cost (the backtest charges a flat 2%; live Jupiter round trips
# measured -2.8% to -27.6% on 2026-08-12 depending on liquidity). It is also the only source of
# genuinely out-of-sample rows, which is the one real cure for having searched the history.
# Quotes only — no keys, no funds, no transactions.
#
# OFF BY DEFAULT, and the default is the point. `run.py` is what feeds the book, and run.py runs
# on the GitHub Actions runner — but the 60s tick job (com.yousefjan.solana-livebook) is launchd,
# i.e. local, and data/livebook.json is gitignored local state. So with this on in the cloud, each
# run would spend one Jupiter quote per survivor to open positions into a book that is discarded
# when the runner exits, while the local ticker sits on an empty book. Enable it only where the
# ticker actually runs:  export SOLANA_LIVEBOOK=1
#
# KNOWN GAP (2026-08-16): that means the book currently has no automatic feed on this Mac, because
# run.py is not scheduled locally. The clean fix is for livebook to open positions from the
# cloud-committed data/ledger.csv on each tick — the cloud stays the system of record and the book
# reads it — which also requires this repo to actually pull. Until then, seed it by hand.
LIVEBOOK_ENABLED = os.environ.get("SOLANA_LIVEBOOK", "").lower() in ("1", "true", "yes")

# ── the ledger-driven feed (how the book gets alerts without run.py running locally) ──
# The tick reads the CLOUD-committed ledger via `git fetch` + `git show origin/main:data/ledger.csv`
# — incremental, needs no auth, and never touches the working tree (a data-collection job must not
# be able to cause a merge conflict).
LEDGER_POLL_S = 300.0             # how often to re-read the cloud ledger; the cron commits /15min
#
# ENTRY LAG IS THE HONEST PROBLEM WITH THIS FEED, so it is capped and recorded rather than assumed
# away. The cloud scan runs every 15 min and only then commits, so a row reaches us 15-25 min after
# its alert_ts. `~/entry_bot/CLAUDE.md` measures what delay does to a launch trade (entering at
# t+60s scored -0.654 against -0.045 at t=0) — that is a first-block context and this is not one
# (HC_MIN_AGE_MINUTES = 90 means A-tier tokens are already 90+ min old when alerted), but the
# direction of the bias is not in doubt. Every position records entry_lag_s; anything past the cap
# is refused and written to livebook_missed.jsonl WITH its lag, so the kept set's bias stays
# checkable offline instead of being invisible.
MAX_ENTRY_LAG_S = 30 * 60


def _check_perms(path):
    """Warn when a credential file is readable by anyone but its owner.

    Secrets sit in plaintext behind FileVault; the file mode is the only thing
    between them and every other process on this machine. A file created by a
    shell redirect lands at the umask default (0644), which is how three of
    these ended up world-readable — so this warns rather than assumes.
    ai_visibility/setkey.py is the writer that gets it right (chmod 0600).
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print("  [warn] %s is mode %o (world-readable) - run: chmod 600 %s"
              % (path, mode, path))


def load_credentials() -> dict:
    """Load push secrets without hardcoding them. Priority, so the SAME code runs both on
    a GitHub Actions runner and on your Mac:
      1. env vars (GitHub Secrets): NTFY_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
      2. solana_screener/config.local.json (gitignored local override).
      3. vrp_backtest/monitor_config.json (the shared Mac secrets).
    Returns {ntfy_topic, telegram:{bot_token,chat_id}, anthropic_api_key}."""
    creds = {"ntfy_topic": os.environ.get("NTFY_TOPIC"),
             "telegram": {}, "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY"),
             "gmgn_api_key": os.environ.get("GMGN_API_KEY")}
    bt, cid = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if bt and cid:
        creds["telegram"] = {"bot_token": bt, "chat_id": cid}

    local = os.path.join(ROOT, "config.local.json")
    if os.path.exists(local):
        _check_perms(local)
        try:
            loc = json.load(open(local))
            for k, v in loc.items():
                if v and not creds.get(k):
                    creds[k] = v
        except Exception:
            pass

    if not creds["telegram"]:
        mon = os.path.join(VRP_BACKTEST, "monitor_config.json")
        if os.path.exists(mon):
            _check_perms(mon)
            try:
                m = json.load(open(mon))
                creds["telegram"] = m.get("telegram", {})
                creds["ntfy_topic"] = creds["ntfy_topic"] or m.get("ntfy_topic")
            except Exception:
                pass
    return creds


if __name__ == "__main__":
    print("solana_screener config")
    print(f"  ROOT      {ROOT}")
    print(f"  DATA_DIR  {DATA_DIR}")
    print(f"  gates: liq>=${LIQ_FLOOR_USD:,.0f}  vol24>=${MIN_VOL_H24_USD:,.0f}  "
          f"top10<={TOP10_MAX_PCT:.0f}%  insider<={INSIDER_MAX_PCT:.0f}%  "
          f"dev<={DEV_MAX_PCT:.0f}%  risk<={RISK_SCORE_MAX:.0f}")
    print(f"  mint/freeze revoked required: {REQUIRE_MINT_REVOKED}/{REQUIRE_FREEZE_REVOKED}"
          f"  LP locked>= {LP_LOCKED_MIN_PCT:.0f}%")
    print("  creds present:", {k: bool(v) for k, v in load_credentials().items()})
