"""
Shared plumbing for the order-management scripts:
  - place_order.py   (open a new position)
  - list_orders.py   (view open orders)
  - cancel_order.py  (cancel an open order)
  - roll_order.py    (close + reopen as a single spread order)

Nothing in here places or cancels anything without the calling
script's own explicit confirmation step.
"""

import datetime
import os

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


def refresh_token_after_expiry():
    """Call after catching PermissionError. Clears cache and re-authenticates."""
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)
    return get_token(CONSUMER_KEY, CONSUMER_SECRET)


def api_get(url, consumer_key, consumer_secret, access_token, access_token_secret, params=None):
    params = params or {}
    header = sign_request(
        "GET", url, consumer_key, consumer_secret, access_token, access_token_secret,
        extra_params=params,
    )
    resp = requests.get(url, headers={"Authorization": header}, params=params)
    if resp.status_code == 401:
        raise PermissionError("token rejected")
    if resp.status_code == 204 or not resp.text.strip():
        # E*TRADE returns 204 with an empty body when there's no data
        # (e.g. no open orders matching the filter). Not an error.
        return {}
    if not resp.ok:
        print(f"\n--- E*TRADE error response ({resp.status_code}) ---")
        print(resp.text)
        print("---\n")
    resp.raise_for_status()
    return resp.json()


def api_post_json(url, consumer_key, consumer_secret, access_token, access_token_secret, body):
    """POST with JSON body. E*TRADE signs POSTs using only the OAuth params (not the JSON body)."""
    header = sign_request(
        "POST", url, consumer_key, consumer_secret, access_token, access_token_secret
    )
    resp = requests.post(
        url,
        headers={"Authorization": header, "Content-Type": "application/json"},
        json=body,
    )
    if resp.status_code == 401:
        raise PermissionError("token rejected")
    if not resp.ok:
        import json as _json
        print(f"\n--- Request body sent ---")
        print(_json.dumps(body, indent=2))
        print(f"--- E*TRADE error response ({resp.status_code}) ---")
        print(resp.text)
        print("---\n")
    resp.raise_for_status()
    return resp.json()


def api_put_json(url, consumer_key, consumer_secret, access_token, access_token_secret, body):
    header = sign_request(
        "PUT", url, consumer_key, consumer_secret, access_token, access_token_secret
    )
    resp = requests.put(
        url,
        headers={"Authorization": header, "Content-Type": "application/json"},
        json=body,
    )
    if resp.status_code == 401:
        raise PermissionError("token rejected")
    if not resp.ok:
        print(f"\n--- E*TRADE error response ({resp.status_code}) ---")
        print(resp.text)
        print("---\n")
    resp.raise_for_status()
    return resp.json()


def select_account(consumer_key, consumer_secret, access_token, access_token_secret, last4):
    """Find the account whose accountId ends in last4. Exits process with a clear
    listing if zero or multiple matches, so we never silently trade the wrong account."""
    accounts = get_account_list(consumer_key, consumer_secret, access_token, access_token_secret)
    account_list = accounts.get("AccountListResponse", {}).get("Accounts", {}).get("Account", [])

    matches = [a for a in account_list if str(a.get("accountId", "")).endswith(last4)]
    if not matches:
        print(f"No account found ending in {last4}. Accounts on this login:")
        for a in account_list:
            print(f"  {a.get('accountId')} - {a.get('accountDesc')} ({a.get('institutionType', 'BROKERAGE')})")
        raise SystemExit(1)
    if len(matches) > 1:
        print(f"Multiple accounts end in {last4} — refine the match.")
        raise SystemExit(1)

    account = matches[0]
    print(f"Using account: {account.get('accountDesc', account.get('accountId'))} (...{last4})")
    return account


def build_option_instrument(symbol, expiry, strike, order_action, quantity):
    return {
        "Product": {
            "securityType": "OPTN",
            "symbol": symbol,
            "callPut": "PUT",
            "expiryYear": str(expiry.year),
            "expiryMonth": str(expiry.month),
            "expiryDay": str(expiry.day),
            "strikePrice": str(strike),
        },
        "orderAction": order_action,
        "quantityType": "QUANTITY",
        "quantity": str(quantity),
    }


def build_single_leg_order(symbol, expiry, strike, order_action, quantity,
                            price_type, limit_price=None, client_order_id=None):
    if client_order_id is None:
        client_order_id = f"cli{int(datetime.datetime.now().timestamp())}"

    instrument = build_option_instrument(symbol, expiry, strike, order_action, quantity)

    order_detail = {
        "allOrNone": "false",
        "priceType": price_type,
        "orderTerm": "GOOD_FOR_DAY",
        "marketSession": "REGULAR",
        "Instrument": [instrument],
    }
    if price_type == "LIMIT":
        if limit_price is None:
            raise ValueError("limit_price required for LIMIT orders")
        order_detail["limitPrice"] = str(limit_price)

    request_body = {
        "PreviewOrderRequest": {
            "orderType": "OPTN",
            "clientOrderId": client_order_id,
            "Order": [order_detail],
        }
    }
    return request_body, client_order_id


