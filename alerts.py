"""
Alert delivery for solana_screener — the notify primitives (macOS Notification Center,
ntfy.sh, Telegram) copied from vrp_backtest/monitor.py & signal_lab/alerts.py, same
certifi SSL idiom. Credentials via config.load_credentials() (your shared vrp secrets),
never hardcoded.

format_alert() builds a ranked body that ships a pre-computed size / hard-stop /
take-profit ladder per token, so discipline is decided at alert time — plus the honesty
footer. ALERT-ONLY: nothing here places an order.
"""
from __future__ import annotations

import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

import certifi

import config

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def macos_notify(title: str, body: str) -> None:
    title = title.replace('"', '\\"')
    body = body.replace('"', '\\"').replace("\n", " — ")
    script = f'display notification "{body}" with title "{title}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass  # not on macOS (e.g. a Linux CI runner) — osascript absent; skip silently


def ntfy_notify(topic: str, title: str, body: str) -> None:
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    req.add_header("Tags", "chart_with_upwards_trend,rotating_light")
    try:
        urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
    except Exception as e:
        print(f"[ntfy error] {e}", file=sys.stderr)


def telegram_notify(bot_token: str, chat_id: str, title: str, body: str) -> None:
    if not bot_token or not chat_id:
        return

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = f"<b>{esc(title)}</b>\n<pre>{esc(body)}</pre>"
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text,
                                   "parse_mode": "HTML"}).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data),
                               timeout=10, context=_SSL_CTX)
    except Exception as e:
        print(f"[telegram error] {e}", file=sys.stderr)


def _plan(entry_price: float) -> tuple[float, float, str]:
    """Pre-computed discipline for a single token: fixed tiny size, hard stop, TP ladder."""
    size = min(config.STACK_USD * config.POSITION_PCT,
               config.STACK_USD / config.MAX_CONCURRENT)
    stop = entry_price * (1 - config.HARD_STOP_PCT)
    tps = ", ".join(f"{m:g}x→sell {int(f*100)}%" for m, f in config.TP_LADDER)
    return size, stop, tps


def format_alert(survivors: list[dict]) -> tuple[str, str]:
    """survivors: ranked list of market dicts carrying a 'score'. Returns (title, body)."""
    title = f"solana_screener: {len(survivors)} candidate(s) passed the rug gates"
    blocks = []
    for s in survivors:
        size, stop, tps = _plan(s.get("price_usd", 0.0))
        blocks.append(
            f"⚡ {s.get('symbol', '?')}  score {s.get('score', 0):.0f}/100\n"
            f"   price ${s.get('price_usd', 0):.6g}  mcap ${s.get('mcap', 0):,.0f}  "
            f"liq ${s.get('liq_usd', 0):,.0f}  age {s.get('pair_age_min', 0):.0f}m\n"
            f"   holders {s.get('total_holders', '?')}  top10 {s.get('top10_pct', '?')}%  "
            f"vol24 ${s.get('vol_h24', 0):,.0f}\n"
            f"   PLAN: buy ~${size:.0f}  hard-stop ${stop:.6g} "
            f"(-{config.HARD_STOP_PCT*100:.0f}%)  TP {tps}\n"
            f"   {s.get('url', '')}"
        )
    body = "\n\n".join(blocks) + "\n\n" + config.FOOTER
    return title, body


def send_all(title: str, body: str, dry_run: bool = True) -> None:
    """Send to every configured channel. dry_run just prints to the console."""
    print(f"\n=== ALERT {'(DRY RUN — not sent)' if dry_run else '(SENDING)'} ===")
    print(title)
    print(body)
    if dry_run:
        return
    creds = config.load_credentials()
    macos_notify(title, body)
    ntfy_notify(creds.get("ntfy_topic"), title, body)
    tg = creds.get("telegram") or {}
    telegram_notify(tg.get("bot_token"), tg.get("chat_id"), title, body)


if __name__ == "__main__":
    demo = [{
        "symbol": "WIF2", "score": 72.0, "price_usd": 0.00042, "mcap": 180_000,
        "liq_usd": 45_000, "vol_h24": 120_000, "pair_age_min": 180,
        "total_holders": 1400, "top10_pct": 18.0,
        "url": "https://dexscreener.com/solana/EXAMPLEPAIRADDRESS",
    }]
    t, b = format_alert(demo)
    send_all(t, b, dry_run=True)
    print("\n(creds present: %s)" % {k: bool(v) for k, v in config.load_credentials().items()})
