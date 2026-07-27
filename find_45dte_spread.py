"""
Find a short-put / long-put credit spread on SPX at ~45DTE, targeting
a short leg near -0.16 delta and a long leg near -0.10 delta —
matching the backtested 45DTE 16/10 put spread variant.

Usage:
    python find_45dte_spread.py
    python find_45dte_spread.py --short-delta -0.16 --long-delta -0.10
    python find_45dte_spread.py --dte 45
    python find_45dte_spread.py --debug

Notes:
- Use --symbol SPX (the default). Same market-data-vs-order-placement
  symbol split as find_1dte_put.py — SPX for chain/expiry lookups,
  SPXW for the actual order legs (place_order.py / roll_order.py).
- Expiry selection: picks whichever listed expiration is numerically
  closest to --dte days out (can land above or below 45, whichever
  is nearer — not "at least 45").
- The two legs are found independently: closest strike to
  --short-delta, and closest strike to --long-delta. They are NOT
  constrained relative to each other (e.g. the long strike isn't
  forced to sit below the short strike) — if the deltas happen to
  imply an unusual ordering, this will show that rather than silently
  correct it, since a real ordering inversion signals something worth
  looking at rather than a case to paper over.
"""

import argparse
import datetime
import os
import sys

from etrade_auth import LIVE_BASE_URL, run_interactive_login, sign_request
from show_balance import load_cached_token, save_token, TOKEN_CACHE_FILE
from find_1dte_put import (
    api_get, get_expiry_dates, get_option_chain, get_spx_quote, find_closest_delta_put,
)
from etrade_common import (
    select_account, build_spread_order, preview_order, place_order as etrade_place_order,
    extract_preview_summary, print_preview_summary, refresh_token_after_expiry,
)

CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "")


def get_token(consumer_key, consumer_secret):
    cached = load_cached_token()
    if cached:
        return cached["oauth_token"], cached["oauth_token_secret"]
    access_token, access_token_secret = run_interactive_login(consumer_key, consumer_secret)
    save_token(access_token, access_token_secret)
    return access_token, access_token_secret