def build_spread_order(leg_a, leg_b, quantity, price_type, limit_price=None, client_order_id=None):
    """
    leg_a / leg_b: dicts with keys symbol, expiry (date), strike, order_action.
    Order of legs matters for some E*TRADE validation rules (buy leg first
    per error 2054) — pass the BUY leg as leg_a when in doubt.

    Builds a 2-leg SPREADS order. Confirmed working for same-direction
    opening spreads (e.g. SELL_OPEN short put + BUY_OPEN long put = a
    credit spread). NOT usable for rolls (close + open combined) — that
    was tested and rejected by E*TRADE with an undocumented error; see
    roll_order.py, which places the close and open as two separate orders
    instead.

    IMPORTANT: SPREADS orders don't use priceType LIMIT/MARKET like single-leg
    OPTN orders do. NET_CREDIT and NET_DEBIT are confirmed accepted values.
    MARKET is accepted as a value but subject to E*TRADE's after-hours/
    opening-minutes restriction on opening market orders (error 3029) —
    that's a market-hours rule, not a request-shape problem. EVEN was tried
    and rejected outright by E*TRADE (error 101, "invalid input for
    priceType") despite being referenced in their own error-message text
    for a different error code — don't use it. price_type passed in should
    be NET_CREDIT, NET_DEBIT, or MARKET; callers decide credit vs debit
    based on the sign of the number the user enters (positive = credit =
    money received, negative = debit = money paid).
    """
    if client_order_id is None:
        client_order_id = f"cli{int(datetime.datetime.now().timestamp())}"

    instruments = [
        build_option_instrument(
            leg_a["symbol"], leg_a["expiry"], leg_a["strike"],
            leg_a["order_action"], quantity,
        ),
        build_option_instrument(
            leg_b["symbol"], leg_b["expiry"], leg_b["strike"],
            leg_b["order_action"], quantity,
        ),
    ]

    order_detail = {
        "allOrNone": "false",
        "priceType": price_type,
        "orderTerm": "GOOD_FOR_DAY",
        "marketSession": "REGULAR",
        "Instrument": instruments,
    }
    if price_type in ("NET_CREDIT", "NET_DEBIT"):
        if limit_price is None:
            raise ValueError("limit_price required for NET_CREDIT/NET_DEBIT orders")
        order_detail["limitPrice"] = str(abs(limit_price))
    # EVEN (net-zero cost) and MARKET take no limitPrice at all.

    request_body = {
        "PreviewOrderRequest": {
            "orderType": "SPREADS",
            "clientOrderId": client_order_id,
            "Order": [order_detail],
        }
    }
    return request_body, client_order_id


def preview_order(consumer_key, consumer_secret, access_token, access_token_secret,
                   account_id_key, request_body):
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/orders/preview.json"
    return api_post_json(url, consumer_key, consumer_secret, access_token, access_token_secret, request_body)


def place_order(consumer_key, consumer_secret, access_token, access_token_secret,
                 account_id_key, order_type, client_order_id, order_detail, preview_ids):
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/orders/place.json"
    body = {
        "PlaceOrderRequest": {
            "orderType": order_type,
            "clientOrderId": client_order_id,
            "Order": [order_detail],
            "PreviewIds": [{"previewId": pid} for pid in preview_ids],
        }
    }
    return api_post_json(url, consumer_key, consumer_secret, access_token, access_token_secret, body)


def list_orders(consumer_key, consumer_secret, access_token, access_token_secret,
                 account_id_key, status=None, symbol=None, count=50):
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/orders.json"
    params = {"count": count}
    if status:
        params["status"] = status
    if symbol:
        params["symbol"] = symbol
    return api_get(url, consumer_key, consumer_secret, access_token, access_token_secret, params=params)


def get_order_status(consumer_key, consumer_secret, access_token, access_token_secret,
                      account_id_key, order_id):
    """
    Look up a specific order's current status by scanning recent orders.
    Returns the status string (e.g. 'EXECUTED', 'OPEN', 'CANCELLED') or
    None if not found. Used to confirm a limit order actually filled
    before firing the second leg of a roll.
    """
    data = list_orders(consumer_key, consumer_secret, access_token, access_token_secret,
                        account_id_key, count=50)
    orders = data.get("OrdersResponse", {}).get("Order", [])
    if isinstance(orders, dict):
        orders = [orders]
    for order in orders:
        if order.get("orderId") == order_id:
            details = order.get("OrderDetail", [])
            if isinstance(details, dict):
                details = [details]
            for d in details:
                if d.get("status"):
                    return d.get("status")
    return None


def get_portfolio(consumer_key, consumer_secret, access_token, access_token_secret, account_id_key):
    """
    Current open POSITIONS (filled and still held) — distinct from list_orders,
    which reflects ORDER status (working/executed/cancelled), not what you
    currently hold. A filled SELL_OPEN order becomes an EXECUTED order but its
    resulting short position lives here, in the portfolio, until closed.
    """
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/portfolio.json"
    return api_get(url, consumer_key, consumer_secret, access_token, access_token_secret)


