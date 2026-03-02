#!/usr/bin/env python3
"""Run one tick of the Kalshi live market-maker.

REQUIRES prod credentials and --confirm-live flag.

Usage:
    source /root/.secrets/kalshi_prod.env
    python scripts/run_live_mm.py --confirm-live
"""
from __future__ import annotations

import argparse, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshi_bot.kalshi_auth import KalshiKey
from kalshi_bot.collectors.kalshi_rest import KalshiRestConfig
from kalshi_bot.paper_mm import QuoteConfig
from kalshi_bot.live_mm import run_once

PROD_BASE_URL = "https://api.elections.kalshi.com"


def _env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi live market-maker")
    ap.add_argument("--confirm-live", action="store_true", required=True,
                    help="REQUIRED safety flag to confirm live trading intent")
    ap.add_argument("--tight", action="store_true", help="Quote at best bid (more fills)")
    ap.add_argument("--size", type=int, default=1, help="Contracts per order (max 5)")
    args = ap.parse_args()

    print("=" * 60)
    print("  ⚡ KALSHI LIVE MARKET-MAKER")
    print("  REAL MONEY — REAL ORDERS")
    print("=" * 60)

    key = KalshiKey(
        access_key_id=_env("KALSHI_PROD_ACCESS_KEY_ID"),
        private_key_path=_env("KALSHI_PROD_PRIVATE_KEY_PATH"),
    )
    cfg = KalshiRestConfig(env="prod")
    qcfg = QuoteConfig(tight=args.tight, quote_size=min(args.size, 5))

    repo_root = Path(__file__).resolve().parents[1]
    state_path = repo_root / "state" / "live_mm_state.json"
    log_path = repo_root / "logs" / "live_orders.jsonl"

    result = run_once(cfg=cfg, key=key, state_path=state_path, log_path=log_path, qcfg=qcfg)

    print(f"Result: {result}")
    if result.get("halted"):
        print(f"HALTED: {result.get('reason')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