def pick_nearest_to_dte(expiries, target_dte):
    """Pick whichever listed expiration is numerically closest to target_dte
    calendar days from today — can land above or below target, whichever
    is nearer, and never picks today itself (0 DTE isn't a 45DTE trade)."""
    today = datetime.date.today()
    future = [d for d in expiries if d > today]
    if not future:
        raise RuntimeError("No future expirations found.")
    return min(future, key=lambda d: abs((d - today).days - target_dte))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--short-delta", type=float, default=-0.16, help="target delta for the short (sold) put")
    parser.add_argument("--long-delta", type=float, default=-0.10, help="target delta for the long (bought) put")
    parser.add_argument("--dte", type=int, default=45, help="target days to expiration")
    parser.add_argument("--symbol", type=str, default="SPX", help="underlying symbol for market-data lookups — use SPX, not SPXW")
    parser.add_argument("--context", type=int, default=3, help="number of nearby strikes to show each side of each leg")
    parser.add_argument("--debug", action="store_true", help="print every strike/delta pair returned by the chain")
    parser.add_argument("--order", action="store_true", help="after finding both legs, launch the order preview/place flow")
    parser.add_argument("--order-symbol", default=None, help="symbol to use for order placement (defaults to SPXW if --symbol is SPX-based)")
    parser.add_argument("--account-last4", default="4422")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)

    def _fetch(strike_price_near=None):
        expiries = get_expiry_dates(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.symbol)
        expiry = pick_nearest_to_dte(expiries, args.dte)
        spot = get_spx_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret)
        chain = get_option_chain(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                  args.symbol, expiry, strike_price_near=strike_price_near)
        return expiry, spot, chain

    try:
        expiry, spot, chain = _fetch(strike_price_near=None)
    except PermissionError:
        print("Cached token expired — re-authenticating...")
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
        access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
        expiry, spot, chain = _fetch(strike_price_near=None)

    actual_dte = (expiry - datetime.date.today()).days

    def _rank_and_maybe_rewiden(chain_json, target_delta):
        """Rank strikes by closeness to target_delta; if the best match sits
        at the low-strike edge of the window (likely cut off before reaching
        the true target), re-anchor further out and retry once — same logic
        proven in find_1dte_put.py."""
        ranked = find_closest_delta_put(chain_json, target_delta)
        if not ranked or not spot:
            return ranked
        by_strike = sorted(ranked, key=lambda r: r[0])
        lowest_strike, lowest_delta, _ = by_strike[0]
        closest_strike, closest_delta, _ = min(ranked, key=lambda r: abs(r[1] - target_delta))
        if closest_strike == lowest_strike and abs(closest_delta - target_delta) > 0.01:
            span = spot - lowest_strike
            new_anchor = int(lowest_strike - span) if span > 0 else int(spot * 0.85)
            _, _, chain2 = _fetch(strike_price_near=new_anchor)
            ranked2 = find_closest_delta_put(chain2, target_delta)
            if ranked2:
                return ranked2
        return ranked

    short_ranked = _rank_and_maybe_rewiden(chain, args.short_delta)
    long_ranked = _rank_and_maybe_rewiden(chain, args.long_delta)

    if not short_ranked or not long_ranked:
        print("Could not find strikes with delta data. Market may be closed, or try again during RTH.")
        return

    if args.debug:
        print(f"\n--- DEBUG: strikes near short target {args.short_delta} ---")
        for strike, delta, put in sorted(short_ranked, key=lambda r: r[0]):
            print(f"  strike={strike}   delta={delta}")
        print(f"\n--- DEBUG: strikes near long target {args.long_delta} ---")
        for strike, delta, put in sorted(long_ranked, key=lambda r: r[0]):
            print(f"  strike={strike}   delta={delta}")
        print("---\n")

    short_strike, short_delta, short_put = min(short_ranked, key=lambda r: abs(r[1] - args.short_delta))
    long_strike, long_delta, long_put = min(long_ranked, key=lambda r: abs(r[1] - args.long_delta))

    print(f"\nExpiry: {expiry.isoformat()}  ({actual_dte} DTE, target was {args.dte})")
    if spot:
        print(f"SPX spot: {spot}")

    print(f"\nSHORT leg (sell): {short_strike} strike put, delta {short_delta:.4f}  (target {args.short_delta})")
    s_bid, s_ask = short_put.get("bid"), short_put.get("ask")
    if s_bid is not None and s_ask is not None:
        print(f"  Bid/Ask: {s_bid} / {s_ask}   Mid: {round((s_bid + s_ask) / 2, 2)}")
    if spot:
        pts = spot - short_strike
        print(f"  {pts:.2f} points / {pts/spot*100:.2f}% OTM")

    print(f"\nLONG leg (buy):  {long_strike} strike put, delta {long_delta:.4f}  (target {args.long_delta})")
    l_bid, l_ask = long_put.get("bid"), long_put.get("ask")
    if l_bid is not None and l_ask is not None:
        print(f"  Bid/Ask: {l_bid} / {l_ask}   Mid: {round((l_bid + l_ask) / 2, 2)}")
    if spot:
        pts = spot - long_strike
        print(f"  {pts:.2f} points / {pts/spot*100:.2f}% OTM")

    if short_strike <= long_strike:
        print(f"\n  NOTE: short strike ({short_strike}) is not above long strike ({long_strike}) —")
        print("  that's an unusual shape for a put credit spread (normally short > long).")
        print("  Worth double-checking these deltas/strikes before trading this.")

    width = abs(short_strike - long_strike)
    print(f"\nSpread width: {width} points")
    if s_bid is not None and s_ask is not None and l_bid is not None and l_ask is not None:
        short_mid = round((s_bid + s_ask) / 2, 2)
        long_mid = round((l_bid + l_ask) / 2, 2)
        net_credit_mid = round(short_mid - long_mid, 2)
        print(f"Rough net credit at mids (short mid - long mid): {net_credit_mid}")
        if width > 0:
            max_loss = width - net_credit_mid
            print(f"Max loss per spread (width - credit): {round(max_loss, 2)}  (x100 per contract = ${round(max_loss * 100, 2)})")
            if max_loss > 0:
                print(f"Rough credit / max-loss ratio: {round(net_credit_mid / max_loss, 4)}")

    if args.context > 0:
        print(f"\nNearby strikes around SHORT leg:")
        by_strike = sorted(short_ranked, key=lambda r: r[0])
        idx = by_strike.index((short_strike, short_delta, short_put))
        lo, hi = max(0, idx - args.context), min(len(by_strike), idx + args.context + 1)
        for strike, delta, put in by_strike[lo:hi]:
            marker = " <== short leg" if strike == short_strike else ""
            print(f"  {strike:>10}   delta={delta:.4f}{marker}")

        print(f"\nNearby strikes around LONG leg:")
        by_strike = sorted(long_ranked, key=lambda r: r[0])
        idx = by_strike.index((long_strike, long_delta, long_put))
        lo, hi = max(0, idx - args.context), min(len(by_strike), idx + args.context + 1)
        for strike, delta, put in by_strike[lo:hi]:
            marker = " <== long leg" if strike == long_strike else ""
            print(f"  {strike:>10}   delta={delta:.4f}{marker}")

    if not args.order:
        return

    # Market-data lookups used SPX; order placement needs the tradable root
    # (SPXW for SPX-family options) — same split as find_1dte_put.py --order
    # and roll_order.py.
    order_symbol = args.order_symbol
    if order_symbol is None:
        order_symbol = "SPXW" if args.symbol.upper() == "SPX" else args.symbol

    print(f"\n=== Order entry: {order_symbol} {expiry.isoformat()} "
          f"{short_strike}/{long_strike} put credit spread ===")

    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                              args.account_last4)
    account_id_key = account["accountIdKey"]

    quantity = int(input("Quantity (number of spreads): ").strip())

    print("\nPrice type:")
    print("  1) Net credit (you receive money)")
    print("  2) Net debit (you pay money)")
    print("  3) Market")
    choice = input("Select [1/2/3]: ").strip()

    limit_price = None
    if choice == "1":
        raw = input("Net credit to receive per spread (e.g. 0.45): ").strip()
        limit_price = float(raw)
        price_type = "NET_CREDIT"
    elif choice == "2":
        raw = input("Net debit to pay per spread (e.g. 0.20): ").strip()
        limit_price = float(raw)
        price_type = "NET_DEBIT"
    else:
        price_type = "MARKET"

    # Buy leg first per E*TRADE's documented ordering requirement (error 2054:
    # "place the buy order as the first leg"). This is a genuine 2-leg
    # OPENING spread (both legs new positions) — the case SPREADS orders
    # are confirmed to support, unlike a roll (close + open combined).
    long_leg = {
        "symbol": order_symbol,
        "expiry": expiry,
        "strike": long_strike,
        "order_action": "BUY_OPEN",
    }
    short_leg = {
        "symbol": order_symbol,
        "expiry": expiry,
        "strike": short_strike,
        "order_action": "SELL_OPEN",
    }

    request_body, client_order_id = build_spread_order(
        long_leg, short_leg, quantity, price_type, limit_price=limit_price,
    )

    try:
        preview_json = preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                      account_id_key, request_body)
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        preview_json = preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                      account_id_key, request_body)

    summary = extract_preview_summary(preview_json)
    header = (f"BUY_OPEN {quantity}x {long_strike} PUT + SELL_OPEN {quantity}x {short_strike} PUT "
              f"({order_symbol} {expiry.isoformat()}, {price_type})")
    print_preview_summary(summary, header, price_type, limit_price)

    if not summary["preview_ids"]:
        print("\nNo preview ID returned — cannot place this order. Review the messages above.")
        return

    confirm = input('\nType "yes" to place this spread order, anything else to cancel: ').strip().lower()
    if confirm != "yes":
        print("Order NOT placed.")
        return

    order_detail = request_body["PreviewOrderRequest"]["Order"][0]
    place_json = etrade_place_order(
        CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
        account_id_key, "SPREADS", client_order_id, order_detail, summary["preview_ids"],
    )

    place_resp = place_json.get("PlaceOrderResponse", {})
    order_ids = place_resp.get("OrderIds", [])
    print("\n--- ORDER PLACED ---")
    print(f"Order ID(s): {order_ids}")
    msgs = place_resp.get("Order", [{}])[0].get("messages", {}).get("Message", [])
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if m:
            print(f"  [{m.get('type')}] {m.get('description')}")


if __name__ == "__main__":
    main()