def cancel_order(consumer_key, consumer_secret, access_token, access_token_secret,
                  account_id_key, order_id):
    url = f"{LIVE_BASE_URL}/v1/accounts/{account_id_key}/orders/cancel.json"
    body = {"CancelOrderRequest": {"orderId": order_id}}
    return api_put_json(url, consumer_key, consumer_secret, access_token, access_token_secret, body)


def extract_preview_summary(preview_json):
    """Pull the key numbers out of a PreviewOrderResponse for display.

    NOTE on scope: everything here comes from E*TRADE's real-time buying-power
    math (Reg-T / PM margin, cash, day-trading) plus the portfolio-margin
    house-excess-equity fields, which is the full risk picture the public API
    exposes. E*TRADE also appears to run a separate, non-real-time house risk
    assessment (the kind that can flag an account after the fact) that isn't
    part of this API and isn't reflected here — this preview can't promise to
    catch that, only the every-order buying-power math.
    """
    resp = preview_json.get("PreviewOrderResponse", {})
    orders = resp.get("Order", [])
    order = orders[0] if orders else {}

    preview_ids = [p["previewId"] for p in resp.get("PreviewIds", [])]

    total_order_value = resp.get("totalOrderValue")
    est_commission = order.get("estimatedCommission")

    def _bp_block(block):
        marginable = block.get("marginable", block.get("settled", {}))
        return {
            "current_bp": marginable.get("currentBp"),
            "order_impact": marginable.get("currentOrderImpact"),
            "net_bp": marginable.get("netBp"),
        }

    margin_bp = _bp_block(resp.get("marginBpDetails", {}))
    cash_bp = _bp_block(resp.get("cashBpDetails", {}))
    dt_bp = _bp_block(resp.get("dtBpDetails", {}))

    pm = resp.get("portfolioMargin", resp.get("PortfolioMargin", {}))
    pm_eligible = pm.get("pmEligible")
    house_excess_curr = pm.get("houseExcessEquityCurr")
    house_excess_new = pm.get("houseExcessEquityNew")
    house_excess_change = pm.get("houseExcessEquityChange")

    messages = []
    msg_list = resp.get("messageList", resp.get("Messages", {}))
    for m in msg_list.get("Message", []) if isinstance(msg_list, dict) else []:
        messages.append(f"[{m.get('type')}] {m.get('description')}")

    return {
        "preview_ids": preview_ids,
        "total_order_value": total_order_value,
        "estimated_commission": est_commission,
        # kept for backward compat with any code reading these directly
        "current_bp": margin_bp["current_bp"],
        "order_impact": margin_bp["order_impact"],
        "net_bp": margin_bp["net_bp"],
        "margin_bp": margin_bp,
        "cash_bp": cash_bp,
        "dt_bp": dt_bp,
        "pm_eligible": pm_eligible,
        "house_excess_curr": house_excess_curr,
        "house_excess_new": house_excess_new,
        "house_excess_change": house_excess_change,
        "messages": messages,
        "raw_order": order,
    }


def print_preview_summary(summary, header_line, price_type, limit_price=None):
    print("\n--- ORDER PREVIEW ---")
    print(header_line)
    if price_type in ("LIMIT", "NET_CREDIT", "NET_DEBIT") and limit_price is not None:
        print(f"{price_type.replace('_', ' ').title()} price: {limit_price}")
    if summary["total_order_value"] is not None:
        print(f"Estimated total order value: {summary['total_order_value']}")
    if summary["estimated_commission"] is not None:
        print(f"Estimated commission: {summary['estimated_commission']}")

    def _print_bp_line(label, block):
        if block["current_bp"] is None and block["order_impact"] is None and block["net_bp"] is None:
            return
        parts = []
        if block["current_bp"] is not None:
            parts.append(f"current={block['current_bp']}")
        if block["order_impact"] is not None:
            parts.append(f"order impact={block['order_impact']}")
        if block["net_bp"] is not None:
            parts.append(f"after order={block['net_bp']}")
        print(f"  {label}: " + "  ".join(parts))

    print("\nBuying power:")
    _print_bp_line("Margin", summary["margin_bp"])
    _print_bp_line("Cash", summary["cash_bp"])
    _print_bp_line("Day-trading", summary["dt_bp"])

    if summary["pm_eligible"] is not None:
        print(f"\nPortfolio Margin eligible: {summary['pm_eligible']}")
    if summary["house_excess_curr"] is not None or summary["house_excess_new"] is not None:
        print(f"House excess equity — current: {summary['house_excess_curr']}   "
              f"after order: {summary['house_excess_new']}   "
              f"change: {summary['house_excess_change']}")

    print("\n(Note: this reflects E*TRADE's real-time buying-power math only.")
    print(" It does not include any separate, after-the-fact house risk")
    print(" assessment E*TRADE may run on the account — this preview cannot")
    print(" guarantee that kind of review won't happen after the order fills.)")

    if summary["messages"]:
        print("\nMessages from E*TRADE:")
        for m in summary["messages"]:
            print(f"  {m}")
    print("\n--- END PREVIEW ---")
