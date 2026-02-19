#!/usr/bin/env python3
"""DEMO-only order submit + cancel round-trip for Kalshi.

This is a safety/plumbing test before enabling any automation.

It places a tiny POST-ONLY limit BUY on one side at a low price to reduce
fill likelihood, then immediately cancels.

Env vars:
  KALSHI_ACCESS_KEY_ID
  KALSHI_PRIVATE_KEY_PATH

Example:
  source /root/.secrets/kalshi_demo.env
  source .venv/bin/activate
  PYTHONPATH=src python ./scripts/demo_submit_cancel.py --env demo --ticker KXBTC15M-... --side yes --price 1 --count 1
"""

from __future__ import annotations

import argparse
import os
import time

from kalshi_bot.kalshi_auth import KalshiKey
from kalshi_bot.collectors.kalshi_rest import KalshiRestConfig, get_json
from kalshi_bot.collectors.kalshi_venue import KalshiConfig, KalshiVenue


def _env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["demo", "prod"], default="demo")
    ap.add_argument("--ticker", default="", help="market ticker (omit to auto-pick active KXBTC15M)")
    ap.add_argument("--side", choices=["yes", "no"], default="yes")
    ap.add_argument("--action", choices=["buy", "sell"], default="buy")
    ap.add_argument("--price", type=int, default=1, help="price in cents (1-99)")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--post-only", action="store_true", default=True)
    args = ap.parse_args()

    if args.env != "demo":
        raise SystemExit("Refusing to run: demo_submit_cancel.py is DEMO-only")

    key = KalshiKey(
        access_key_id=_env_required("KALSHI_ACCESS_KEY_ID"),
        private_key_path=_env_required("KALSHI_PRIVATE_KEY_PATH"),
    )

    # pick active market if ticker not provided
    ticker = args.ticker
    if not ticker:
        rest_cfg = KalshiRestConfig(env="demo")
        data = get_json(
            cfg=rest_cfg,
            key=key,
            path="/trade-api/v2/markets",
            params={"limit": 50, "series_ticker": "KXBTC15M", "status": "open"},
        )
        markets = data.get("markets") or []
        for m in markets:
            if (m.get("status") or "").lower() == "active":
                ticker = m.get("ticker") or m.get("market_ticker")
                break
        if not ticker and markets:
            m0 = markets[0]
            ticker = m0.get("ticker") or m0.get("market_ticker")

    if not ticker:
        raise SystemExit("No market ticker provided and could not auto-pick an active KXBTC15M market")

    v = KalshiVenue(KalshiConfig(env="demo"), key)

    client_order_id = f"demo_{int(time.time()*1000)}"

    body = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": args.side,
        "action": args.action,
        "count": args.count,
        # price fields: set the appropriate one
        ("yes_price" if args.side == "yes" else "no_price"): args.price,
        "type": "limit",
        "post_only": bool(args.post_only),
        # good til cancelled to keep it resting until we cancel
        "time_in_force": "good_till_canceled",
    }

    print("SUBMIT", {k: body[k] for k in body if k != "client_order_id"}, "client_order_id=...", flush=True)
    order_id = v.submit_order(body)
    print("ORDER_ID", order_id, flush=True)

    # cancel immediately
    v.cancel_order(order_id)
    print("CANCEL_OK", order_id, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
