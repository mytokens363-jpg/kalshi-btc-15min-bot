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


def main() -> None:
    st = load_state()
    today = today_key()

    daily = (st.get("daily") or {}).get(today, {})
    cash_start = daily.get("cash_cents_first", st.get("cash_cents", 24770))
    cash_now   = daily.get("cash_cents_last",  st.get("cash_cents", 24770))
    realized   = daily.get("realized_pnl_cents_today", 0)
    halted     = st.get("halted", False)

    placed, filled = count_today_orders()

    pnl_sign = "+" if realized >= 0 else ""
    pnl_str  = f"{pnl_sign}${realized/100:.2f}"

    lines = [
        f"📊 <b>Kalshi Daily Summary</b> — {today}",
        f"",
        f"💰 Cash:     ${cash_now/100:.2f}",
        f"📈 P&L:      {pnl_str}",
        f"📋 Orders:   {placed} placed / {filled} filled",
    ]

    if halted:
        lines.append("🛑 Status:   HALTED (daily loss limit hit)")
    else:
        lines.append("✅ Status:   Running")

    tg("\n".join(lines))
    print("Summary sent.")


if __name__ == "__main__":
    main()
