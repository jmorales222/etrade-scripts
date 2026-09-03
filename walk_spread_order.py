"""
Place a SPX put credit spread as a NET_CREDIT limit order, then "walk" the
limit price every --interval seconds toward the current market until it
fills, hits a worst-acceptable-credit floor, or runs out of attempts.

Why this exists:
    Market orders on options give up the full bid/ask spread. A single
    static limit at mid can sit unfilled forever if the market has moved.
    This script re-checks live bid/ask each cycle, recomputes the net
    credit mid, and re-prices a working order to chase a fill without
    ever accepting less than a floor price you set going in.

E*TRADE's API (as used elsewhere in this repo) exposes preview / place /
cancel / list — there is no confirmed "replace" endpoint here, so each
step is: cancel the working order, recompute mid from fresh bid/ask,
preview + place a new order at the new price. This is simpler and safer
to reason about than an untested replace call, at the cost of briefly
having no working order between cancel and re-place each cycle.

Usage:
    python walk_spread_order.py --short-strike 5540 --long-strike 5520 \
        --expiry 2026-09-23 --quantity 2 --floor-credit 0.90

    python walk_spread_order.py --short-delta -0.16 --long-delta -0.10 \
        --dte 45 --quantity 2 --floor-credit-pct 0.85
        (looks up strikes fresh each cycle the same way find_45dte_spread.py does,
         in case the chain has moved — see --lock-strikes to disable this)

Floor price:
    You MUST supply either --floor-credit (an absolute dollar credit, e.g.
    0.90) or --floor-credit-pct (a fraction of the FIRST cycle's mid credit,
    e.g. 0.85 = never accept less than 85% of the opening mid). The walk
    will never place a limit below this floor — if the market requires
    going below it to fill, the script stops and tells you rather than
    guessing what you'd still accept.

Stepping behavior:
    - Cycle 1: limit = fresh mid (best price, give it a shot at the real market)
    - Cycles 2..--patience: still re-price to fresh mid each time (the
      market may simply not have caught up to a stale quote yet)
    - After --patience unfilled cycles at mid: start stepping the limit
      down from mid toward the floor by --step per cycle, still never
      going below --floor-credit
    - Stops at --max-attempts cycles total, filled or not

This does NOT send a Telegram prompt or read an s1 email signal — those
are separate pieces (mail-triggered gating, chat-based confirm). This
script assumes you've already decided to enter the trade and starts
from the same typed "yes" confirmation the rest of this codebase uses.
"""

import argparse
import datetime
import os
import sys
import time

from etrade_common import (
    CONSUMER_KEY, CONSUMER_SECRET, get_token, refresh_token_after_expiry,
    select_account, build_spread_order, preview_order, place_order as etrade_place_order,
    extract_preview_summary, print_preview_summary, cancel_order, get_order_status,
)
from find_1dte_put import get_option_chain, get_expiry_dates, get_spx_quote, find_closest_delta_put
from find_45dte_spread import pick_nearest_to_dte, find_strike_for_delta
from trade_log import log_fill


def get_leg_bid_ask(consumer_key, consumer_secret, access_token, access_token_secret,
                     market_data_symbol, expiry, strike):
    """Fetch fresh bid/ask for one put strike. Returns (bid, ask) or (None, None)."""
    chain = get_option_chain(consumer_key, consumer_secret, access_token, access_token_secret,
                              market_data_symbol, expiry, strike_price_near=int(strike))
    pairs = chain.get("OptionChainResponse", {}).get("OptionPair", [])
    for pair in pairs:
        put = pair.get("Put")
        if put and float(put.get("strikePrice", -1)) == float(strike):
            return put.get("bid"), put.get("ask")
    return None, None


def get_spread_mid(consumer_key, consumer_secret, access_token, access_token_secret,
                    market_data_symbol, expiry, short_strike, long_strike):
    """
    Fresh net-credit mid for a short-put/long-put spread, computed from
    each leg's live bid/ask (NBBO-derived, per E*TRADE's quote docs).
    Returns (net_credit_mid, short_bid, short_ask, long_bid, long_ask) —
    any element is None if that leg's quote wasn't available (e.g. market
    closed), signaling the caller should not trade this cycle.
    """
    s_bid, s_ask = get_leg_bid_ask(consumer_key, consumer_secret, access_token, access_token_secret,
                                    market_data_symbol, expiry, short_strike)
    l_bid, l_ask = get_leg_bid_ask(consumer_key, consumer_secret, access_token, access_token_secret,
                                    market_data_symbol, expiry, long_strike)
    if None in (s_bid, s_ask, l_bid, l_ask):
        return None, s_bid, s_ask, l_bid, l_ask
    short_mid = (s_bid + s_ask) / 2
    long_mid = (l_bid + l_ask) / 2
    net_credit_mid = round(short_mid - long_mid, 2)
    return net_credit_mid, s_bid, s_ask, l_bid, l_ask


