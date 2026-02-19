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
    return st.setdefault("positions", {}).setdefault(market, {"yes": 0, "no": 0})


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


def _total_open_contracts(st: Dict[str, Any]) -> int:
    # inventory based cap across markets
    total = 0
    for m, pos in (st.get("positions") or {}).items():
        total += abs(int(pos.get("yes", 0))) + abs(int(pos.get("no", 0)))
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
    pos[side] = int(pos.get(side, 0)) + fill

    # cash: buying YES/NO costs px cents per contract. Selling not implemented in v0.
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
        my_pos = int(pos.get(side, 0))
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

    mkt = pick_active_market(cfg, key)
    if not mkt:
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
        # clear quotes in one-sided books
        _open_orders(st, mkt).pop("yes", None)
        _open_orders(st, mkt).pop("no", None)
        save_state(state_path, st)
        return "ONE_SIDED"

    ensure_quotes(st=st, market=mkt, best_yes=best_yes, best_no=best_no, limits=limits, qcfg=qcfg)

    save_state(state_path, st)

    cash = int(st.get("cash_cents", 0)) / 100.0
    pos = _get_pos(st, mkt)
    oo = _open_orders(st, mkt)
    return (
        f"PAPER_MM {mkt} cash=${cash:.2f} pos_yes={pos.get('yes',0)} pos_no={pos.get('no',0)} "
        f"oo_yes={oo.get('yes',{})} oo_no={oo.get('no',{})} fills(+){fills_yes+fills_no}"
    )
