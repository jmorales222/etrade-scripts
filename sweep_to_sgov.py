"""
Sweep idle cash into SGOV at end of day, keeping a minimum cash buffer.

Why: cash sitting in E*TRADE's default sweep earns less than SGOV (which
tracks ~T-bill rates). Put-spread credits accumulate as cash and just sit
there losing yield until moved. This script buys as many whole shares of
SGOV as the available cash (above a floor you set) allows.

This is a MARKET order — SGOV is a large, extremely liquid ETF (tracks
0-3mo T-bills), so bid/ask spread is a non-issue; no walking-limit logic
needed here the way it is for SPX options.

IMPORTANT — untested territory: an earlier diagnostic (diag_equity_preview.py)
in this repo exists specifically because this account previously hit error
1000 ("account not approved for trading") on an equity preview, and that
diagnostic never confirmed the issue was resolved. This script's FIRST run
should be watched closely — if the preview step returns error 1000 or
anything unexpected, STOP and investigate before ever passing --confirm.

Usage:
    python sweep_to_sgov.py --account-last4 4422 --min-cash-buffer 80
        (dry run: shows the computed buy, does not place it)

    python sweep_to_sgov.py --account-last4 4422 --min-cash-buffer 80 --confirm
        (asks for a typed "yes", same as the rest of this codebase, before placing)
"""

import argparse
import datetime
import os
import sys

from etrade_auth import LIVE_BASE_URL, run_interactive_login, sign_request
from show_balance import (
    load_cached_token, save_token, TOKEN_CACHE_FILE, get_balance, get_account_list,
)
from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, preview_order, place_order as etrade_place_order,
    extract_preview_summary, print_preview_summary, api_get,
)
from retry_helper import with_retry

SGOV_SYMBOL = "SGOV"


def get_sgov_quote(consumer_key, consumer_secret, access_token, access_token_secret):
    """Fetch current bid/ask/last for SGOV, for display only — the actual
    order is MARKET, this is just so you can see roughly what price you're
    buying at before confirming."""
    url = f"{LIVE_BASE_URL}/v1/market/quote/{SGOV_SYMBOL}.json"

    def _fetch():
        return api_get(url, consumer_key, consumer_secret, access_token, access_token_secret)

    data = with_retry(_fetch, what="get_sgov_quote")
    quotes = data.get("QuoteResponse", {}).get("QuoteData", [])
    if not quotes:
        return None, None, None
    all_data = quotes[0].get("All", {})
    return all_data.get("bid"), all_data.get("ask"), all_data.get("lastTrade")


