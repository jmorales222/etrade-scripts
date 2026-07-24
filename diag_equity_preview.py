"""
Diagnostic only: preview (never place) a 1-share equity limit order,
priced far below market so it could never fill even if placed.

Purpose: isolate whether error 1000 ("account not approved for
trading") is a blanket account/API-permission block, or specific to
options / index options.

This script always stops after preview — it has no place-order path.
"""

import os
import sys

import requests

from etrade_auth import LIVE_BASE_URL, run_interactive_login, sign_request
from show_balance import load_cached_token, save_token, TOKEN_CACHE_FILE, get_account_list

CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "")


def get_token(consumer_key, consumer_secret):
    cached = load_cached_token()
    if cached:
        return cached["oauth_token"], cached["oauth_token_secret"]
    access_token, access_token_secret = run_interactive_login(consumer_key, consumer_secret)
    save_token(access_token, access_token_secret)
    return access_token, access_token_secret


def api_post_json(url, consumer_key, consumer_secret, access_token, access_token_secret, body):
    header = sign_request("POST", url, consumer_key, consumer_secret, access_token, access_token_secret)
    resp = requests.post(url, headers={"Authorization": header, "Content-Type": "application/json"}, json=body)
    print(f"\nHTTP {resp.status_code}")
    print(resp.text)
    return resp


def main():
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)

    accounts = get_account_list(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret)
    account_list = accounts.get("AccountListResponse", {}).get("Accounts", {}).get("Account", [])
    matches = [a for a in account_list if str(a.get("accountId", "")).endswith("4422")]
    if not matches:
        print("No account ending in 4422 found. Accounts on this login:")
        for a in account_list:
            print(f"  {a.get('accountId')} - {a.get('accountDesc')}")
        sys.exit(1)
    account = matches[0]
    account_id_key = account["accountIdKey"]
    print(f"Using account: {account.get('accountDesc', account.get('accountId'))} (...4422)")

    # 1 share of KO (Coca-Cola — boring, liquid, cheap), limit price
    # absurdly low ($1.00) so it could never execute even if placed.
    # We never place it anyway; this only calls preview.
    body = {
        "PreviewOrderRequest": {
            "orderType": "EQ",
            "clientOrderId": "diag1000test",
            "Order": [
                {
                    "allOrNone": "false",
                    "priceType": "LIMIT",
                    "limitPrice": "1.00",
                    "orderTerm": "GOOD_FOR_DAY",
                    "marketSession": "REGULAR",
                    "Instrument": [
                        {
                            "Product": {
                                "securityType": "EQ",
                                "symbol": "KO",
                            },
                            "orderAction": "BUY",
                            "quantityType": "QUANTITY",
                            "quantity": "1",
                        }
                    ],
                }
            ],
        }
    }

    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/orders/preview.json"
    print("\nPreviewing a 1-share KO limit BUY at $1.00 (diagnostic only, will not be placed)...")
    api_post_json(url, CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, body)
    print("\n(This script never places an order — diagnostic complete.)")


if __name__ == "__main__":
    main()
