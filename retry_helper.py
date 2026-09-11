"""
Retry-with-backoff for transient failures against E*TRADE's API.

E*TRADE's infrastructure has shown real intermittent issues (503s during
what looks like maintenance/capacity events, occasional flapping between
404/500 on the same endpoint seconds apart). Most of these clear up on
their own within seconds. This wraps a call so a single transient blip
doesn't kill a whole script — it does NOT retry PermissionError (expired
token — that's a different, non-transient failure the callers already
handle by re-authenticating), and it does NOT retry 4xx errors other than
408/429, since those (400, 401 after refresh, 404 with a real body, etc.)
usually mean something is actually wrong with the request, not a blip.

Usage:
    result = with_retry(lambda: get_account_list(...), what="get_account_list")
"""

import time

import requests

RETRYABLE_STATUS_CODES = {500, 502, 503, 504, 408, 429}


def with_retry(fn, what="request", max_attempts=4, base_delay=2, max_delay=20):
    """
    Call fn() (a zero-arg callable — wrap your real call in a lambda).
    Retries on connection errors, timeouts, and retryable HTTP status
    codes, with exponential backoff. Re-raises PermissionError immediately
    (callers handle token refresh themselves). Re-raises any other
    exception, or a non-retryable HTTPError, immediately without retrying.
    Raises the last exception if all attempts are exhausted.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except PermissionError:
            raise
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in RETRYABLE_STATUS_CODES:
                raise
            last_exc = e
            print(f"  [retry] {what} failed with HTTP {status} "
                  f"(attempt {attempt}/{max_attempts}) — transient, retrying...")
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            print(f"  [retry] {what} failed with {type(e).__name__} "
                  f"(attempt {attempt}/{max_attempts}) — retrying...")

        if attempt < max_attempts:
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            time.sleep(delay)

    print(f"  [retry] {what} still failing after {max_attempts} attempts — giving up.")
    raise last_exc