def build_equity_market_buy(symbol, quantity, client_order_id=None):
    """
    Build an EQ (equity) MARKET BUY order body. etrade_common.py's
    build_single_leg_order is hardcoded to securityType OPTN, so this is
    a separate builder for the equity case — same request shape confirmed
    working (at least through preview) by diag_equity_preview.py earlier
    in this repo, just filled in with real quantity/order-term instead of
    that script's throwaway diagnostic values.
    """
    if client_order_id is None:
        client_order_id = f"sgov{int(datetime.datetime.now().timestamp())}"

    order_detail = {
        "allOrNone": "false",
        "priceType": "MARKET",
        "orderTerm": "GOOD_FOR_DAY",
        "marketSession": "REGULAR",
        "Instrument": [
            {
                "Product": {
                    "securityType": "EQ",
                    "symbol": symbol,
                },
                "orderAction": "BUY",
                "quantityType": "QUANTITY",
                "quantity": str(quantity),
            }
        ],
    }

    request_body = {
        "PreviewOrderRequest": {
            "orderType": "EQ",
            "clientOrderId": client_order_id,
            "Order": [order_detail],
        }
    }
    return request_body, client_order_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-last4", default="4422")
    parser.add_argument("--min-cash-buffer", type=float, default=80.0,
                         help="never sweep cash below this floor (default 80)")
    parser.add_argument("--confirm", action="store_true",
                         help="actually place the order after preview (asks for typed 'yes'). "
                              "Without this flag, the script stops after showing what it WOULD do.")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)

    def _select_account():
        return select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                               args.account_last4)
    try:
        account = with_retry(_select_account, what="select_account")
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        account = with_retry(_select_account, what="select_account")

    account_id_key = account["accountIdKey"]
    inst_type = account.get("institutionType", "BROKERAGE")

    def _get_balance():
        return get_balance(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                            account_id_key, institution_type=inst_type)
    try:
        bal = with_retry(_get_balance, what="get_balance")
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        bal = with_retry(_get_balance, what="get_balance")

    cash = bal.get("BalanceResponse", {}).get("Cash", {}).get("fundsForOpenOrdersCash")
    if cash is None:
        computed = bal.get("BalanceResponse", {}).get("Computed", {})
        cash = computed.get("cashAvailableForInvestment") or computed.get("cashBalance")

    if cash is None:
        print("Could not determine available cash from the balance response. "
              "Raw response for debugging:")
        print(bal)
        sys.exit(1)

    print(f"Available cash: ${cash:,.2f}")
    print(f"Minimum buffer: ${args.min_cash_buffer:,.2f}")

    sweepable = cash - args.min_cash_buffer
    if sweepable <= 0:
        print(f"\nNothing to sweep — available cash (${cash:,.2f}) is at or below "
              f"the buffer (${args.min_cash_buffer:,.2f}).")
        return

    bid, ask, last = get_sgov_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret)
    price_estimate = ask or last or bid
    if not price_estimate:
        print("Could not fetch a SGOV price to estimate share count. Aborting.")
        sys.exit(1)

    print(f"SGOV bid/ask/last: {bid} / {ask} / {last}")

    shares = int(sweepable // price_estimate)
    if shares < 1:
        print(f"\nSweepable amount (${sweepable:,.2f}) isn't enough for even 1 share of SGOV "
              f"at ~${price_estimate}. Nothing to do.")
        return

    estimated_cost = round(shares * price_estimate, 2)
    print(f"\nWould buy {shares} share(s) of SGOV at ~${price_estimate} "
          f"(est. cost ${estimated_cost:,.2f}), leaving ~${cash - estimated_cost:,.2f} cash.")

    request_body, client_order_id = build_equity_market_buy(SGOV_SYMBOL, shares)

    def _preview():
        return preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                              account_id_key, request_body)
    try:
        preview_json = with_retry(_preview, what="preview_order")
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        preview_json = with_retry(_preview, what="preview_order")

    summary = extract_preview_summary(preview_json)
    header = f"BUY {shares}x {SGOV_SYMBOL} (MARKET)"
    print_preview_summary(summary, header, "MARKET", None)

    if not summary["preview_ids"]:
        print("\nNo preview ID returned — cannot place this order. Review the messages above.")
        print("NOTE: if you see error code 1000 ('account not approved for trading') here, "
              "this account/API key may not have equity trading enabled — see the note at "
              "the top of this file and diag_equity_preview.py. Do not proceed with --confirm "
              "until that's resolved with E*TRADE support.")
        return

    if not args.confirm:
        print("\n(Dry run — pass --confirm to actually place this order.)")
        return

    ans = input('\nType "yes" to place this SGOV buy, anything else to cancel: ').strip().lower()
    if ans != "yes":
        print("Not placed.")
        return

    order_detail = request_body["PreviewOrderRequest"]["Order"][0]

    def _place():
        return etrade_place_order(
            CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
            account_id_key, "EQ", client_order_id, order_detail, summary["preview_ids"],
        )
    try:
        place_json = with_retry(_place, what="place_order")
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        place_json = with_retry(_place, what="place_order")

    place_resp = place_json.get("PlaceOrderResponse", {})
    order_ids = place_resp.get("OrderIds", [])
    print(f"\n--- ORDER PLACED --- Order ID(s): {order_ids}")
    msgs = place_resp.get("Order", [{}])[0].get("messages", {}).get("Message", [])
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if m:
            print(f"  [{m.get('type')}] {m.get('description')}")


if __name__ == "__main__":
    main()
