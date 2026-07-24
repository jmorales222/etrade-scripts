"""
Cancel an open order on E*TRADE.

Usage:
    python cancel_order.py --order-id 12345
    python cancel_order.py            # lists open orders, then prompts for which to cancel

Always requires typing "yes" before actually cancelling.
"""

import argparse
import os
import sys

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, list_orders, cancel_order,
)
from list_orders import summarize_order


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", type=int, default=None, help="order ID to cancel; omit to browse open orders")
    parser.add_argument("--account-last4", default="4422")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    order_id = args.order_id

    if order_id is None:
        try:
            data = list_orders(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                account_id_key, status="OPEN")
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            data = list_orders(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                account_id_key, status="OPEN")

        orders = data.get("OrdersResponse", {}).get("Order", [])
        if isinstance(orders, dict):
            orders = [orders]

        if not orders:
            print("\nNo open orders found.")
            return

        print(f"\n{len(orders)} open order(s):\n")
        for i, order in enumerate(orders):
            oid, status, legs = summarize_order(order)
            print(f"  [{i}] Order ID: {oid}   Status: {status}")
            for leg in legs:
                print(f"        {leg}")

        choice = input("\nEnter the [index] of the order to cancel, or press Enter to abort: ").strip()
        if not choice:
            print("Aborted.")
            return
        try:
            order_id = summarize_order(orders[int(choice)])[0]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return

    confirm = input(f'\nType "yes" to cancel order {order_id}, anything else to abort: ').strip().lower()
    if confirm != "yes":
        print("Not cancelled.")
        return

    try:
        result = cancel_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                               account_id_key, order_id)
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        result = cancel_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                               account_id_key, order_id)

    resp = result.get("CancelOrderResponse", {})
    print(f"\nCancelled. Order ID: {resp.get('orderId')}")
    msgs = resp.get("Messages", {}).get("Message", [])
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if m:
            print(f"  [{m.get('type')}] {m.get('description')}")


if __name__ == "__main__":
    main()
