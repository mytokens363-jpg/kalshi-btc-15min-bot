from __future__ import annotations

"""Live market-maker for Kalshi BTC 15-min markets.

Sends REAL orders to Kalshi production API.
HARD risk guards — cannot be overridden at runtime.

Risk limits:
  - Max 5 contracts per order
  - Max $25 notional per market (250 contracts @ $0.10)
  - Max $250 total exposure across all markets
  - Daily loss limit: $50 — halts all trading if breached

Usage:
    source /root/.secrets/kalshi_prod.env
    python scripts/run_live_mm.py --confirm-live
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .kalshi_auth import KalshiKey
from .collectors.kalshi_rest import KalshiRestConfig, get_json
import urllib.parse

from .paper_mm import (
    SERIES,
    QuoteConfig,
    _best_level,
    _now_day_key,
    choose_quote_price,
    fetch_orderbook,
    load_state,
    pick_active_market,
    save_state,
)

# ── BTC price feed ───────────────────────────────────────────────────────────

def get_btc_mid() -> Optional[float]:
    """Fetch BTC mid price — Binance primary, Coinbase fallback (Binance geo-blocked on VPS)."""
    # Try Coinbase first (works on VPS; Binance returns 451 geo-block)
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
            return float(d["data"]["amount"])
    except Exception:
        pass
    # Fallback: CoinGecko
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
            return float(d["bitcoin"]["usd"])
    except Exception:
        return None


def btc_directional_bias(state_path: Path) -> Optional[str]:
    """Compare current BTC price to price 15min ago. Returns 'up', 'down', or None."""
    mid = get_btc_mid()
    if mid is None:
        return None

    # Load/update price history
    price_log = state_path.parent / "btc_price_log.json"
    history: list = []
    if price_log.exists():
        try:
            history = json.loads(price_log.read_text())
        except Exception:
            history = []

    now_ts = int(time.time())
    history.append({"ts": now_ts, "mid": mid})
    # Keep last 20 minutes of data
    history = [h for h in history if now_ts - h["ts"] < 1200]
    price_log.write_text(json.dumps(history))

    # Find price ~15 min ago
    target_ts = now_ts - 900
    old_entry = min(history, key=lambda h: abs(h["ts"] - target_ts), default=None)
    if not old_entry or abs(old_entry["ts"] - target_ts) > 180:
        return None  # Not enough history yet

    pct_change = (mid - old_entry["mid"]) / old_entry["mid"] * 100
    if pct_change > 0.15:
        return "up"
    elif pct_change < -0.15:
        return "down"
    return None  # flat


# ── Hard risk constants (cannot be changed without code edit) ─────────────────

HARD_MAX_CONTRACTS_PER_ORDER = 5
HARD_MAX_NOTIONAL_PER_MARKET_CENTS = 2500   # $25
HARD_MAX_TOTAL_EXPOSURE_CENTS = 25000        # $250
HARD_DAILY_LOSS_LIMIT_CENTS = 5000           # $50

# ── Telegram ──────────────────────────────────────────────────────────────────

_TG_TOKEN = "8260461432:AAFrRoqb7C3XcK4h-F2Pi368LJlWdaOxkqE"
_TG_CHAT = 1054649761


def _tg_send(text: str) -> None:
    payload = json.dumps({"chat_id": _TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print(f"[tg error] {e}")


# ── State helpers ─────────────────────────────────────────────────────────────

def _daily(st: Dict[str, Any]) -> Dict[str, Any]:
    day = _now_day_key()
    return st.setdefault("daily", {}).setdefault(day, {"filled": 0, "realized_pnl_cents": 0})


def _realized_loss_today_cents(st: Dict[str, Any]) -> int:
    d = _daily(st)
    pnl = int(d.get("realized_pnl_cents", 0))
    return -pnl if pnl < 0 else 0


def _total_exposure_cents(st: Dict[str, Any]) -> int:
    """Sum of all open position cost basis."""
    total = 0
    for pos in (st.get("positions") or {}).values():
        total += int(pos.get("yes_cost_cents", 0))
        total += int(pos.get("no_cost_cents", 0))
    return total


def _market_exposure_cents(st: Dict[str, Any], market: str) -> int:
    pos = (st.get("positions") or {}).get(market, {})
    return int(pos.get("yes_cost_cents", 0)) + int(pos.get("no_cost_cents", 0))


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(
    *,
    cfg: KalshiRestConfig,
    key: KalshiKey,
    market_ticker: str,
    side: str,          # 'yes' or 'no'
    price_cents: int,
    count: int,
) -> Optional[Dict[str, Any]]:
    """Place a limit order. Returns order dict or None on failure."""
    count = min(count, HARD_MAX_CONTRACTS_PER_ORDER)
    if count < 1:
        return None

    body = {
        "ticker": market_ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": "limit",
        "yes_price": price_cents if side == "yes" else (100 - price_cents),
        "expiration_ts": int(time.time()) + 60,
    }

    from .kalshi_auth import rest_auth_headers
    headers = rest_auth_headers(key, "POST", "/trade-api/v2/portfolio/orders")
    headers["Content-Type"] = "application/json"

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{cfg.base_url}/trade-api/v2/portfolio/orders",  # cfg.base_url uses env='prod'
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("order", {})
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"[place_order error] {e.code}: {err}")
        # Only alert on non-transient errors (not 429/503)
        if e.code not in (429, 503, 504):
            pass  # errors logged only
        return None
    except Exception as e:
        print(f"[place_order error] {e}")
        return None  # Silent — transient network errors, don't spam


def cancel_order(*, cfg: KalshiRestConfig, key: KalshiKey, order_id: str) -> bool:
    from .kalshi_auth import rest_auth_headers
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = rest_auth_headers(key, "DELETE", path)
    req = urllib.request.Request(
        f"{cfg.base_url}{path}",
        headers=headers,
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"[cancel_order error] {e}")
        return False


def get_order_status(*, cfg: KalshiRestConfig, key: KalshiKey, order_id: str) -> Optional[Dict[str, Any]]:
    from .kalshi_auth import rest_auth_headers
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = rest_auth_headers(key, "GET", path)
    req = urllib.request.Request(f"{cfg.base_url}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("order", {})
    except Exception:
        return None


# ── Order log ─────────────────────────────────────────────────────────────────

def log_order(log_path: Path, entry: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps({**entry, "ts": int(time.time())}) + "\n")


# ── Main tick ─────────────────────────────────────────────────────────────────

def run_once(
    *,
    cfg: KalshiRestConfig,
    key: KalshiKey,
    state_path: Path,
    log_path: Path,
    qcfg: QuoteConfig,
) -> Dict[str, Any]:
    """Run one tick of the live market-maker. Returns summary dict."""
    st = load_state(state_path)
    summary: Dict[str, Any] = {"placed": 0, "cancelled": 0, "fills_detected": 0, "errors": []}

    # ── Daily loss halt check ─────────────────────────────────────────────────
    loss = _realized_loss_today_cents(st)
    if loss >= HARD_DAILY_LOSS_LIMIT_CENTS:
        msg = f"🛑 Kalshi live MM HALTED — daily loss ${loss/100:.2f} exceeds ${HARD_DAILY_LOSS_LIMIT_CENTS/100:.0f} limit"
        print(msg)
        pass  # halt logged, daily summary reports it
        save_state(state_path, st)
        return {**summary, "halted": True, "reason": "daily_loss_limit"}

    # ── BTC directional bias ─────────────────────────────────────────────────
    bias = btc_directional_bias(state_path)
    summary["btc_bias"] = bias

    # ── Find active market ────────────────────────────────────────────────────
    market = pick_active_market(cfg, key)
    if not market:
        print("[live_mm] No active market found")
        save_state(state_path, st)
        return {**summary, "halted": False}

    # ── Cancel stale open orders ──────────────────────────────────────────────
    open_orders = st.setdefault("open_orders", {}).setdefault(market, {})
    for side in ("yes", "no"):
        oo = open_orders.get(side)
        if oo and oo.get("order_id"):
            order = get_order_status(cfg=cfg, key=key, order_id=oo["order_id"])
            if order is None or order.get("status") in ("cancelled", "filled", "expired"):
                # Detect fills
                filled = int(order.get("filled_count", 0)) if order else 0
                remaining_before = int(oo.get("remaining", 0))
                new_fills = max(0, remaining_before - int(order.get("remaining_count", remaining_before))) if order else 0
                if new_fills > 0:
                    price = int(oo.get("price", 0))
                    cost = new_fills * price
                    pos = st.setdefault("positions", {}).setdefault(market, {
                        "yes_qty": 0, "yes_cost_cents": 0, "no_qty": 0, "no_cost_cents": 0
                    })
                    pos[f"{side}_qty"] = int(pos.get(f"{side}_qty", 0)) + new_fills
                    pos[f"{side}_cost_cents"] = int(pos.get(f"{side}_cost_cents", 0)) + cost
                    summary["fills_detected"] += new_fills
                    fill_msg = f"✅ Kalshi fill: {side} {new_fills}x {market} @ {price}¢"
                    print(fill_msg)
                    pass  # fill logged, reported in daily summary
                    log_order(log_path, {"event": "fill", "side": side, "count": new_fills, "price": price, "market": market})
                open_orders[side] = {}
                summary["cancelled"] += 1
            elif order.get("status") == "resting":
                # Still resting — cancel to re-quote fresh
                cancel_order(cfg=cfg, key=key, order_id=oo["order_id"])
                open_orders[side] = {}
                summary["cancelled"] += 1

    # ── Fetch orderbook ───────────────────────────────────────────────────────
    try:
        ob = fetch_orderbook(cfg, key, market)
    except Exception as e:
        print(f"[live_mm] orderbook error: {e}")
        save_state(state_path, st)
        return {**summary, "halted": False, "errors": [str(e)]}

    ob_data = ob.get("orderbook") or ob
    yes_levels = ob_data.get("yes") or []
    no_levels = ob_data.get("no") or []

    # ── Quote both sides ──────────────────────────────────────────────────────
    # Skip the unfavorable side when we have a strong directional signal
    sides_to_quote = []
    for side, levels in (("yes", yes_levels), ("no", no_levels)):
        if bias == "up" and side == "no":
            continue   # BTC trending up → skip NO quotes
        if bias == "down" and side == "yes":
            continue   # BTC trending down → skip YES quotes
        sides_to_quote.append((side, levels))

    for side, levels in sides_to_quote:
        best = _best_level(levels)
        if not best:
            continue
        best_px, _ = best
        quote_px = choose_quote_price(best_px, qcfg)
        if quote_px < 1 or quote_px > 99:
            continue

        # Risk checks
        mkt_exposure = _market_exposure_cents(st, market)
        total_exposure = _total_exposure_cents(st)
        order_cost = qcfg.quote_size * quote_px

        if mkt_exposure + order_cost > HARD_MAX_NOTIONAL_PER_MARKET_CENTS:
            continue
        if total_exposure + order_cost > HARD_MAX_TOTAL_EXPOSURE_CENTS:
            continue

        # Place order
        order = place_order(
            cfg=cfg, key=key,
            market_ticker=market,
            side=side,
            price_cents=quote_px,
            count=qcfg.quote_size,
        )
        if order and order.get("order_id"):
            open_orders[side] = {
                "order_id": order["order_id"],
                "price": quote_px,
                "remaining": qcfg.quote_size,
                "placed_ts": int(time.time()),
            }
            summary["placed"] += 1
            log_order(log_path, {"event": "placed", "side": side, "price": quote_px, "count": qcfg.quote_size, "market": market, "order_id": order["order_id"]})

    save_state(state_path, st)
    return {**summary, "halted": False, "market": market}
