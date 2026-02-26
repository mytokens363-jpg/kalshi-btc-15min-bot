from __future__ import annotations

"""Internal paper-trading market-maker for Kalshi binary markets.

This is intentionally simple and read-only with respect to the exchange:
- It fetches real orderbooks from Kalshi (demo env)
- It simulates placing maker bids on YES and NO
- It simulates fills using *orderbook size decreases* at our quoted price level

No real orders are sent.

Assumptions/heuristics:
- Kalshi REST orderbook returns level arrays for each side (yes/no) as
  [[price_cents, size], ...] and is ordered best-first.
- We treat the returned levels as the *best available bid* ladder.
- A decrease in the displayed size at our price between polls is treated as
  trading volume that could have filled our resting quote.

This can be refined later (trade prints, book deltas, queue position).
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .kalshi_auth import KalshiKey
from .collectors.kalshi_rest import KalshiRestConfig, get_json

SERIES = "KXBTC15M"


@dataclass(frozen=True)
class RiskLimits:
    max_contracts_per_side_per_market: int = 10
    max_open_contracts_total: int = 50
    max_filled_contracts_per_day: int = 50


@dataclass(frozen=True)
class QuoteConfig:
    # If True: quote at best bid (more fills). If False: step back by 1c when possible.
    tight: bool = False
    # quote size per side per cycle
    quote_size: int = 1
    # minimum price in cents we will bid
    min_price: int = 1


def _now_day_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def pick_active_market(cfg: KalshiRestConfig, key: KalshiKey) -> Optional[str]:
    data = get_json(
        cfg=cfg,
        key=key,
        path="/trade-api/v2/markets",
        params={"limit": 50, "series_ticker": SERIES, "status": "open"},
    )
    markets = data.get("markets") or []
    for m in markets:
        if (m.get("status") or "").lower() == "active":
            return m.get("ticker") or m.get("market_ticker")
    if markets:
        m0 = markets[0]
        return m0.get("ticker") or m0.get("market_ticker")
    return None


def fetch_orderbook(cfg: KalshiRestConfig, key: KalshiKey, market_ticker: str) -> Dict[str, Any]:
    return get_json(cfg=cfg, key=key, path=f"/trade-api/v2/markets/{market_ticker}/orderbook")


def _best_level(levels: list) -> Optional[Tuple[int, float]]:
    if not levels:
        return None
    px, sz = levels[0]
    return int(px), float(sz)


def choose_quote_price(best_px: int, qcfg: QuoteConfig) -> int:
    if qcfg.tight:
        return max(qcfg.min_price, best_px)
    # safe: one cent behind best when possible
    return max(qcfg.min_price, best_px - 1)


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, st: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
    tmp.replace(path)


def _get_pos(st: Dict[str, Any], market: str) -> Dict[str, Any]:
    # Track both qty and cost basis so we can compute realized PnL on settlement.
    # qty: integer contracts
    # cost_cents: total cents paid for that side
    return st.setdefault("positions", {}).setdefault(
        market,
        {
            "yes_qty": 0,
            "yes_cost_cents": 0,
            "no_qty": 0,
            "no_cost_cents": 0,
        },
    )


def _open_orders(st: Dict[str, Any], market: str) -> Dict[str, Any]:
    return st.setdefault("open_orders", {}).setdefault(market, {})


def _filled_today(st: Dict[str, Any]) -> int:
    day = _now_day_key()
    d = st.setdefault("daily", {}).setdefault(day, {"filled": 0})
    return int(d.get("filled", 0))


def _inc_filled_today(st: Dict[str, Any], n: int) -> None:
    day = _now_day_key()
    d = st.setdefault("daily", {}).setdefault(day, {"filled": 0})
    d["filled"] = int(d.get("filled", 0)) + int(n)


def _utc_day_key_from_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))


def _realized_pnl_today_cents(st: Dict[str, Any], day_key: str) -> int:
    total = 0
    for r in st.get("realized", []) or []:
        try:
            ts = int(r.get("ts", 0) or 0)
            if not ts:
                continue
            if _utc_day_key_from_ts(ts) != day_key:
                continue
            total += int(r.get("pnl_cents", 0) or 0)
        except Exception:
            continue
    return int(total)


def _nonzero_position_markets(st: Dict[str, Any]) -> int:
    n = 0
    for _m, pos in (st.get("positions") or {}).items():
        try:
            if int(pos.get("yes_qty", 0) or 0) != 0 or int(pos.get("no_qty", 0) or 0) != 0:
                n += 1
        except Exception:
            continue
    return int(n)


def _open_orders_count(st: Dict[str, Any]) -> int:
    n = 0
    for _m, sides in (st.get("open_orders") or {}).items():
        if not isinstance(sides, dict):
            continue
        for s in ("yes", "no"):
            o = sides.get(s)
            if not isinstance(o, dict):
                continue
            try:
                if int(o.get("remaining", 0) or 0) > 0:
                    n += 1
            except Exception:
                continue
    return int(n)


def _update_daily_metrics(st: Dict[str, Any], mtm: Dict[str, Any]) -> None:
    """Persist lightweight daily cash/equity metrics for summary/reconciliation.

    Stored under st[daily][YYYY-MM-DD] using UTC day keys to match bot summary.
    """
    day = _now_day_key()
    d = st.setdefault("daily", {}).setdefault(day, {"filled": 0})

    cash_cents = int(st.get("cash_cents", 0) or 0)
    equity_cents = int(mtm.get("equity_cents", cash_cents) or cash_cents)
    unreal_cents = int(mtm.get("unrealized_cents", 0) or 0)

    # first seen (start-of-day snapshot)
    d.setdefault("cash_cents_first", cash_cents)
    d.setdefault("equity_cents_first", equity_cents)

    # rolling last seen
    d["cash_cents_last"] = cash_cents
    d["equity_cents_last"] = equity_cents
    d["unrealized_cents_last"] = unreal_cents

    # extrema
    d["cash_cents_min"] = min(int(d.get("cash_cents_min", cash_cents) or cash_cents), cash_cents)
    d["cash_cents_max"] = max(int(d.get("cash_cents_max", cash_cents) or cash_cents), cash_cents)

    d["realized_pnl_cents_today"] = _realized_pnl_today_cents(st, day)
    d["open_orders_count"] = _open_orders_count(st)
    d["nonzero_position_markets"] = _nonzero_position_markets(st)
    d["updated_ts"] = int(time.time())



def _total_open_contracts(st: Dict[str, Any]) -> int:
    # inventory based cap across markets
    total = 0
    for _m, pos in (st.get("positions") or {}).items():
        total += abs(int(pos.get("yes_qty", 0))) + abs(int(pos.get("no_qty", 0)))
    return total


def simulate_fills_from_size_decrease(
    *,
    st: Dict[str, Any],
    market: str,
    side: str,
    ob_levels: list,
) -> int:
    """If we have an open quote at px, and the displayed size at that px decreased,
    treat the decrease as potential fills up to our remaining.
    """

    oo = _open_orders(st, market).get(side)
    if not oo:
        return 0

    px = int(oo["price"])  # cents
    remaining = int(oo.get("remaining", 0))
    if remaining <= 0:
        return 0

    # find current size at our px
    cur_sz = None
    for p, s in ob_levels:
        if int(p) == px:
            cur_sz = float(s)
            break
    if cur_sz is None:
        # our level disappeared; assume we got filled for remaining (aggressive assumption)
        fill = remaining
    else:
        last_seen = float(oo.get("last_seen_level_size", cur_sz))
        dec = max(0.0, last_seen - cur_sz)
        fill = min(remaining, int(dec))

    if fill <= 0:
        # update last seen
        if cur_sz is not None:
            oo["last_seen_level_size"] = cur_sz
        return 0

    oo["remaining"] = remaining - fill
    if cur_sz is not None:
        oo["last_seen_level_size"] = cur_sz

    pos = _get_pos(st, market)
    if side == "yes":
        pos["yes_qty"] = int(pos.get("yes_qty", 0)) + fill
        pos["yes_cost_cents"] = int(pos.get("yes_cost_cents", 0)) + fill * px
    else:
        pos["no_qty"] = int(pos.get("no_qty", 0)) + fill
        pos["no_cost_cents"] = int(pos.get("no_cost_cents", 0)) + fill * px

    # cash: buying YES/NO costs px cents per contract.
    st["cash_cents"] = int(st.get("cash_cents", 0)) - fill * px

    _inc_filled_today(st, fill)
    st.setdefault("fills", []).append({"ts": int(time.time()), "market": market, "side": side, "px": px, "qty": fill})
    return fill


def ensure_quotes(
    *,
    st: Dict[str, Any],
    market: str,
    best_yes: Optional[Tuple[int, float]],
    best_no: Optional[Tuple[int, float]],
    limits: RiskLimits,
    qcfg: QuoteConfig,
) -> None:
    pos = _get_pos(st, market)
    oo = _open_orders(st, market)

    filled_today = _filled_today(st)
    if filled_today >= limits.max_filled_contracts_per_day:
        # kill switch: stop quoting
        oo.pop("yes", None)
        oo.pop("no", None)
        st["halted"] = True
        return

    st["halted"] = False

    # overall exposure cap
    if _total_open_contracts(st) >= limits.max_open_contracts_total:
        return

    for side, best in ("yes", best_yes), ("no", best_no):
        if not best:
            # one-sided book: do not quote
            oo.pop(side, None)
            continue

        best_px, best_sz = best
        my_pos = int(pos.get(f"{side}_qty", 0))
        if my_pos >= limits.max_contracts_per_side_per_market:
            oo.pop(side, None)
            continue

        desired_px = choose_quote_price(best_px, qcfg)
        desired_qty = min(qcfg.quote_size, limits.max_contracts_per_side_per_market - my_pos)
        if desired_qty <= 0:
            continue

        cur = oo.get(side)
        if cur and int(cur.get("price", -1)) == desired_px and int(cur.get("remaining", 0)) > 0:
            # keep
            continue

        # place/replace virtual order
        oo[side] = {
            "price": desired_px,
            "remaining": int(desired_qty),
            "placed_ts": int(time.time()),
            "last_seen_level_size": float(best_sz) if int(best_px) == int(desired_px) else 0.0,
        }


def _migrate_positions_from_legacy(st: Dict[str, Any]) -> None:
    # Legacy state used positions[market] = {yes:int, no:int} without cost.
    # We can reconstruct qty+cost from the fills ledger if present.
    pos = st.get("positions")
    if not isinstance(pos, dict) or not pos:
        return

    # If already migrated, bail.
    any_new = False
    for _m, p in pos.items():
        if isinstance(p, dict) and ("yes_qty" in p or "no_qty" in p):
            any_new = True
            break
    if any_new:
        return

    # Rebuild from fills
    rebuilt: Dict[str, Dict[str, int]] = {}
    for f in st.get("fills", []) or []:
        try:
            m = str(f["market"])
            side = str(f["side"])
            px = int(f["px"])
            qty = int(f["qty"])
        except Exception:
            continue
        r = rebuilt.setdefault(m, {"yes_qty": 0, "yes_cost_cents": 0, "no_qty": 0, "no_cost_cents": 0})
        if side == "yes":
            r["yes_qty"] += qty
            r["yes_cost_cents"] += qty * px
        elif side == "no":
            r["no_qty"] += qty
            r["no_cost_cents"] += qty * px

    st["positions"] = rebuilt


def settle_finalized_markets(cfg: KalshiRestConfig, key: KalshiKey, st: Dict[str, Any]) -> int:
    """Settle any held markets that are finalized/resolved.

    Uses /markets/{ticker}.market.result ("yes"|"no"|"") when status==finalized.
    Payout: winning side pays 100 cents per contract (notional $1).
    """
    settled = 0
    positions = st.get("positions") or {}
    for mkt, pos in list(positions.items()):
        yq = int(pos.get("yes_qty", 0))
        nq = int(pos.get("no_qty", 0))
        if yq == 0 and nq == 0:
            continue

        data = get_json(cfg=cfg, key=key, path=f"/trade-api/v2/markets/{mkt}")
        market = data.get("market") or {}
        if (market.get("status") or "").lower() != "finalized":
            continue

        result = (market.get("result") or "").lower()
        payout_cents = 0
        if result == "yes":
            payout_cents = 100 * yq
        elif result == "no":
            payout_cents = 100 * nq
        else:
            # unknown; skip
            continue

        cost_cents = int(pos.get("yes_cost_cents", 0)) + int(pos.get("no_cost_cents", 0))
        st["cash_cents"] = int(st.get("cash_cents", 0)) + payout_cents
        st.setdefault("realized", []).append({
            "ts": int(time.time()),
            "market": mkt,
            "result": result,
            "payout_cents": payout_cents,
            "cost_cents": cost_cents,
            "pnl_cents": payout_cents - cost_cents,
            "yes_qty": yq,
            "no_qty": nq,
        })

        # clear positions + orders for that market
        positions.pop(mkt, None)
        (st.get("open_orders") or {}).pop(mkt, None)
        settled += 1

    st["positions"] = positions
    return settled


def mark_to_market(cfg: KalshiRestConfig, key: KalshiKey, st: Dict[str, Any]) -> Dict[str, Any]:
    """Compute MTM using best bid levels from REST orderbook."""
    out = {"equity_cents": int(st.get("cash_cents", 0)), "unrealized_cents": 0, "lines": []}
    for mkt, pos in (st.get("positions") or {}).items():
        yq = int(pos.get("yes_qty", 0))
        nq = int(pos.get("no_qty", 0))
        if yq == 0 and nq == 0:
            continue

        ob = fetch_orderbook(cfg, key, mkt)
        book = ob.get("orderbook") or {}
        yes_bid = _best_level(book.get("yes") or [])
        no_bid = _best_level(book.get("no") or [])
        y_bid_px = int(yes_bid[0]) if yes_bid else 0
        n_bid_px = int(no_bid[0]) if no_bid else 0

        mtm_cents = yq * y_bid_px + nq * n_bid_px
        cost_cents = int(pos.get("yes_cost_cents", 0)) + int(pos.get("no_cost_cents", 0))
        unreal = mtm_cents - cost_cents

        out["equity_cents"] += mtm_cents
        out["unrealized_cents"] += unreal
        out["lines"].append({
            "market": mkt,
            "yes_qty": yq,
            "no_qty": nq,
            "yes_bid": y_bid_px,
            "no_bid": n_bid_px,
            "mtm_cents": mtm_cents,
            "unrealized_cents": unreal,
        })

    return out


def run_once(
    *,
    env: str,
    key: KalshiKey,
    state_path: Path,
    limits: RiskLimits,
    qcfg: QuoteConfig,
) -> str:
    cfg = KalshiRestConfig(env=env)
    st = load_state(state_path)
    st.setdefault("cash_cents", 25000)  # default $250

    _migrate_positions_from_legacy(st)

    # A) settlement for finalized markets
    settled = settle_finalized_markets(cfg, key, st)

    mkt = pick_active_market(cfg, key)
    if not mkt:
        save_state(state_path, st)
        return "NO_MARKET"

    ob = fetch_orderbook(cfg, key, mkt)
    book = ob.get("orderbook") or {}
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []

    # simulate fills first
    fills_yes = simulate_fills_from_size_decrease(st=st, market=mkt, side="yes", ob_levels=yes_levels)
    fills_no = simulate_fills_from_size_decrease(st=st, market=mkt, side="no", ob_levels=no_levels)

    # quote only when both sides exist
    best_yes = _best_level(yes_levels)
    best_no = _best_level(no_levels)
    if not best_yes or not best_no:
        _open_orders(st, mkt).pop("yes", None)
        _open_orders(st, mkt).pop("no", None)
        save_state(state_path, st)
        return "ONE_SIDED"

    ensure_quotes(st=st, market=mkt, best_yes=best_yes, best_no=best_no, limits=limits, qcfg=qcfg)

    # B) MTM
    mtm = mark_to_market(cfg, key, st)

    # Persist daily metrics for later reconciliation (cash/equity/unrealized/etc.)
    _update_daily_metrics(st, mtm)

    save_state(state_path, st)

    cash = int(st.get("cash_cents", 0)) / 100.0
    eq = int(mtm["equity_cents"]) / 100.0
    unrl = int(mtm["unrealized_cents"]) / 100.0
    pos = _get_pos(st, mkt)
    oo = _open_orders(st, mkt)

    return (
        f"PAPER_MM {mkt} cash=${cash:.2f} equity=${eq:.2f} unrl=${unrl:.2f} "
        f"pos_yes={pos.get('yes_qty',0)} pos_no={pos.get('no_qty',0)} fills(+){fills_yes+fills_no} settled(+){settled} "
        f"oo_yes={oo.get('yes',{})} oo_no={oo.get('no',{})}"
    )
