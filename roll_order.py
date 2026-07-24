"""
Roll an existing short put into a new 1DTE short put.

E*TRADE's public order-placement API does not support rolling as a single
atomic order — testing confirmed SPREADS orders only accept same-direction
legs (e.g. two new opening legs), not a close+open pair. This places the
close and the open as two separate, sequential single-leg orders instead —
almost certainly what E*TRADE's own website "Roll" button does under the
hood too.

Market orders execute immediately, so both legs fire back-to-back with no
wait. Limit orders may sit unfilled, so if the close leg is a limit order,
this polls its status and waits for a real fill before opening the new
leg — otherwise you could briefly hold both the old and new short puts at
once, doubling exposure.

Usage:
    python roll_order.py --old-symbol SPXW --old-strike 7265 --old-expiry 2026-07-23 \
                          --new-delta -0.01

If --old-* isn't given, it lists your open short puts and lets you pick one.
The new leg always targets the next available 1DTE expiry (same-day roll only).
"""

import argparse
import datetime
import os
import sys
import time

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, get_portfolio, get_order_status,
)
from list_orders import summarize_order
from find_1dte_put import (
    get_expiry_dates, pick_1dte_expiry, get_option_chain, get_spx_quote,
    find_closest_delta_put,
)
from place_order import get_bid_ask_for_strike, place_single_leg_order


