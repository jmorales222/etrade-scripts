"""
Find the ~1-delta (target -0.01) 1DTE SPX put on E*TRADE and report how
far its strike sits from spot, in points and percent.

Usage:
    python find_1dte_put.py
    python find_1dte_put.py --delta -0.02       # override target delta

Notes:
- Use --symbol SPX (the default). This script calls E*TRADE's market-data
  endpoints (optionexpiredate, optionchains), which want the bare index
  ticker SPX, not SPXW. SPXW is a different subsystem's symbol — it's what
  place_order.py / roll_order.py need for the actual order legs, since
  those endpoints key off the tradable weekly root instead of the index.
  Passing --symbol SPXW here will 400.
- E*TRADE's chain endpoint returns Greeks (including delta) only when
  you pass includeGreeks=true, and only reliably for near-term expiries.
- "1DTE" means the nearest expiration that is NOT today. The script
  finds tomorrow's (or the next available) expiration automatically.
"""

import argparse
import datetime
import os
import sys

import requests

from etrade_auth import LIVE_BASE_URL, run_interactive_login, sign_request
from show_balance import load_cached_token, save_token, TOKEN_CACHE_FILE

CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "")


def get_token(consumer_key, consumer_secret):
    cached = load_cached_token()
    if cached:
        return cached["oauth_token"], cached["oauth_token_secret"]
    access_token, access_token_secret = run_interactive_login(consumer_key, consumer_secret)
    save_token(access_token, access_token_secret)
    return access_token, access_token_secret


def api_get(url, consumer_key, consumer_secret, access_token, access_token_secret, params=None):
    params = params or {}
    header = sign_request(
        "GET", url, consumer_key, consumer_secret, access_token, access_token_secret,
        extra_params=params,
    )
    resp = requests.get(url, headers={"Authorization": header}, params=params)
    if resp.status_code == 401:
        raise PermissionError("token rejected")
    if not resp.ok:
        print(f"\n--- E*TRADE error response ({resp.status_code}) ---")
        print(resp.text)
        print("---\n")
    resp.raise_for_status()
    return resp.json()


def get_expiry_dates(consumer_key, consumer_secret, access_token, access_token_secret, symbol):
    url = f"{LIVE_BASE_URL}/v1/market/optionexpiredate.json"
    data = api_get(url, consumer_key, consumer_secret, access_token, access_token_secret,
                    params={"symbol": symbol})
    dates = data.get("OptionExpireDateResponse", {}).get("ExpirationDate", [])
    if isinstance(dates, dict):
        dates = [dates]
    out = []
    for d in dates:
        out.append(datetime.date(int(d["year"]), int(d["month"]), int(d["day"])))
    return sorted(out)


def pick_1dte_expiry(expiries):
    """Nearest expiration strictly after today."""
    today = datetime.date.today()
    future = [d for d in expiries if d > today]
    if not future:
        raise RuntimeError("No future expirations found.")
    return future[0]


def get_option_chain(consumer_key, consumer_secret, access_token, access_token_secret,
                      symbol, expiry, strike_count=50, strike_price_near=None):
    url = f"{LIVE_BASE_URL}/v1/market/optionchains.json"
    params = {
        "symbol": symbol,
        "expiryYear": expiry.year,
        "expiryMonth": expiry.month,
        "expiryDay": expiry.day,
        "chainType": "PUT",
        "includeGreeks": "true",
        "noOfStrikes": strike_count,
        "optionCategory": "ALL",
    }
    if strike_price_near is not None:
        params["strikePriceNear"] = strike_price_near
    return api_get(url, consumer_key, consumer_secret, access_token, access_token_secret, params=params)


def get_spx_quote(consumer_key, consumer_secret, access_token, access_token_secret, symbol="SPX"):
    url = f"{LIVE_BASE_URL}/v1/market/quote/{symbol}.json"
    try:
        data = api_get(url, consumer_key, consumer_secret, access_token, access_token_secret)
    except requests.HTTPError as e:
        print(f"Quote fetch failed for {symbol}: {e}")
        return None
    quotes = data.get("QuoteResponse", {}).get("QuoteData", [])
    if not quotes:
        print(f"Quote fetch for {symbol} returned no QuoteData. Raw response: {data}")
        return None
    all_data = quotes[0].get("All", {})
    last = all_data.get("lastTrade")
    if last is None:
        # index quotes sometimes populate different fields than equities
        last = all_data.get("close") or all_data.get("previousClose")
    return last


