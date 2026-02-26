#!/usr/bin/env python3
"""Summarize internal paper-MM results from state/paper_mm_state.json.

This is read-only and does not call Kalshi.

Usage:
  python ./scripts/paper_mm_summary.py --state state/paper_mm_state.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/paper_mm_state.json")
    ap.add_argument("--last", type=int, default=8, help="show last N fills/realized entries")
    args = ap.parse_args()

    p = Path(args.state)
    if not p.exists():
        print("No state file yet.")
        return 0

    st = json.loads(p.read_text())

    cash_cents = int(st.get("cash_cents", 0))
    cash = cash_cents / 100.0

    daily = st.get("daily", {}) or {}
    day_keys = sorted(daily.keys())
    today_key = day_keys[-1] if day_keys else None
    filled_today = int(daily.get(today_key, {}).get("filled", 0)) if today_key else 0

    # Optional richer daily metrics (written by paper_mm)
    d_today = daily.get(today_key, {}) if today_key else {}
    cash_first = d_today.get("cash_cents_first")
    cash_last = d_today.get("cash_cents_last")
    equity_last = d_today.get("equity_cents_last")
    unreal_last = d_today.get("unrealized_cents_last")
    realized_today = d_today.get("realized_pnl_cents_today")
    open_orders_count = d_today.get("open_orders_count")
    nonzero_pos_mkts = d_today.get("nonzero_position_markets")

    realized = st.get("realized", []) or []
    realized_pnl_cents = sum(int(x.get("pnl_cents", 0)) for x in realized)

    positions = st.get("positions", {}) or {}
    open_markets = len(positions)

    print(f"Kalshi PAPER-MM summary")
    print(f"- cash: ${cash:.2f}")

    if today_key and cash_first is not None and cash_last is not None:
        print(f"- cash (today start→last): ${int(cash_first)/100:.2f} → ${int(cash_last)/100:.2f}")
    if today_key and equity_last is not None:
        eq = int(equity_last)/100.0
        un = int(unreal_last)/100.0 if unreal_last is not None else 0.0
        print(f"- est equity (last): ${eq:.2f} (unrealized ${un:.2f})")
    if today_key and realized_today is not None:
        print(f"- realized PnL (today): ${int(realized_today)/100.0:.2f}")
    if today_key and open_orders_count is not None:
        print(f"- open orders (working): {int(open_orders_count)}")
    if today_key and nonzero_pos_mkts is not None:
        print(f"- markets w/ inventory: {int(nonzero_pos_mkts)}")
    print(f"- realized PnL (all-time): ${realized_pnl_cents/100.0:.2f} (n={len(realized)})")
    if today_key:
        print(f"- filled today ({today_key} UTC): {filled_today}")
    print(f"- open markets: {open_markets}")

    if open_markets:
        print("- positions (by market):")
        for mkt, pos in list(positions.items())[:12]:
            yq = int(pos.get("yes_qty", 0))
            nq = int(pos.get("no_qty", 0))
            yc = int(pos.get("yes_cost_cents", 0))
            nc = int(pos.get("no_cost_cents", 0))
            print(f"  • {mkt}: YES {yq} (cost ${yc/100:.2f}) | NO {nq} (cost ${nc/100:.2f})")
        if open_markets > 12:
            print(f"  … +{open_markets-12} more")

    if realized:
        print("- last realized:")
        for r in realized[-args.last:]:
            print(
                f"  • {r.get('market')} result={r.get('result')} pnl=${int(r.get('pnl_cents',0))/100:.2f} "
                f"(payout=${int(r.get('payout_cents',0))/100:.2f} cost=${int(r.get('cost_cents',0))/100:.2f})"
            )

    fills = st.get("fills", []) or []
    if fills:
        print("- last fills:")
        for f in fills[-args.last:]:
            print(f"  • {f.get('market')} {f.get('side')} {f.get('qty')} @ {f.get('px')}c")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
