# solana_screener

An **honest** Solana memecoin screener + alerter. It discovers new tokens early, auto-rejects
the obvious rugs/honeypots/insider launches (RugCheck + Dexscreener data), ranks the survivors,
and pushes them to your existing Telegram / ntfy / macOS channels with a **pre-computed
size / hard-stop / take-profit plan**. It then logs every alert and its forward returns so you
can measure the screen's **real** hit-rate over time.

**Alert-only. It never touches keys or funds. Not financial advice.**

## The honest part (read this first)

- **~98.6% of Pump.fun tokens go to ~0**, ~68.67% last-trade on launch day, only ~1–2% ever
  graduate. No tool picks the next 100x in advance. Ansem/TJR-style winners ran on *attention*,
  not on anything a safety screen can detect (they often had high insider/sniper concentration).
- This tool improves your **rug-avoidance and discipline**, not your odds of finding a moonshot.
  Its edge is *surviving long enough for the rare winner to matter* — and telling you the truth
  about whether that's even working, via the ledger.
- **Discovery is minutes-to-hours late, not first-block.** There is no free "brand-new pairs"
  REST endpoint; discovery uses RugCheck `/stats/new_tokens` + Dexscreener profiles/boosts.
  A true first-block sniper needs the PumpPortal WebSocket (deliberately out of scope).

## Layout

| File | Role |
|---|---|
| `config.py` | Every threshold + weight, HTTP tunables, `load_credentials()` (reuses your vrp secrets). |
| `http_client.py` | stdlib urllib + certifi, per-host throttle, retry, gzip, disk cache. |
| `sources/dexscreener.py` | Discovery (profiles/boosts) + market enrichment (liquidity/vol/mcap/age). |
| `sources/rugcheck.py` | Discovery (`new_tokens`) + safety report → normalized gate inputs. |
| `screen.py` | **Pure/deterministic** hard gates + soft score + A-tier test. |
| `ledger.py` | Track-record CSV: entry snapshot + forward returns (1h/6h/24h/7d). |
| `alerts.py` | macOS / ntfy / Telegram delivery + ranked body with the trade plan. |
| `run.py` | One-shot orchestrator (`--commit` / `--send`); `screen_token()` shared with the listener. |
| `listener.py` | 24/7 PumpPortal WebSocket: logs launches, screens graduations in seconds (launchd). |
| `dashboard.py` | Renders `docs/index.html` (GitHub Pages phone dashboard) from the latest scan. |
| `verify.py` | Asserts the load-bearing invariants. |

## Running it

```bash
# per-module smoke tests (print a sanity check; send/write nothing)
python3 config.py
python3 http_client.py
python3 sources/dexscreener.py
python3 sources/rugcheck.py
python3 screen.py
python3 ledger.py
python3 alerts.py

# invariants
python3 verify.py

# end-to-end
python3 run.py            # dry run: print ranked survivors, nothing sent/written
python3 run.py --commit   # write ledger + state, still no alerts
python3 run.py --send     # write AND push alerts
python3 ledger.py         # your honest scorecard, once rows have matured
```

## Insider/bundle gates + the A-tier band (v2)

Bundled launches (supply split across 10-25 fresh wallets bought in the launch slot) look
clean on top-10 concentration — that's how $ITSY/$BULLION-shaped scams passed v1. v2 adds
three gates from RugCheck report fields v1 ignored, all free:

- **`insiderNetworks`** — RugCheck's funding-graph clustering (the research-validated #1
  bundle signal). Reject if clustered networks hold > `INSIDER_NETWORK_MAX_PCT` of supply.
- **`graphInsidersDetected`** — reject if > `GRAPH_INSIDERS_MAX` wallets cluster as insiders.
- **`creatorTokens`** — serial-deployer history. Reject creators with too many prior
  launches or a mostly-dead track record (scam factories).

**Alerts now fire ONLY for A-tier** tokens (every gate + `HC_*` thresholds: zero insider
networks, fresh creator, safe-band top10/holders/liquidity, score ≥ 70). B-tier survivors
are silently ledgered as a control group — `python3 ledger.py` prints an A-vs-B scorecard.
If A doesn't beat B, the band is not adding signal; loosen or rethink it.

**A-tier means best *survival* odds, not predicted ROI.** The research is blunt: insider
presence predicts dumps, not pumps; ~84% of graduated pump.fun tokens were high-risk and
60% collapsed within 20 minutes of migrating. No screen finds "the next 100x" in advance.

## Exit alerts + the S-tier experiment (v3)

**Exit alerts.** Every scan now checks OPEN alerted positions against the plan printed in
their entry alert: crossing a TP-ladder multiple (2x/5x/10x) or breaching the -50% hard
stop pushes an EXITS alert — each level fires exactly once. Entries are probabilistic;
exits are mechanical. This is where returns get realized instead of round-tripping.

**S-tier (smart-money copy-watch, EXPERIMENT).** Add wallets to `smart_wallets.json`;
the listener subscribes to their trades and alerts when one buys a coin that passes the
hard rug+insider gates (maturity band deliberately not required — early entry is the
point of the experiment). Tracked in a local-only `data/ledger_s.csv` scorecard.
Research basis: profitable wallets returned ~14%, bot-speed copiers ~3%; at human alert
speed it is UNPROVEN. If the S scorecard doesn't beat A/B, kill the experiment.

## Tuning

Everything is in `config.py`. Loosen/tighten a single gate (e.g. `LIQ_FLOOR_USD`,
`TOP10_MAX_PCT`, `INSIDER_MAX_PCT`, `RISK_SCORE_MAX`) or reweight `SOFT_WEIGHTS`, and the whole
screen changes with no code edits. Position discipline (`STACK_USD`, `POSITION_PCT`,
`HARD_STOP_PCT`, `TP_LADDER`) also lives there.

## Credentials

Reuses the Telegram/ntfy secrets already in `~/vrp_backtest/monitor_config.json` (same source
signal_lab borrows from). Override by creating `solana_screener/config.local.json` with
`{"ntfy_topic": "...", "telegram": {"bot_token": "...", "chat_id": "..."}}`.

## Deferred (not in v1)

- **launchd scheduling** (`com.yousefjan.solana-screener.plist`, every 30–60 min) once you trust
  the output. `ALERT_COOLDOWN_HOURS` already makes frequent runs safe from spam.
- **PumpPortal WebSocket** first-block discovery.
- **Execution layer** — only ever consider after the ledger shows a real, repeated edge.