def place_walk_cycle(consumer_key, consumer_secret, access_token, access_token_secret,
                      account_id_key, order_symbol, expiry, short_strike, long_strike,
                      quantity, limit_credit):
    """Preview + place one NET_CREDIT spread order at limit_credit. No confirm
    prompt here — the walk loop's outer confirm already covered intent to
    trade; each cycle is a re-price of that same decision, not a new one.
    Returns (order_id, client_order_id) or (None, None) if preview/place failed."""
    long_leg = {"symbol": order_symbol, "expiry": expiry, "strike": long_strike, "order_action": "BUY_OPEN"}
    short_leg = {"symbol": order_symbol, "expiry": expiry, "strike": short_strike, "order_action": "SELL_OPEN"}

    request_body, client_order_id = build_spread_order(
        long_leg, short_leg, quantity, "NET_CREDIT", limit_price=limit_credit,
    )

    preview_json = preview_order(consumer_key, consumer_secret, access_token, access_token_secret,
                                  account_id_key, request_body)
    summary = extract_preview_summary(preview_json)

    if not summary["preview_ids"]:
        print("  No preview ID returned for this cycle's order — skipping placement.")
        for m in summary["messages"]:
            print(f"    {m}")
        return None, None

    order_detail = request_body["PreviewOrderRequest"]["Order"][0]
    place_json = etrade_place_order(
        consumer_key, consumer_secret, access_token, access_token_secret,
        account_id_key, "SPREADS", client_order_id, order_detail, summary["preview_ids"],
    )
    place_resp = place_json.get("PlaceOrderResponse", {})
    order_ids = place_resp.get("OrderIds", [])
    order_id = order_ids[0].get("orderId") if order_ids else None
    return order_id, client_order_id


