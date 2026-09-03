"""
Append filled spread trades to a CSV log in a synced Dropbox folder.

No Dropbox API, no auth — this just writes a plain CSV file to a local
path that the Dropbox desktop client is already watching and syncing.
As long as that client is running, the file shows up in Dropbox with
no further setup.

Path resolution:
    Set DROPBOX_TRADE_LOG_DIR to override where the CSV lives. If unset,
    defaults to Jonathan's synced folder:
        /mnt/business/jonathan/investments
    The CSV filename itself is fixed: spx_spread_trades.csv

Usage as a library (this is how walk_spread_order.py calls it):
    from trade_log import log_fill
    log_fill(
        order_id=39, symbol="SPXW", expiry=date(2026, 10, 16),
        short_strike=7360.0, short_delta=-0.1601,
        long_strike=7190.0, long_delta=-0.1034,
        quantity=1, fill_credit=12.2, opening_mid=12.3,
        floor_credit=10.46, cycles_to_fill=1, account_last4="4422",
    )

Usage standalone (manual backfill of a trade not placed by the walk script):
    python trade_log.py --order-id 39 --symbol SPXW --expiry 2026-10-16 \
        --short-strike 7360 --short-delta -0.1601 \
        --long-strike 7190 --long-delta -0.1034 \
        --quantity 1 --fill-credit 12.2 --account-last4 4422
    (--opening-mid, --floor-credit, --cycles-to-fill are optional for
     manual backfill entries since a hand-placed trade may not have them)
"""

import argparse
import csv
import datetime
import os

CSV_FILENAME = "spx_spread_trades.csv"

DEFAULT_DROPBOX_DIR = "/mnt/business/jonathan/investments"

FIELDNAMES = [
    "filled_at",          # ISO timestamp of when this row was logged
    "order_id",
    "account_last4",
    "symbol",              # e.g. SPXW
    "expiry",               # YYYY-MM-DD
    "short_strike",
    "short_delta",
    "long_strike",
    "long_delta",
    "width",                # short_strike - long_strike
    "quantity",
    "fill_credit",          # actual net credit received per spread
    "opening_mid",          # mid at cycle 1, if this came from walk_spread_order.py
    "floor_credit",         # walk floor, if applicable
    "cycles_to_fill",       # how many walk cycles it took, if applicable
    "total_credit",         # fill_credit * quantity * 100
]


def _log_path():
    directory = os.environ.get("DROPBOX_TRADE_LOG_DIR", DEFAULT_DROPBOX_DIR)
    return os.path.join(directory, CSV_FILENAME), directory


def log_fill(order_id, symbol, expiry, short_strike, short_delta,
             long_strike, long_delta, quantity, fill_credit,
             account_last4, opening_mid=None, floor_credit=None,
             cycles_to_fill=None):
    """
    Append one row to the trade log CSV. Creates the file (with header)
    if it doesn't exist yet. Returns the path written to, or None if the
    write failed (this never raises — a logging failure should not take
    down a script that just successfully placed a real trade).
    """
    path, directory = _log_path()

    if not os.path.isdir(directory):
        print(f"  [trade_log] WARNING: Dropbox directory not found: {directory}")
        print(f"  [trade_log] Trade was filled but NOT logged. Log this manually.")
        return None

    width = None
    total_credit = None
    try:
        width = round(float(short_strike) - float(long_strike), 2)
    except (TypeError, ValueError):
        pass
    try:
        total_credit = round(float(fill_credit) * float(quantity) * 100, 2)
    except (TypeError, ValueError):
        pass

    row = {
        "filled_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "order_id": order_id,
        "account_last4": account_last4,
        "symbol": symbol,
        "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
        "short_strike": short_strike,
        "short_delta": short_delta,
        "long_strike": long_strike,
        "long_delta": long_delta,
        "width": width,
        "quantity": quantity,
        "fill_credit": fill_credit,
        "opening_mid": opening_mid,
        "floor_credit": floor_credit,
        "cycles_to_fill": cycles_to_fill,
        "total_credit": total_credit,
    }

    file_exists = os.path.isfile(path)
    try:
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        print(f"  [trade_log] WARNING: could not write to {path}: {e}")
        print(f"  [trade_log] Trade was filled but NOT logged. Log this manually.")
        return None

    print(f"  [trade_log] Logged to {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="Manually log a trade to the CSV (e.g. one placed outside walk_spread_order.py).")
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    parser.add_argument("--short-strike", type=float, required=True)
    parser.add_argument("--short-delta", type=float, required=True)
    parser.add_argument("--long-strike", type=float, required=True)
    parser.add_argument("--long-delta", type=float, required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--fill-credit", type=float, required=True)
    parser.add_argument("--account-last4", required=True)
    parser.add_argument("--opening-mid", type=float, default=None)
    parser.add_argument("--floor-credit", type=float, default=None)
    parser.add_argument("--cycles-to-fill", type=int, default=None)
    args = parser.parse_args()

    expiry = datetime.date.fromisoformat(args.expiry)
    log_fill(
        order_id=args.order_id, symbol=args.symbol, expiry=expiry,
        short_strike=args.short_strike, short_delta=args.short_delta,
        long_strike=args.long_strike, long_delta=args.long_delta,
        quantity=args.quantity, fill_credit=args.fill_credit,
        account_last4=args.account_last4, opening_mid=args.opening_mid,
        floor_credit=args.floor_credit, cycles_to_fill=args.cycles_to_fill,
    )


if __name__ == "__main__":
    main()
