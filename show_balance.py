"""
Proof of concept: log in to E*TRADE and print account balance(s).

Usage:
    python show_balance.py

First run each day: this opens a browser for you to approve access,
then asks you to paste back the verification code E*TRADE shows you.
The resulting access token is cached in .etrade_token.json so you're
not re-approving on every single run (still expires at midnight ET).

Setup required before running:
    1. Fill in CONSUMER_KEY / CONSUMER_SECRET below (or set them as
       the ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars).
    2. pip install requests
"""

import json
import os
import sys

import requests

from etrade_auth import LIVE_BASE_URL, run_interactive_login, sign_request

TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".etrade_token.json")

CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "")


def load_cached_token():
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            return json.load(f)
    return None


def save_token(access_token, access_token_secret):
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(
            {"oauth_token": access_token, "oauth_token_secret": access_token_secret},
            f,
        )


def get_account_list(consumer_key, consumer_secret, access_token, access_token_secret):
    url = f"{LIVE_BASE_URL}/v1/accounts/list.json"
    header = sign_request(
        "GET", url, consumer_key, consumer_secret, access_token, access_token_secret
    )
    resp = requests.get(url, headers={"Authorization": header})
    resp.raise_for_status()
    return resp.json()


def get_balance(consumer_key, consumer_secret, access_token, access_token_secret, account_id_key, institution_type="BROKERAGE"):
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/balance.json"
    extra_params = {"instType": institution_type, "realTimeNAV": "true"}
    header = sign_request(
        "GET",
        url,
        consumer_key,
        consumer_secret,
        access_token,
        access_token_secret,
        extra_params=extra_params,
    )
    resp = requests.get(url, headers={"Authorization": header}, params=extra_params)
    resp.raise_for_status()
    return resp.json()


def main():
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing consumer key/secret. Set ETRADE_CONSUMER_KEY and")
        print("ETRADE_CONSUMER_SECRET as environment variables, or edit this")
        print("file directly.")
        sys.exit(1)

    cached = load_cached_token()
    if cached:
        access_token = cached["oauth_token"]
        access_token_secret = cached["oauth_token_secret"]
        print("Using cached access token (from earlier today).")
    else:
        access_token, access_token_secret = run_interactive_login(
            CONSUMER_KEY, CONSUMER_SECRET
        )
        save_token(access_token, access_token_secret)

    try:
        accounts = get_account_list(
            CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret
        )
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            print("Cached token rejected (likely expired) — re-running login...")
            os.remove(TOKEN_CACHE_FILE)
            access_token, access_token_secret = run_interactive_login(
                CONSUMER_KEY, CONSUMER_SECRET
            )
            save_token(access_token, access_token_secret)
            accounts = get_account_list(
                CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret
            )
        else:
            raise

    account_list = accounts.get("AccountListResponse", {}).get("Accounts", {}).get("Account", [])
    if not account_list:
        print("No accounts found on this login.")
        return

    for acct in account_list:
        acct_id_key = acct["accountIdKey"]
        acct_desc = acct.get("accountDesc", acct.get("accountId"))
        inst_type = acct.get("institutionType", "BROKERAGE")

        print(f"\n--- {acct_desc} ({acct['accountId']}) ---")
        try:
            bal = get_balance(
                CONSUMER_KEY,
                CONSUMER_SECRET,
                access_token,
                access_token_secret,
                acct_id_key,
                institution_type=inst_type,
            )
            computed = bal.get("BalanceResponse", {}).get("Computed", {})
            net_value = computed.get("RealTimeValues", {}).get("totalAccountValue") \
                or computed.get("netAccountValue")
            cash = bal.get("BalanceResponse", {}).get("Cash", {}).get("fundsForOpenOrdersCash")

            print(f"  Total account value: {net_value}")
            if cash is not None:
                print(f"  Cash available:       {cash}")
        except requests.HTTPError as e:
            print(f"  Could not fetch balance: {e}")


if __name__ == "__main__":
    main()