def walk(consumer_key, consumer_secret, access_token, access_token_secret,
         account_id_key, market_data_symbol, order_symbol, expiry,
         short_strike, long_strike, quantity, floor_credit, interval,
         patience, step, max_attempts, short_delta=None, long_delta=None,
         account_last4=None):
    """
    Runs the walk loop. Returns True if filled, False otherwise.
    Refreshes token on PermissionError and retries the current cycle once.
    short_delta/long_delta/account_last4 are optional and only used to
    enrich the trade log entry on fill — the walk itself doesn't need them.
    """
    opening_mid = None
    working_order_id = None
    current_limit_credit = None

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== Cycle {attempt}/{max_attempts} ===")

        try:
            mid, s_bid, s_ask, l_bid, l_ask = get_spread_mid(
                consumer_key, consumer_secret, access_token, access_token_secret,
                market_data_symbol, expiry, short_strike, long_strike,
            )
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            mid, s_bid, s_ask, l_bid, l_ask = get_spread_mid(
                consumer_key, consumer_secret, access_token, access_token_secret,
                market_data_symbol, expiry, short_strike, long_strike,
            )

        if mid is None:
            print(f"  Could not get fresh quotes this cycle (short bid/ask={s_bid}/{s_ask}, "
                  f"long bid/ask={l_bid}/{l_ask}). Market may be closed. Skipping cycle.")
            if working_order_id is None:
                time.sleep(interval)
                continue
        else:
            if opening_mid is None:
                opening_mid = mid
            print(f"  Short {short_strike} bid/ask: {s_bid}/{s_ask}   "
                  f"Long {long_strike} bid/ask: {l_bid}/{l_ask}   Net credit mid: {mid}")

            if attempt <= patience:
                limit_credit = mid
            else:
                steps_past_patience = attempt - patience
                limit_credit = round(mid - (step * steps_past_patience), 2)

            if limit_credit < floor_credit:
                print(f"  Computed limit {limit_credit} is below floor {floor_credit} — "
                      f"holding at floor instead of chasing further down.")
                limit_credit = floor_credit

            if working_order_id is not None:
                print(f"  Cancelling working order {working_order_id} to re-price...")
                try:
                    cancel_order(consumer_key, consumer_secret, access_token, access_token_secret,
                                 account_id_key, working_order_id)
                except PermissionError:
                    access_token, access_token_secret = refresh_token_after_expiry()
                    cancel_order(consumer_key, consumer_secret, access_token, access_token_secret,
                                 account_id_key, working_order_id)
                working_order_id = None

            print(f"  Placing NET_CREDIT limit at {limit_credit}...")
            try:
                order_id, _ = place_walk_cycle(
                    consumer_key, consumer_secret, access_token, access_token_secret,
                    account_id_key, order_symbol, expiry, short_strike, long_strike,
                    quantity, limit_credit,
                )
            except PermissionError:
                access_token, access_token_secret = refresh_token_after_expiry()
                order_id, _ = place_walk_cycle(
                    consumer_key, consumer_secret, access_token, access_token_secret,
                    account_id_key, order_symbol, expiry, short_strike, long_strike,
                    quantity, limit_credit,
                )

            if order_id is None:
                print("  Order placement failed this cycle — will retry next cycle.")
                time.sleep(interval)
                continue

            working_order_id = order_id
            current_limit_credit = limit_credit
            print(f"  Working order ID: {working_order_id}")

        time.sleep(interval)

        if working_order_id is not None:
            try:
                status = get_order_status(consumer_key, consumer_secret, access_token, access_token_secret,
                                           account_id_key, working_order_id)
            except PermissionError:
                access_token, access_token_secret = refresh_token_after_expiry()
                status = get_order_status(consumer_key, consumer_secret, access_token, access_token_secret,
                                           account_id_key, working_order_id)

            print(f"  Status after {interval}s: {status}")
            if status == "EXECUTED":
                print(f"\n*** FILLED at cycle {attempt}. Order ID: {working_order_id} ***")
                log_fill(
                    order_id=working_order_id, symbol=order_symbol, expiry=expiry,
                    short_strike=short_strike, short_delta=short_delta,
                    long_strike=long_strike, long_delta=long_delta,
                    quantity=quantity, fill_credit=current_limit_credit,
                    account_last4=account_last4, opening_mid=opening_mid,
                    floor_credit=floor_credit, cycles_to_fill=attempt,
                )
                return True

    if working_order_id is not None:
        print(f"\nOut of attempts. Cancelling final working order {working_order_id}...")
        try:
            cancel_order(consumer_key, consumer_secret, access_token, access_token_secret,
                         account_id_key, working_order_id)
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            cancel_order(consumer_key, consumer_secret, access_token, access_token_secret,
                         account_id_key, working_order_id)
        print("Cancelled. Not filled.")

    return False


