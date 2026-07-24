"""
List current open positions (the portfolio) — distinct from list_orders.py,
which shows ORDER status (working/executed/cancelled). A filled SELL_OPEN
order becomes an EXECUTED order, but the resulting position lives here,
in the portfolio, until it's closed or expires.

Usage:
    python list_positions.py
    python list_positions.py --debug
"""

import argparse
import json
import sys

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, get_portfolio,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-last4", default="4422")
    parser.add_argument("--debug", action="store_true", help="print raw portfolio JSON")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    try:
        data = get_portfolio(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, account_id_key)
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        data = get_portfolio(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, account_id_key)

    if args.debug:
        print("\n--- DEBUG: raw PortfolioResponse ---")
        print(json.dumps(data, indent=2))
        print("--- END DEBUG ---\n")

    accounts = data.get("PortfolioResponse", {}).get("AccountPortfolio", [])
    if isinstance(accounts, dict):
        accounts = [accounts]

    positions = []
    for acct in accounts:
        pos = acct.get("Position", [])
        if isinstance(pos, dict):
            pos = [pos]
        positions.extend(pos)

    if not positions:
        print("\nNo open positions.")
        return

    print(f"\n{len(positions)} open position(s):\n")
    for pos in positions:
        product = pos.get("Product", {})
        sec_type = product.get("securityType")
        if sec_type == "OPTN":
            desc = (f"{product.get('symbol')} {product.get('expiryMonth')}/{product.get('expiryDay')}/"
                    f"{product.get('expiryYear')} {product.get('strikePrice')} {product.get('callPut')}")
        else:
            desc = product.get("symbol", "?")
        print(f"  {pos.get('positionType', '?'):>6}  qty={pos.get('quantity')}   {desc}   "
              f"marketValue={pos.get('marketValue')}   totalGain={pos.get('totalGain')}")


if __name__ == "__main__":
    main()
