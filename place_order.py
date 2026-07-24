"""
Preview and place a single-leg option order on E*TRADE.

This module only builds/previews/places orders — it does not pick
strikes. Use find_1dte_put.py to find a strike, then hand it to
place_order.py to trade it (either via the CLI, or in-process via
run_order_flow, which find_1dte_put.py --order uses directly so it
doesn't have to shell out to a second process / re-authenticate).

Order flow (matches E*TRADE's required sequence):
  1. Build order legs
  2. POST preview -> get previewId + real margin impact (currentOrderImpact)
     and total order value (the credit, for a net credit sell)
  3. Show the user the preview details
  4. User must type "yes" to proceed
  5. POST place, referencing the previewId from step 2

Nothing here ever places an order without an explicit typed "yes"
after seeing the real preview numbers from E*TRADE itself.
"""

import argparse
import datetime
import os
import sys

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, build_single_leg_order, preview_order, place_order,
    extract_preview_summary, print_preview_summary,
)
from find_1dte_put import get_option_chain


def get_bid_ask_for_strike(consumer_key, consumer_secret, access_token, access_token_secret,
                            market_data_symbol, expiry, strike):
    """Fetch the option chain anchored near this strike and pull bid/ask for the exact match."""
    chain = get_option_chain(consumer_key, consumer_secret, access_token, access_token_secret,
                              market_data_symbol, expiry, strike_price_near=int(strike))
    pairs = chain.get("OptionChainResponse", {}).get("OptionPair", [])
    for pair in pairs:
        put = pair.get("Put")
        if put and float(put.get("strikePrice", -1)) == float(strike):
            return put.get("bid"), put.get("ask")
    return None, None


def place_single_leg_order(symbol, strike, expiry, action, quantity, price_type, limit_price,
                            account_id_key, access_token, access_token_secret, confirm=True):
    """
    Non-interactive preview + (optionally confirmed) place for one leg.
    Returns (order_ids, filled) where filled is best-effort based on the
    place response's messages — market orders during regular hours are
    treated as filled; anything else is treated as not-yet-confirmed.
    Raises PermissionError on token expiry so callers can refresh and retry.
    """
    request_body, client_order_id = build_single_leg_order(
        symbol=symbol, expiry=expiry, strike=strike, order_action=action,
        quantity=quantity, price_type=price_type, limit_price=limit_price,
    )

    preview_json = preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                  account_id_key, request_body)
    summary = extract_preview_summary(preview_json)
    header = f"{action} {quantity}x {symbol} {expiry.isoformat()} {strike} PUT ({price_type})"
    print_preview_summary(summary, header, price_type, limit_price)

    if not summary["preview_ids"]:
        print("\nNo preview ID returned — cannot place this order. Review the messages above.")
        return None, False

    if confirm:
        ans = input('\nType "yes" to place this leg, anything else to cancel: ').strip().lower()
        if ans != "yes":
            print("Leg NOT placed.")
            return None, False

    order_detail = request_body["PreviewOrderRequest"]["Order"][0]
    place_json = place_order(
        CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
        account_id_key, "OPTN", client_order_id, order_detail, summary["preview_ids"],
    )
    place_resp = place_json.get("PlaceOrderResponse", {})
    order_ids = [o.get("orderId") for o in place_resp.get("OrderIds", [])]
    print(f"\n--- LEG PLACED --- Order ID(s): {order_ids}")

    msgs = place_resp.get("Order", [{}])[0].get("messages", {}).get("Message", [])
    msgs = msgs if isinstance(msgs, list) else [msgs]
    filled = False
    for m in msgs:
        if not m:
            continue
        print(f"  [{m.get('type')}] {m.get('description')}")
        # Code 1026 = "Your order was successfully entered during market hours" —
        # for a MARKET order this means it executed essentially immediately.
        if price_type == "MARKET" and str(m.get("code")) == "1026":
            filled = True

    return order_ids, filled


def run_order_flow(symbol, strike, expiry, action, account_last4,
                    access_token, access_token_secret):
    """
    The full interactive preview/confirm/place flow for a single-leg order,
    reusable in-process (e.g. from find_1dte_put.py --order) or via the CLI
    below. Takes an already-authenticated token pair — callers own auth/refresh.
    """
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, account_last4)
    account_id_key = account["accountIdKey"]

    # Market-data lookups (chain/bid-ask) want the bare index ticker (SPX),
    # not the order-placement root (SPXW) — same split as roll_order.py.
    market_data_symbol = "SPX" if symbol.upper().startswith("SPX") else symbol
    print(f"\nFetching current bid/ask for {symbol} {expiry.isoformat()} {strike} PUT...")
    try:
        bid, ask = get_bid_ask_for_strike(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           market_data_symbol, expiry, strike)
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        bid, ask = get_bid_ask_for_strike(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                           market_data_symbol, expiry, strike)

    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2, 2)
        print(f"Bid: {bid}   Ask: {ask}   Mid: {mid}")
    else:
        print("Could not fetch bid/ask for this strike (market may be closed, or strike not in range).")

    print("\nPrice type:")
    print("  1) Limit")
    print("  2) Market")
    choice = input("Select [1/2]: ").strip()
    price_type = "LIMIT" if choice != "2" else "MARKET"

    limit_price = None
    if price_type == "LIMIT":
        limit_price = input("Limit price (credit received per contract, e.g. 0.25): ").strip()

    quantity = int(input("Quantity (contracts): ").strip())

    try:
        place_single_leg_order(symbol, strike, expiry, action, quantity, price_type, limit_price,
                                account_id_key, access_token, access_token_secret, confirm=True)
    except PermissionError:
        print("Cached token expired — re-authenticating...")
        access_token, access_token_secret = refresh_token_after_expiry()
        place_single_leg_order(symbol, strike, expiry, action, quantity, price_type, limit_price,
                                account_id_key, access_token, access_token_secret, confirm=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="underlying symbol, e.g. SPXW")
    parser.add_argument("--strike", required=True, type=float)
    parser.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    parser.add_argument("--action", default="SELL_OPEN", choices=[
        "SELL_OPEN", "BUY_OPEN", "SELL_CLOSE", "BUY_CLOSE"
    ])
    parser.add_argument("--account-last4", default="4422", help="last 4 digits of the account number to trade in")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    expiry = datetime.date.fromisoformat(args.expiry)

    run_order_flow(args.symbol, args.strike, expiry, args.action, args.account_last4,
                   access_token, access_token_secret)


if __name__ == "__main__":
    main()
