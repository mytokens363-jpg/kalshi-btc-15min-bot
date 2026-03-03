#!/usr/bin/env python3
"""Send daily Kalshi MM summary to Telegram at 8PM ET."""
from __future__ import annotations

import json, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TG_TOKEN = "8260461432:AAFrRoqb7C3XcK4h-F2Pi368LJlWdaOxkqE"
TG_CHAT  = 1054649761

STATE_PATH = Path(__file__).resolve().parents[1] / "state" / "live_mm_state.json"
LOG_PATH   = Path(__file__).resolve().parents[1] / "logs" / "live_orders.jsonl"


def tg(text: str) -> None:
    payload = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def today_key() -> str:
    # ET offset: UTC-5 (EST)
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 5 * 3600))


def count_today_orders() -> tuple[int, int]:
    """Returns (placed, filled) from order log for today."""
    today = today_key()
    placed = filled = 0
    if not LOG_PATH.exists():
        return 0, 0
    with open(LOG_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
                ts = r.get("ts", 0)
                day = time.strftime("%Y-%m-%d", time.gmtime(ts - 5 * 3600))
                if day != today:
                    continue
                if r.get("event") == "placed":
                    placed += 1
                elif r.get("event") == "fill":
                    filled += r.get("count", 0)
            except Exception:
                continue
    return placed, filled


def fetch_live_balance() -> dict:
    """Pull real account data from Kalshi API."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from kalshi_bot.kalshi_auth import KalshiKey
        from kalshi_bot.collectors.kalshi_rest import KalshiRestConfig, get_json
        key = KalshiKey(
            access_key_id="ca30aa24-7fbb-4f8c-abbe-2bbc6d6bc48c",
            private_key_path="/root/.secrets/kalshi_prod.pem",
        )
        cfg = KalshiRestConfig(env="prod")
        bal = get_json(cfg=cfg, key=key, path="/trade-api/v2/portfolio/balance")
        orders = get_json(cfg=cfg, key=key, path="/trade-api/v2/portfolio/orders", params={"status": "resting"})
        return {
            "cash_cents": bal.get("balance", 0),
            "portfolio_cents": bal.get("portfolio_value", 0),
            "resting_orders": len(orders.get("orders") or []),
        }
    except Exception as e:
        print(f"[balance fetch error] {e}")
        return {}


def main() -> None:
    st    = load_state()
    today = today_key()
    live  = fetch_live_balance()

    cash_cents     = live.get("cash_cents", 0)
    portfolio_cents = live.get("portfolio_cents", 0)
    total_cents    = cash_cents + portfolio_cents
    resting        = live.get("resting_orders", 0)

    daily    = (st.get("daily") or {}).get(today, {})
    realized = daily.get("realized_pnl_cents_today", 0)
    halted   = st.get("halted", False)

    placed, filled = count_today_orders()

    pnl_sign = "+" if realized >= 0 else ""
    pnl_str  = f"{pnl_sign}${realized/100:.2f}"

    lines = [
        f"📊 <b>Kalshi Live Summary</b> — {today}",
        f"",
        f"💰 Cash:      ${cash_cents/100:.2f}",
        f"📦 Positions: ${portfolio_cents/100:.2f}",
        f"🏦 Total:     ${total_cents/100:.2f}",
        f"📈 P&L today: {pnl_str}",
        f"📋 Orders:    {placed} placed / {filled} filled / {resting} resting",
    ]

    if halted:
        lines.append("🛑 Status:   HALTED (daily loss limit hit)")
    else:
        lines.append("✅ Status:   Running")

    tg("\n".join(lines))
    print("Summary sent.")


if __name__ == "__main__":
    main()
