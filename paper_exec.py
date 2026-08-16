"""
Paper-execution layer — measures the one thing the honest ledger cannot: EXECUTION COST.

The ledger's forward returns are frictionless (Dexscreener mid-prices). Real memecoin
fills are not: a $10 round trip through a ~$80k-liquidity pool measured ~-2% before the
price moved at all (Jupiter quotes, 2026-07-05). This module simulates the alert's own
trade plan with REAL Jupiter routing quotes at the moment each signal fires:

  entry alert → quote USDC→token for the plan's position size, record the fill
  TP alert    → quote token→USDC for the ladder fraction, record the proceeds
  stop alert  → quote token→USDC for everything left; NO ROUTE = $0 proceeds (honest:
                that is exactly the "couldn't exit a dead coin" outcome)

NO KEYS, NO FUNDS, NO TRANSACTIONS — quotes only. When the scorecard eventually turns
positive, this paper book answers the real go/no-go question for automation: does the
edge survive slippage? If paper P&L isn't repeatedly positive, an execution layer would
just be automating losses faster.

Reproducibility contract: now_s is always passed IN by the caller (never wall-clock in
here); quotes are live I/O by nature and are recorded once, never recomputed.
"""
from __future__ import annotations

import json
import os

import pandas as pd

import config
from http_client import get_json

COLUMNS = ["ts", "mint", "symbol", "tier", "side", "usd_flow", "tokens_raw_delta",
           "price_impact_pct", "note"]


def quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    """One live Jupiter routing quote (throttled/retried by http_client, never cached).
    Returns None when Jupiter has no route — for a memecoin that means no liquidity."""
    url = (f"{config.JUP_QUOTE_URL}?inputMint={input_mint}&outputMint={output_mint}"
           f"&amount={int(amount_raw)}&slippageBps={config.PAPER_SLIPPAGE_BPS}")
    d = get_json(url)
    return d if isinstance(d, dict) and d.get("outAmount") else None


