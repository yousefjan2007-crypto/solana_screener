"""
Live pump.fun listener — the event-driven fast path. Subscribes to PumpPortal's free
WebSocket (wss://pumpportal.fun/api/data) for:

  • token CREATIONS  → logged to data/launches_YYYYMMDD.jsonl (research trail: dev
    wallets, timing; no evaluation — a second-old token has no pool and no safety data).
  • MIGRATIONS (bonding-curve graduation → tradable pool) → the actionable moment. The
    token is screened through the SAME pipeline as the batch scan (run.screen_token:
    hard gates + insider gates + A-tier), seconds-to-minutes after graduation instead
    of waiting for the next 15-minute cloud scan. A-tier passes are alerted.

Division of labor: this listener is the SPEED path and keeps no ledger; the cloud scan
remains the system of record (it will independently pick up any surviving coin and
ledger it, so forward returns are still tracked). Listener-side files are gitignored so
they never conflict with the state the cloud commits back to the repo.

Long-running by design (launchd KeepAlive, com.yousefjan.solana-listener) — the one
deliberate exception to the one-shot-script idiom, because a WebSocket must stay open.
Reconnects with backoff on drops. Alerts respect a listener-local cooldown.

Run modes:
  python3 listener.py --test    connect, print events for ~60s, evaluate nothing, exit.
  python3 listener.py           evaluate migrations, DRY alerts (print only).
  python3 listener.py --send    evaluate migrations, push real alerts.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import threading
import time

import certifi
import websocket  # websocket-client

import config
import alerts
import run as pipeline
from sources import dexscreener as dex

WS_URL = "wss://pumpportal.fun/api/data"
LISTENER_STATE = os.path.join(config.DATA_DIR, "listener_state.json")
EVENTS_LOG = os.path.join(config.DATA_DIR, "listener_events.jsonl")
LAUNCH_LOG_KEEP_DAYS = 7
# Dexscreener needs a little time to index a brand-new pool; retry schedule (seconds).
ENRICH_RETRY_DELAYS = (10, 30, 60, 120)

SEND = "--send" in sys.argv
TEST = "--test" in sys.argv


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _append_jsonl(path: str, obj: dict) -> None:
    try:
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception as e:
        _log(f"jsonl write failed ({path}): {e}")


def _launch_log_path(now_s: float) -> str:
    day = time.strftime("%Y%m%d", time.gmtime(now_s))
    return os.path.join(config.DATA_DIR, f"launches_{day}.jsonl")


def _prune_launch_logs(now_s: float) -> None:
    cutoff = now_s - LAUNCH_LOG_KEEP_DAYS * 86400
    for p in glob.glob(os.path.join(config.DATA_DIR, "launches_*.jsonl")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def _load_state() -> dict:
    if os.path.exists(LISTENER_STATE):
        try:
            return json.load(open(LISTENER_STATE))
        except Exception:
            return {}
    return {}


def _cooldown_ok(mint: str, now_s: float) -> bool:
    state = _load_state()
    return now_s - float(state.get(mint, 0)) >= config.ALERT_COOLDOWN_HOURS * 3600


def _mark_alerted(mint: str, now_s: float) -> None:
    state = _load_state()
    state[mint] = now_s
    json.dump(state, open(LISTENER_STATE, "w"), indent=2)


def evaluate_migration(mint: str, symbol: str) -> None:
    """Runs in its own thread: wait for the pool to be indexed, then screen through the
    exact same gates as the batch scan. Alert only on A-tier."""
    m = None
    for delay in ENRICH_RETRY_DELAYS:
        time.sleep(delay)
        m = dex.forward_snapshot(mint, now_s=time.time())
        if m and m.get("liq_usd", 0) > 0:
            break
    if not m:
        _log(f"  {symbol}: no Dexscreener pair after "
             f"{sum(ENRICH_RETRY_DELAYS)}s — skipped")
        return

    row = pipeline.screen_token(mint, m)
    now_s = time.time()
    verdict = ("REJECTED (hard gates)" if row is None
               else f"tier {row['tier']} score {row['score']:.0f}")
    _log(f"  {symbol}: graduated → {verdict}")
    _append_jsonl(EVENTS_LOG, {"ts": now_s, "mint": mint, "symbol": symbol,
                               "verdict": verdict,
                               "hc_misses": (row or {}).get("hc_misses", [])})
    if row is None or row["tier"] != "A":
        return
    if not _cooldown_ok(mint, now_s):
        _log(f"  {symbol}: A-tier but within cooldown — not re-alerted")
        return
    title, body = alerts.format_alert([row])
    title = title.replace("A-TIER", "A-TIER fresh graduation")
    alerts.send_all(title, body, dry_run=not SEND)
    _mark_alerted(mint, now_s)


def on_message(ws, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except Exception:
        return
    tx = str(msg.get("txType", "")).lower()
    mint = msg.get("mint")
    now_s = time.time()
    if not mint:
        return  # subscription acks etc.
    if tx == "create":
        _append_jsonl(_launch_log_path(now_s), {
            "ts": now_s, "mint": mint, "symbol": msg.get("symbol", "?"),
            "name": msg.get("name", ""), "dev": msg.get("traderPublicKey", ""),
            "pool": msg.get("pool", ""),
        })
        if TEST:
            _log(f"create   {msg.get('symbol', '?'):12.12s} {mint}")
    elif tx == "migrate":
        sym = msg.get("symbol", "?")
        _log(f"MIGRATE  {sym:12.12s} {mint}" + ("" if not TEST else " (test: not evaluated)"))
        if not TEST:
            threading.Thread(target=evaluate_migration, args=(mint, sym),
                             daemon=True).start()


# Same certifi idiom as every urllib call in this workspace — the system certs fail
# with CERTIFICATE_VERIFY_FAILED, and websocket-client defaults to them.
_SSLOPT = {"ca_certs": certifi.where()}


def on_error(ws, err) -> None:
    _log(f"socket error: {err}")


def on_open(ws) -> None:
    ws.send(json.dumps({"method": "subscribeNewToken"}))
    ws.send(json.dumps({"method": "subscribeMigration"}))
    _log(f"connected — subscribed to creations + migrations "
         f"({'TEST' if TEST else 'SEND' if SEND else 'DRY'} mode)")


def main() -> None:
    _prune_launch_logs(time.time())
    if TEST:
        ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                    on_error=on_error)
        t = threading.Thread(target=lambda: ws.run_forever(sslopt=_SSLOPT), daemon=True)
        t.start()
        time.sleep(60)
        ws.close()
        _log("test window over — exiting")
        return
    backoff = 1
    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                        on_error=on_error)
            ws.run_forever(ping_interval=30, ping_timeout=10, sslopt=_SSLOPT)
        except Exception as e:
            _log(f"socket error: {e}")
        _log(f"disconnected — reconnecting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