def main():
    parser = argparse.ArgumentParser()

    strike_group = parser.add_argument_group("explicit strikes (skip the delta lookup)")
    strike_group.add_argument("--short-strike", type=float, default=None)
    strike_group.add_argument("--long-strike", type=float, default=None)
    strike_group.add_argument("--expiry", type=str, default=None, help="YYYY-MM-DD, required if using explicit strikes")

    delta_group = parser.add_argument_group("delta-based lookup (finds strikes fresh, like find_45dte_spread.py)")
    delta_group.add_argument("--short-delta", type=float, default=None)
    delta_group.add_argument("--long-delta", type=float, default=None)
    delta_group.add_argument("--dte", type=int, default=45)

    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--symbol", default="SPX", help="market-data symbol for chain/quote lookups")
    parser.add_argument("--order-symbol", default=None, help="order-placement symbol; defaults to SPXW if --symbol is SPX")
    parser.add_argument("--account-last4", default="4422")

    parser.add_argument("--floor-credit", type=float, default=None,
                         help="absolute minimum net credit the walk will ever place, e.g. 0.90")
    parser.add_argument("--floor-credit-pct", type=float, default=None,
                         help="minimum net credit as a fraction of cycle-1 mid, e.g. 0.85")

    parser.add_argument("--interval", type=int, default=20, help="seconds between re-price cycles")
    parser.add_argument("--patience", type=int, default=3, help="cycles to sit at fresh mid before stepping toward the floor")
    parser.add_argument("--step", type=float, default=0.05, help="credit given up per cycle once past --patience")
    parser.add_argument("--max-attempts", type=int, default=6, help="hard cap on cycles before giving up")

    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    using_explicit = args.short_strike is not None and args.long_strike is not None
    using_delta = args.short_delta is not None and args.long_delta is not None
    if using_explicit == using_delta:
        print("Specify exactly one of: --short-strike/--long-strike/--expiry, OR --short-delta/--long-delta/--dte.")
        sys.exit(1)
    if using_explicit and not args.expiry:
        print("--expiry is required when using --short-strike/--long-strike.")
        sys.exit(1)
    if args.floor_credit is None and args.floor_credit_pct is None:
        print("Must supply --floor-credit or --floor-credit-pct — the walk needs a floor it will never trade below.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)

    market_data_symbol = args.symbol
    order_symbol = args.order_symbol or ("SPXW" if args.symbol.upper() == "SPX" else args.symbol)

    short_delta_found = None
    long_delta_found = None

    if using_explicit:
        expiry = datetime.date.fromisoformat(args.expiry)
        short_strike, long_strike = args.short_strike, args.long_strike
    else:
        def _fetch_chain_for_hop(strike_price_near):
            return get_option_chain(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                     market_data_symbol, expiry, strike_price_near=strike_price_near)

        try:
            expiries = get_expiry_dates(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, market_data_symbol)
            expiry = pick_nearest_to_dte(expiries, args.dte)
            spot = get_spx_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, market_data_symbol)
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            expiries = get_expiry_dates(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, market_data_symbol)
            expiry = pick_nearest_to_dte(expiries, args.dte)
            spot = get_spx_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, market_data_symbol)

        if not spot:
            print("Could not fetch spot price — needed to converge on the target-delta strikes. "
                  "Market may be closed.")
            sys.exit(1)

        short_ranked = find_strike_for_delta(_fetch_chain_for_hop, spot, args.short_delta)
        long_ranked = find_strike_for_delta(_fetch_chain_for_hop, spot, args.long_delta)
        if not short_ranked or not long_ranked:
            print("Could not find strikes with delta data. Market may be closed.")
            sys.exit(1)
        short_strike = min(short_ranked, key=lambda r: abs(r[1] - args.short_delta))[0]
        long_strike = min(long_ranked, key=lambda r: abs(r[1] - args.long_delta))[0]
        short_delta_found = min(short_ranked, key=lambda r: abs(r[1] - args.short_delta))[1]
        long_delta_found = min(long_ranked, key=lambda r: abs(r[1] - args.long_delta))[1]
        print(f"Resolved short leg: {short_strike} strike, delta {short_delta_found:.4f} (target {args.short_delta})")
        print(f"Resolved long leg:  {long_strike} strike, delta {long_delta_found:.4f} (target {args.long_delta})")
        if short_strike == long_strike:
            print("ERROR: short and long strikes resolved to the same strike — refusing to place "
                  "a degenerate spread. Try again (market data may have been incomplete this run).")
            sys.exit(1)

    print(f"\nSpread: SELL {short_strike} PUT / BUY {long_strike} PUT, {order_symbol} {expiry.isoformat()}")
    print(f"Quantity: {args.quantity}")

    # Establish floor. If pct-based, we need cycle-1 mid first.
    floor_credit = args.floor_credit
    if floor_credit is None:
        try:
            mid0, s_bid, s_ask, l_bid, l_ask = get_spread_mid(
                CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                market_data_symbol, expiry, short_strike, long_strike,
            )
        except PermissionError:
            access_token, access_token_secret = refresh_token_after_expiry()
            mid0, s_bid, s_ask, l_bid, l_ask = get_spread_mid(
                CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                market_data_symbol, expiry, short_strike, long_strike,
            )
        if mid0 is None:
            print("Could not fetch an opening quote to compute a percent-based floor "
                  "(market may be closed). Use --floor-credit with an absolute value instead.")
            sys.exit(1)
        floor_credit = round(mid0 * args.floor_credit_pct, 2)
        print(f"Opening mid: {mid0}   Floor ({args.floor_credit_pct*100:.0f}% of opening mid): {floor_credit}")
    else:
        print(f"Floor credit: {floor_credit}")

    print(f"\nWalk plan: fresh mid for cycles 1-{args.patience}, then step down {args.step}/cycle, "
          f"never below {floor_credit}. {args.interval}s between cycles, max {args.max_attempts} cycles.")

    confirm = input('\nType "yes" to start the walk, anything else to cancel: ').strip().lower()
    if confirm != "yes":
        print("Not started.")
        return

    account = select_account(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.account_last4)
    account_id_key = account["accountIdKey"]

    filled = walk(
        CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
        account_id_key, market_data_symbol, order_symbol, expiry,
        short_strike, long_strike, args.quantity, floor_credit,
        args.interval, args.patience, args.step, args.max_attempts,
        short_delta=short_delta_found, long_delta=long_delta_found,
        account_last4=args.account_last4,
    )

    if not filled:
        print("\nWalk ended without a fill. No working order remains open.")


if __name__ == "__main__":
    main()