def _load_positions(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return {}
    return {}


def _save_positions(pos: dict, path: str) -> None:
    json.dump(pos, open(path, "w"), indent=2)


def _append_fill(fill: dict, path: str) -> None:
    row = pd.DataFrame([{c: fill.get(c, "") for c in COLUMNS}])
    row.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def open_position(mint: str, symbol: str, tier: str, now_s: float, *,
                  ledger_path: str | None = None, positions_path: str | None = None,
                  quote_fn=quote) -> dict | None:
    """Simulate the entry the alert recommends (POSITION_PCT of STACK_USD, via USDC).
    Idempotent per mint: a re-run, or the listener and the cloud scan overlapping,
    can never double-buy. Token amounts stay in RAW units end-to-end, so P&L is pure
    USD cash flow and token decimals never enter the math."""
    ledger_path = ledger_path or config.PAPER_LEDGER_PATH
    positions_path = positions_path or config.PAPER_POSITIONS_PATH
    pos = _load_positions(positions_path)
    if mint in pos:
        return None
    usd = config.STACK_USD * config.POSITION_PCT
    q = quote_fn(config.USDC_MINT, mint, round(usd * 1e6))
    if q is None:
        _append_fill({"ts": now_s, "mint": mint, "symbol": symbol, "tier": tier,
                      "side": "buy_failed", "usd_flow": 0.0, "tokens_raw_delta": 0,
                      "note": "no route — position not opened"}, ledger_path)
        return None
    fill = {"ts": now_s, "mint": mint, "symbol": symbol, "tier": tier, "side": "buy",
            "usd_flow": -usd, "tokens_raw_delta": int(q["outAmount"]),
            "price_impact_pct": float(q.get("priceImpactPct") or 0.0), "note": ""}
    _append_fill(fill, ledger_path)
    pos[mint] = {"symbol": symbol, "tier": tier, "opened_ts": now_s, "cost_usd": usd,
                 "tokens_raw": int(q["outAmount"]),
                 "tokens_remaining_raw": int(q["outAmount"]),
                 "realized_usd": 0.0, "closed": False}
    _save_positions(pos, positions_path)
    return fill


def execute_exit(event: dict, now_s: float, *,
                 ledger_path: str | None = None, positions_path: str | None = None,
                 quote_fn=quote) -> dict | None:
    """Simulate the sell an exit alert recommends. `event` comes straight from
    ledger.update_forward: {"kind": "tp"|"stop", "mint", "symbol", "levels"?}.
    TP sells the ladder fractions OF THE ORIGINAL size (so the 10% moonbag survives a
    full ladder); stop sells everything left. Events for mints never paper-bought (e.g.
    the B control) are ignored. A stop with no Jupiter route closes at $0 — recorded
    honestly instead of imagined away; a TP with no route (API hiccup on a coin that is
    up 2x) keeps the tokens and logs the miss."""
    ledger_path = ledger_path or config.PAPER_LEDGER_PATH
    positions_path = positions_path or config.PAPER_POSITIONS_PATH
    pos = _load_positions(positions_path)
    p = pos.get(event.get("mint"))
    if p is None or p.get("closed"):
        return None
    kind = event.get("kind")
    if kind == "tp":
        frac = sum(f for _m, f in event.get("levels", []))
        sell_raw = min(int(p["tokens_raw"] * frac), p["tokens_remaining_raw"])
        side = "tp_" + "+".join(f"{m:g}x" for m, _f in event.get("levels", []))
    else:
        sell_raw, side = p["tokens_remaining_raw"], "stop"
    if sell_raw <= 0:
        return None
    q = quote_fn(event["mint"], config.USDC_MINT, sell_raw)
    if q is None and kind == "tp":
        _append_fill({"ts": now_s, "mint": event["mint"], "symbol": p["symbol"],
                      "tier": p["tier"], "side": side + "_failed", "usd_flow": 0.0,
                      "tokens_raw_delta": 0, "note": "no route at TP — tokens kept"},
                     ledger_path)
        return None
    fill = {"ts": now_s, "mint": event["mint"], "symbol": p["symbol"], "tier": p["tier"],
            "side": side, "usd_flow": int(q["outAmount"]) / 1e6 if q else 0.0,
            "tokens_raw_delta": -sell_raw,
            "price_impact_pct": float(q.get("priceImpactPct") or 0.0) if q else "",
            "note": "" if q else "NO ROUTE — dead coin, proceeds $0"}
    _append_fill(fill, ledger_path)
    p["tokens_remaining_raw"] -= sell_raw
    p["realized_usd"] += fill["usd_flow"]
    if kind == "stop" or p["tokens_remaining_raw"] <= 0:
        p["closed"] = True
    _save_positions(pos, positions_path)
    return fill


def summary(ledger_path: str | None = None, positions_path: str | None = None,
            quote_fn=quote, mark_open: bool = True) -> None:
    """The paper scorecard: realized cash + live mark of open remainders vs cost. The
    gap between `python3 ledger.py` (frictionless) and THIS is your execution cost."""
    ledger_path = ledger_path or config.PAPER_LEDGER_PATH
    positions_path = positions_path or config.PAPER_POSITIONS_PATH
    label = " (S experiment, local-only)" if positions_path == config.PAPER_POSITIONS_S_PATH else ""
    pos = _load_positions(positions_path)
    print(f"paper execution{label}: {len(pos)} position(s)")
    if not pos:
        print("  (no paper fills yet — a position opens at the next A-tier/S alert)")
        return
    inv = real = open_val = 0.0
    print(f"  {'':2s}{'symbol':12s} {'cost':>7s} {'realized':>9s} {'open val':>9s} {'P&L':>8s}  status")
    for mint, p in pos.items():
        cur = 0.0
        if mark_open and not p["closed"] and p["tokens_remaining_raw"] > 0:
            q = quote_fn(mint, config.USDC_MINT, p["tokens_remaining_raw"])
            cur = int(q["outAmount"]) / 1e6 if q else 0.0
        inv, real, open_val = inv + p["cost_usd"], real + p["realized_usd"], open_val + cur
        pnl = p["realized_usd"] + cur - p["cost_usd"]
        print(f"  {p['tier']:2s}{str(p['symbol'])[:12]:12s} {p['cost_usd']:>7.2f} "
              f"{p['realized_usd']:>9.2f} {cur:>9.2f} {pnl:>+8.2f}  "
              f"{'closed' if p['closed'] else 'open'}")
    net = real + open_val - inv
    print(f"  TOTAL invested ${inv:.2f} → realized ${real:.2f} + open ${open_val:.2f} "
          f"= net {net:+.2f} ({(real + open_val) / inv - 1.0:+.1%})")
    print("  Automation is justified ONLY if this number (not the frictionless ledger's)\n"
          "  is repeatedly positive. The gap between the two IS your execution cost.")


if __name__ == "__main__":
    # smoke test: live round trip at plan size on the most liquid pair there is, then
    # the paper scorecards (A book, and the local S book if it exists). Sends nothing.
    SOL = "So11111111111111111111111111111111111111112"
    usd = config.STACK_USD * config.POSITION_PCT
    q1 = quote(config.USDC_MINT, SOL, round(usd * 1e6))
    q2 = quote(SOL, config.USDC_MINT, int(q1["outAmount"])) if q1 else None
    if q2:
        back = int(q2["outAmount"]) / 1e6
        print(f"smoke: ${usd:.2f} USDC→SOL→USDC round trip returns ${back:.2f} "
              f"({back / usd - 1.0:+.2%} cost on a max-liquidity pair; memecoins are worse)")
    else:
        print("smoke: Jupiter quote FAILED (offline? rate-limited?)")
    print()
    summary()
    if os.path.exists(config.PAPER_POSITIONS_S_PATH):
        print()
        summary(config.PAPER_LEDGER_S_PATH, config.PAPER_POSITIONS_S_PATH)
