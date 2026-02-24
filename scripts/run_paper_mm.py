#!/usr/bin/env python3

"""Run one tick of the internal Kalshi paper market-maker.

This sends NO orders to Kalshi. It only reads orderbooks and simulates fills.

Env vars:
  KALSHI_ACCESS_KEY_ID
  KALSHI_PRIVATE_KEY_PATH

Example:
  source /root/.secrets/kalshi_demo.env
  source .venv/bin/activate
  PYTHONPATH=src python ./scripts/run_paper_mm.py --env demo
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from kalshi_bot.kalshi_auth import KalshiKey
from kalshi_bot.paper_mm import QuoteConfig, RiskLimits, run_once


def _env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["demo", "prod"], default="demo")
    ap.add_argument("--cash", type=float, default=250.0)
    ap.add_argument("--tight", action="store_true")
    args = ap.parse_args()

    if args.env != "demo":
        raise SystemExit("Refusing to run paper MM on prod")

    key = KalshiKey(
        access_key_id=_env_required("KALSHI_ACCESS_KEY_ID"),
        private_key_path=_env_required("KALSHI_PRIVATE_KEY_PATH"),
    )

    state_path = Path(__file__).resolve().parents[1] / "state" / "paper_mm_state.json"

    # Initialize cash if state does not exist
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(f'{{"cash_cents": {int(args.cash*100)} }}')

    limits = RiskLimits(
        max_contracts_per_side_per_market=20,
        max_open_contracts_total=200,
        max_filled_contracts_per_day=200,
    )
    qcfg = QuoteConfig(tight=bool(args.tight), quote_size=1)

    msg = run_once(env=args.env, key=key, state_path=state_path, limits=limits, qcfg=qcfg)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