def find_closest_delta_put(chain_json, target_delta):
    """Return sorted list of (strike, delta, put_data) by |delta - target|, ascending."""
    results = []
    pairs = chain_json.get("OptionChainResponse", {}).get("OptionPair", [])
    for pair in pairs:
        put = pair.get("Put")
        if not put:
            continue
        greeks = put.get("OptionGreeks", {})
        delta = greeks.get("delta")
        if delta is None:
            continue
        strike = put.get("strikePrice")
        results.append((strike, delta, put))

    if not results:
        return []

    results.sort(key=lambda r: abs(r[1] - target_delta))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delta", type=float, default=-0.01, help="target delta (negative for puts)")
    parser.add_argument("--symbol", type=str, default="SPX", help="underlying symbol for market-data lookups — use SPX, not SPXW (SPXW is for order placement only, see place_order.py)")
    parser.add_argument("--context", type=int, default=3, help="number of nearby strikes to show each side")
    parser.add_argument("--debug", action="store_true", help="print every strike/delta pair returned by the chain")
    parser.add_argument("--order", action="store_true", help="after finding the strike, launch the order preview/place flow")
    parser.add_argument("--order-symbol", default=None, help="symbol to use for order placement (defaults to SPXW if --symbol is SPX-based; only relevant with --order)")
    parser.add_argument("--account-last4", default="4422")
    args = parser.parse_args()

    if not CONSUMER_KEY or not CONSUMER_SECRET:
        print("Missing ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET env vars.")
        sys.exit(1)

    access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)

    def _fetch_spot():
        return get_spx_quote(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret)

    def _fetch_chain(expiry, strike_price_near=None):
        return get_option_chain(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret,
                                 args.symbol, expiry, strike_price_near=strike_price_near)

    def _run():
        expiries = get_expiry_dates(CONSUMER_KEY, CONSUMER_SECRET, access_token, access_token_secret, args.symbol)
        expiry = pick_1dte_expiry(expiries)
        spot = _fetch_spot()

        # First pass: anchor near spot to see the delta curve's shape/direction.
        chain = _fetch_chain(expiry, strike_price_near=int(spot) if spot else None)
        ranked = find_closest_delta_put(chain, args.delta)

        # If the closest we found is still far from target AND it sits at the
        # low-strike edge of what came back, the true target strike is likely
        # further OTM than this window reached — re-anchor lower and retry.
        if ranked and spot:
            by_strike = sorted(ranked, key=lambda r: r[0])
            lowest_strike, lowest_delta, _ = by_strike[0]
            closest_strike, closest_delta, _ = min(ranked, key=lambda r: abs(r[1] - args.delta))
            if closest_strike == lowest_strike and abs(closest_delta - args.delta) > 0.005:
                # Re-anchor ~ (spot - lowest_strike) further below the current floor.
                span = spot - lowest_strike
                new_anchor = int(lowest_strike - span) if span > 0 else int(spot * 0.9)
                chain2 = _fetch_chain(expiry, strike_price_near=new_anchor)
                ranked2 = find_closest_delta_put(chain2, args.delta)
                if ranked2:
                    chain, ranked = chain2, ranked2

        return expiry, chain, ranked, spot

    try:
        expiry, chain, ranked, spot = _run()
    except PermissionError:
        print("Cached token expired — re-authenticating...")
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
        access_token, access_token_secret = get_token(CONSUMER_KEY, CONSUMER_SECRET)
        expiry, chain, ranked, spot = _run()

    if not ranked:
        print("No puts with delta data returned. Market may be closed, or try again during RTH.")
        return

    if args.debug:
        print(f"\n--- DEBUG: all {len(ranked)} strikes with delta, sorted by strike ---")
        for strike, delta, put in sorted(ranked, key=lambda r: r[0]):
            print(f"  strike={strike}   delta={delta}")
        print("---\n")

    best_strike, best_delta, best_put = ranked[0]

    print(f"\nExpiry (1DTE): {expiry.isoformat()}")
    if spot:
        print(f"SPX spot: {spot}")
    print(f"Target delta: {args.delta}\n")

    print(f"Best match: {best_strike} strike put, delta {best_delta:.4f}")
    bid = best_put.get("bid")
    ask = best_put.get("ask")
    if bid is not None and ask is not None:
        print(f"  Bid/Ask: {bid} / {ask}")

    if spot:
        points_away = spot - best_strike
        pct_away = (points_away / spot) * 100
        print(f"  Distance from spot: {points_away:.2f} points ({pct_away:.2f}% OTM)")
    else:
        print("  (Could not fetch SPX spot price to compute distance.)")

    if args.context > 0:
        print(f"\nNearby strikes (sorted by strike):")
        by_strike = sorted(ranked, key=lambda r: r[0])
        best_idx = by_strike.index((best_strike, best_delta, best_put))
        lo = max(0, best_idx - args.context)
        hi = min(len(by_strike), best_idx + args.context + 1)
        for strike, delta, put in by_strike[lo:hi]:
            marker = " <== best match" if strike == best_strike else ""
            pts = f"{(spot - strike):.2f}pts / {((spot - strike) / spot * 100):.2f}%" if spot else "n/a"
            print(f"  {strike:>10}   delta={delta:.4f}   {pts}{marker}")

    if args.order:
        by_strike = sorted(ranked, key=lambda r: r[0])
        lo = max(0, by_strike.index((best_strike, best_delta, best_put)) - args.context)
        hi = min(len(by_strike), by_strike.index((best_strike, best_delta, best_put)) + args.context + 1)
        selectable = by_strike[lo:hi] if args.context > 0 else [(best_strike, best_delta, best_put)]

        print("\nSelect a strike to trade:")
        for i, (strike, delta, put) in enumerate(selectable):
            marker = " (best match)" if strike == best_strike else ""
            print(f"  [{i}] {strike} PUT   delta={delta:.4f}{marker}")

        default_idx = next(i for i, (s, d, p) in enumerate(selectable) if s == best_strike)
        choice = input(f"\nIndex to trade [{default_idx}], or Enter to skip: ").strip()
        if choice == "":
            chosen_idx = default_idx
        else:
            try:
                chosen_idx = int(choice)
                selectable[chosen_idx]
            except (ValueError, IndexError):
                print("Invalid selection — skipping order entry.")
                chosen_idx = None

        if chosen_idx is not None:
            chosen_strike = selectable[chosen_idx][0]
            # args.symbol is the market-data symbol (SPX) used for the chain lookup;
            # order placement needs the tradable root (SPXW for SPX-family options).
            # Same split as roll_order.py — these are different E*TRADE subsystems.
            order_symbol = args.order_symbol
            if order_symbol is None:
                order_symbol = "SPXW" if args.symbol.upper() == "SPX" else args.symbol
            from place_order import run_order_flow
            run_order_flow(order_symbol, chosen_strike, expiry, "SELL_OPEN", args.account_last4,
                           access_token, access_token_secret)


if __name__ == "__main__":
    main()