def pick_open_short_put(consumer_key, consumer_secret, access_token, access_token_secret, account_id_key):
    """
    List current open POSITIONS (not orders — a filled order becomes an
    EXECUTED order but the resulting position lives in the portfolio until
    closed), filter to short puts, let the user choose one.
    """
    data = get_portfolio(consumer_key, consumer_secret, access_token, access_token_secret, account_id_key)
    accounts = data.get("PortfolioResponse", {}).get("AccountPortfolio", [])
    if isinstance(accounts, dict):
        accounts = [accounts]

    positions = []
    for acct in accounts:
        pos = acct.get("Position", [])
        if isinstance(pos, dict):
            pos = [pos]
        positions.extend(pos)

    candidates = []
    for pos in positions:
        product = pos.get("Product", {})
        position_type = pos.get("positionType", "").upper()
        if (product.get("securityType") == "OPTN"
                and product.get("callPut") == "PUT"
                and position_type == "SHORT"):
            qty = pos.get("quantity")
            candidates.append({
                "symbol": product.get("symbol"),
                "strike": float(product.get("strikePrice")),
                "expiry": datetime.date(
                    int(product.get("expiryYear")),
                    int(product.get("expiryMonth")),
                    int(product.get("expiryDay")),
                ),
                # quantity often comes back negative for shorts — normalize to a
                # positive contract count for the order builder.
                "quantity": abs(int(qty)) if qty is not None else 1,
            })

    if not candidates:
        print("\nNo open short puts found in the portfolio to roll.")
        return None

    print("\nOpen short puts:")
    for i, c in enumerate(candidates):
        print(f"  [{i}] {c['symbol']} {c['expiry'].isoformat()} {c['strike']} PUT  qty={c['quantity']}")

    choice = input("\nSelect [index] to roll, or Enter to abort: ").strip()
    if not choice:
        return None
    try:
        return candidates[int(choice)]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-symbol", default=None)
    parser.add_argument("--old-strike", type=float, default=None)
    parser.add_argument("--old-expiry", default=None, help="YYYY-MM-DD")
    parser.add_argument("--old-quantity", type=int, default=None)
    parser.add_argument("--new-delta", type=float, default=-0.01, help="target delta for the new short put")
    parser.add_argument("--new-symbol", default=None, help="defaults to same as --old-symbol")
    parser.add_argument("--account-last4", default="4422")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    # --- Determine the leg being closed ---
    if args.old_symbol and args.old_strike is not None and args.old_expiry:
        old_leg = {
            "symbol": args.old_symbol,
            "strike": args.old_strike,
            "expiry": datetime.date.fromisoformat(args.old_expiry),
            "quantity": args.old_quantity or 1,
        }
    else:
        try:
            old_leg = pick_open_short_put(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           account_id_key)
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            old_leg = pick_open_short_put(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           account_id_key)
        if old_leg is None:
            print("Aborted — no position selected to roll.")
            return

    # Old leg's symbol (SPXW) is correct for the order leg we're building,
    # but E*TRADE's market-data endpoints (expiry dates, chains) want the
    # bare index ticker SPX, not SPXW. Two different subsystems, two symbols.
    order_symbol = args.new_symbol or old_leg["symbol"]
    market_data_symbol = "SPX" if order_symbol.upper().startswith("SPX") else order_symbol

    # --- Find the new 1DTE strike ---
    print(f"\nFinding new 1DTE {order_symbol} put near delta {args.new_delta}...")
    expiries = get_expiry_dates(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, market_data_symbol)
    new_expiry = pick_1dte_expiry(expiries)
    spot = get_spx_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret)

    chain = get_option_chain(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                              market_data_symbol, new_expiry, strike_price_near=int(spot) if spot else None)
    ranked = find_closest_delta_put(chain, args.new_delta)

    if ranked and spot:
        by_strike = sorted(ranked, key=lambda r: r[0])
        lowest_strike, lowest_delta, _ = by_strike[0]
        closest_strike, closest_delta, _ = min(ranked, key=lambda r: abs(r[1] - args.new_delta))
        if closest_strike == lowest_strike and abs(closest_delta - args.new_delta) > 0.005:
            span = spot - lowest_strike
            new_anchor = int(lowest_strike - span) if span > 0 else int(spot * 0.9)
            chain2 = get_option_chain(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                       market_data_symbol, new_expiry, strike_price_near=new_anchor)
            ranked2 = find_closest_delta_put(chain2, args.new_delta)
            if ranked2:
                ranked = ranked2

    if not ranked:
        print("Could not find a new strike with delta data. Market may be closed.")
        return

    new_strike, new_delta_actual, new_put = ranked[0]
    new_bid, new_ask = new_put.get("bid"), new_put.get("ask")
    print(f"New leg: {order_symbol} {new_expiry.isoformat()} {new_strike} PUT, delta {new_delta_actual:.4f}")
    if new_bid is not None and new_ask is not None:
        print(f"  Bid/Ask: {new_bid} / {new_ask}   Mid: {round((new_bid + new_ask) / 2, 2)}")
    if spot:
        pts = spot - new_strike
        print(f"  {pts:.2f} points / {pts/spot*100:.2f}% OTM")

    print(f"\nFetching bid/ask for the leg being closed ({old_leg['symbol']} {old_leg['strike']} PUT)...")
    old_market_symbol = "SPX" if old_leg["symbol"].upper().startswith("SPX") else old_leg["symbol"]
    try:
        old_bid, old_ask = get_bid_ask_for_strike(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                                    old_market_symbol, old_leg["expiry"], old_leg["strike"])
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        old_bid, old_ask = get_bid_ask_for_strike(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                                    old_market_symbol, old_leg["expiry"], old_leg["strike"])

    if old_bid is not None and old_ask is not None:
        print(f"  Bid/Ask: {old_bid} / {old_ask}   Mid: {round((old_bid + old_ask) / 2, 2)}")
        if new_bid is not None and new_ask is not None:
            # Closing costs the ask (buy to close); opening receives the bid (sell open) —
            # using mids for a rough net-credit estimate at typical fill quality.
            old_mid = round((old_bid + old_ask) / 2, 2)
            new_mid = round((new_bid + new_ask) / 2, 2)
            est_net = round(new_mid - old_mid, 2)
            print(f"  Rough net credit at mids (open mid - close mid): {est_net}")
    else:
        print("  Could not fetch bid/ask for the closing leg.")

    quantity = old_leg["quantity"]
    print(f"\nRolling: CLOSE {old_leg['symbol']} {old_leg['expiry'].isoformat()} {old_leg['strike']} PUT "
          f"-> OPEN {order_symbol} {new_expiry.isoformat()} {new_strike} PUT, qty={quantity}")

    confirm_qty = input(f"Quantity to roll [{quantity}]: ").strip()
    if confirm_qty:
        quantity = int(confirm_qty)

    print("\nNOTE: E*TRADE's public order API does not support rolling as a single")
    print("atomic order (confirmed via testing — only same-direction multi-leg")
    print("spreads are supported). This places the close and open as two separate")
    print("orders, same as the website's Roll likely does under the hood.\n")

    # --- Leg 1: close the old position ---
    print("=== LEG 1: Close existing position ===")
    print("Price type:")
    print("  1) Limit")
    print("  2) Market")
    close_choice = input("Select [1/2]: ").strip()
    close_price_type = "MARKET" if close_choice == "2" else "LIMIT"
    close_limit_price = None
    if close_price_type == "LIMIT":
        close_limit_price = input("Limit price to pay to close (e.g. 0.05): ").strip()

    try:
        close_order_ids, close_filled = place_single_leg_order(
            old_leg["symbol"], old_leg["strike"], old_leg["expiry"], "BUY_TO_CLOSE",
            quantity, close_price_type, close_limit_price,
            account_id_key, access_token, access_token_secret, confirm=True,
        )
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        close_order_ids, close_filled = place_single_leg_order(
            old_leg["symbol"], old_leg["strike"], old_leg["expiry"], "BUY_TO_CLOSE",
            quantity, close_price_type, close_limit_price,
            account_id_key, access_token, access_token_secret, confirm=True,
        )

    if not close_order_ids:
        print("\nClose leg was not placed — stopping before opening the new leg.")
        return

    # Market orders during regular hours fill essentially immediately, so we
    # don't need to wait. Limit orders might sit unfilled — poll before
    # firing the second leg, so we never briefly hold both positions at once.
    if not close_filled:
        close_order_id = close_order_ids[0]
        print(f"\nWaiting for close order {close_order_id} to fill before opening the new leg...")
        max_checks = 30
        for i in range(max_checks):
            time.sleep(10)
            try:
                status = get_order_status(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           account_id_key, close_order_id)
            except PermissionError:
                access_token, access_token_secret = refresh_token_after_expiry()
                status = get_order_status(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           account_id_key, close_order_id)
            print(f"  [{i+1}/{max_checks}] status: {status}")
            if status == "EXECUTED":
                close_filled = True
                break
            if status in ("CANCELLED", "REJECTED", "EXPIRED"):
                print(f"\nClose order ended in status {status} — stopping before opening the new leg.")
                return

        if not close_filled:
            print("\nClose order still not filled after waiting. Not opening the new leg.")
            print(f"Check order {close_order_id} manually (list_orders.py / cancel_order.py) before retrying.")
            return

    print("\nClose leg confirmed filled.")

    # --- Leg 2: open the new position ---
    print("\n=== LEG 2: Open new position ===")
    print("Price type:")
    print("  1) Limit")
    print("  2) Market")
    open_choice = input("Select [1/2]: ").strip()
    open_price_type = "MARKET" if open_choice == "2" else "LIMIT"
    open_limit_price = None
    if open_price_type == "LIMIT":
        open_limit_price = input("Limit price to receive for opening (e.g. 0.45): ").strip()

    try:
        open_order_ids, _ = place_single_leg_order(
            order_symbol, new_strike, new_expiry, "SELL_OPEN",
            quantity, open_price_type, open_limit_price,
            account_id_key, access_token, access_token_secret, confirm=True,
        )
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        open_order_ids, _ = place_single_leg_order(
            order_symbol, new_strike, new_expiry, "SELL_OPEN",
            quantity, open_price_type, open_limit_price,
            account_id_key, access_token, access_token_secret, confirm=True,
        )

    print("\n--- ROLL COMPLETE ---")
    print(f"Close order ID(s): {close_order_ids}")
    print(f"Open order ID(s): {open_order_ids}")


if __name__ == "__main__":
    main()
