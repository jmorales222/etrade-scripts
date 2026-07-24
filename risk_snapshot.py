"""
Show a full account risk/buying-power snapshot: margin, cash, and
day-trading buying power, plus portfolio-margin house excess equity —
everything the preview-order response exposes, without needing to be
mid-way through placing a real trade.

Mechanism: E*TRADE only returns these numbers as part of a PreviewOrderResponse,
so this previews a trivial 1-share limit order (KO, $1.00 — priced so it could
never fill) purely to read back the account-wide fields. Nothing is ever placed.

Usage:
    python risk_snapshot.py
    python risk_snapshot.py --account-last4 4422

IMPORTANT CAVEAT: this reflects E*TRADE's real-time buying-power math
only. It does NOT include any separate, after-the-fact house risk
assessment E*TRADE may run on the account. If your account was ever
flagged by an internal risk review outside of real-time BP math, this
snapshot can't promise to catch or predict that — the public API
doesn't appear to expose whatever methodology produces that review.
"""

import argparse
import json
import os
import sys

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, preview_order, extract_preview_summary,
)


def build_throwaway_preview_body():
    return {
        "PreviewOrderRequest": {
            "orderType": "EQ",
            "clientOrderId": f"risksnap{os.getpid()}",
            "Order": [
                {
                    "allOrNone": "false",
                    "priceType": "LIMIT",
                    "limitPrice": "1.00",
                    "orderTerm": "GOOD_FOR_DAY",
                    "marketSession": "REGULAR",
                    "Instrument": [
                        {
                            "Product": {"securityType": "EQ", "symbol": "KO"},
                            "orderAction": "BUY",
                            "quantityType": "QUANTITY",
                            "quantity": "1",
                        }
                    ],
                }
            ],
        }
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-last4", default="4422")
    parser.add_argument("--debug", action="store_true", help="print the raw preview JSON")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    body = build_throwaway_preview_body()

    try:
        preview_json = preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                      account_id_key, body)
    except PermissionError:
        access_token, access_token_secret = refresh_token_after_expiry()
        preview_json = preview_order(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                      account_id_key, body)

    summary = extract_preview_summary(preview_json)

    if args.debug:
        print("\n--- DEBUG: raw PreviewOrderResponse ---")
        print(json.dumps(preview_json, indent=2))
        print("--- END DEBUG ---\n")

    print("\n--- ACCOUNT RISK SNAPSHOT ---")
    print("(via a throwaway 1-share diagnostic preview — nothing placed)\n")

    def _print_bp(label, block):
        if block["current_bp"] is None:
            print(f"  {label}: not available")
            return
        print(f"  {label}: {block['current_bp']}")

    print("Current buying power:")
    _print_bp("Margin", summary["margin_bp"])
    _print_bp("Cash", summary["cash_bp"])
    _print_bp("Day-trading", summary["dt_bp"])

    if summary["pm_eligible"] is not None:
        print(f"\nPortfolio Margin eligible: {summary['pm_eligible']}")
    if summary["house_excess_curr"] is not None:
        print(f"House excess equity (current): {summary['house_excess_curr']}")

    print("\nCAVEAT: This is E*TRADE's real-time buying-power math only.")
    print("It does not reflect any separate, after-the-fact house risk")
    print("assessment E*TRADE may run — that methodology isn't exposed")
    print("by the public API, so this snapshot can't promise to predict it.")
    print("\n--- END SNAPSHOT ---")


if __name__ == "__main__":
    main()
