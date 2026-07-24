"""
List orders on E*TRADE (defaults to OPEN orders).

Usage:
    python list_orders.py
    python list_orders.py --status EXECUTED
    python list_orders.py --symbol SPXW
"""

import argparse
import os
import sys

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, list_orders,
)


def summarize_order(order):
    """Flatten one Order entry from OrdersResponse into a display-friendly dict."""
    order_id = order.get("orderId")
    details = order.get("OrderDetail", [])
    if isinstance(details, dict):
        details = [details]

    legs_desc = []
    status = None
    for d in details:
        status = d.get("status", status)
        instruments = d.get("Instrument", [])
        if isinstance(instruments, dict):
            instruments = [instruments]
        for inst in instruments:
            product = inst.get("Product", {})
            action = inst.get("orderAction", "?")
            qty = inst.get("orderedQuantity", inst.get("quantity", "?"))
            if product.get("securityType") == "OPTN":
                desc = (f"{action} {qty}x {product.get('symbol')} "
                        f"{product.get('expiryMonth')}/{product.get('expiryDay')}/{product.get('expiryYear')} "
                        f"{product.get('strikePrice')} {product.get('callPut')}")
            else:
                desc = f"{action} {qty}x {product.get('symbol')}"
            legs_desc.append(desc)

    return order_id, status, legs_desc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="OPEN", help="OPEN, EXECUTED, CANCELLED, etc. (default OPEN)")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--account-last4", default="4422")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    try:
        data = list_orders(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                            account_id_key, status=args.status, symbol=args.symbol)
    except PermissionError:
        print("Cached token expired — re-authenticating...")
        access_token, access_token_secret = refresh_token_after_expiry()
        data = list_orders(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                            account_id_key, status=args.status, symbol=args.symbol)

    orders = data.get("OrdersResponse", {}).get("Order", [])
    if isinstance(orders, dict):
        orders = [orders]

    if not orders:
        print(f"\nNo orders found with status={args.status}" + (f", symbol={args.symbol}" if args.symbol else ""))
        return

    print(f"\n{len(orders)} order(s) with status={args.status}:\n")
    for order in orders:
        order_id, status, legs = summarize_order(order)
        print(f"Order ID: {order_id}   Status: {status}")
        for leg in legs:
            print(f"    {leg}")
        print()


if __name__ == "__main__":
    main()
