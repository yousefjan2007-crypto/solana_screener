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
| `screen.py` | **Pure/deterministic** hard gates + soft score. |
| `ledger.py` | Track-record CSV: entry snapshot + forward returns (1h/6h/24h/7d). |
| `alerts.py` | macOS / ntfy / Telegram delivery + ranked body with the trade plan. |
| `run.py` | One-shot orchestrator (`--commit` / `--send`). |
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
