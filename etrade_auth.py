"""
Minimal OAuth 1.0a client for E*TRADE's API.

E*TRADE uses OAuth 1.0a (HMAC-SHA1), not a bearer-token/API-key scheme.
That means every request — including login — has to be individually
signed with your consumer secret and (once you have one) token secret.

Flow implemented here:
  1. get_request_token()   -> temporary token, and a URL for you to visit
  2. (you approve the app in a browser, get a verification code)
  3. get_access_token(...) -> long-lived-for-today access token/secret

Access tokens are valid until midnight US Eastern, or expire after
2 hours of no use (renewable via renew_access_token while unexpired).
"""

import hashlib
import hmac
import random
import time
import urllib.parse
import webbrowser

import requests

LIVE_BASE_URL = "https://api.etrade.com"
REQUEST_TOKEN_URL = f"{LIVE_BASE_URL}/oauth/request_token"
AUTHORIZE_URL = "https://us.etrade.com/e/t/etws/authorize"
ACCESS_TOKEN_URL = f"{LIVE_BASE_URL}/oauth/access_token"
RENEW_ACCESS_TOKEN_URL = f"{LIVE_BASE_URL}/oauth/renew_access_token"


def _percent_encode(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def _build_signature(method, url, params, consumer_secret, token_secret=""):
    """OAuth 1.0a HMAC-SHA1 signature per E*TRADE / RFC 5849."""
    sorted_params = sorted(params.items())
    param_string = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted_params
    )
    base_string = "&".join(
        [method.upper(), _percent_encode(url), _percent_encode(param_string)]
    )
    signing_key = f"{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}"
    hashed = hmac.new(
        signing_key.encode(), base_string.encode(), hashlib.sha1
    )
    import base64

    return base64.b64encode(hashed.digest()).decode()


def _oauth_params(consumer_key):
    return {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": str(random.getrandbits(64)),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }


def get_request_token(consumer_key, consumer_secret):
    """Step 1: get a temporary request token and print the URL to authorize it."""
    params = _oauth_params(consumer_key)
    params["oauth_callback"] = "oob"  # E*TRADE requires out-of-band callback

    signature = _build_signature("GET", REQUEST_TOKEN_URL, params, consumer_secret)
    params["oauth_signature"] = signature

    resp = requests.get(REQUEST_TOKEN_URL, params=params)
    resp.raise_for_status()
    parsed = dict(urllib.parse.parse_qsl(resp.text))

    oauth_token = parsed["oauth_token"]
    oauth_token_secret = parsed["oauth_token_secret"]

    authorize_url = (
        f"{AUTHORIZE_URL}?key={consumer_key}&token={oauth_token}"
    )
    return oauth_token, oauth_token_secret, authorize_url


def get_access_token(consumer_key, consumer_secret, oauth_token, oauth_token_secret, verifier):
    """Step 3: exchange the approved request token + verifier for an access token."""
    params = _oauth_params(consumer_key)
    params["oauth_token"] = oauth_token
    params["oauth_verifier"] = verifier

    signature = _build_signature(
        "GET", ACCESS_TOKEN_URL, params, consumer_secret, oauth_token_secret
    )
    params["oauth_signature"] = signature

    resp = requests.get(ACCESS_TOKEN_URL, params=params)
    resp.raise_for_status()
    parsed = dict(urllib.parse.parse_qsl(resp.text))

    return parsed["oauth_token"], parsed["oauth_token_secret"]


def sign_request(method, url, consumer_key, consumer_secret, oauth_token, oauth_token_secret, extra_params=None):
    """Build the OAuth Authorization header for any authenticated E*TRADE API call."""
    params = _oauth_params(consumer_key)
    params["oauth_token"] = oauth_token

    sig_params = dict(params)
    if extra_params:
        sig_params.update(extra_params)

    signature = _build_signature(method, url, sig_params, consumer_secret, oauth_token_secret)
    params["oauth_signature"] = signature

    header = "OAuth " + ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"' for k, v in params.items()
    )
    return header


def run_interactive_login(consumer_key, consumer_secret):
    """Full step 1+2+3 flow, run once per day. Returns (access_token, access_token_secret)."""
    oauth_token, oauth_token_secret, authorize_url = get_request_token(
        consumer_key, consumer_secret
    )

    print("\n1. Opening browser to authorize this app with E*TRADE...")
    print(f"   If it doesn't open automatically, visit:\n   {authorize_url}\n")
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass

    verifier = input("2. After approving, paste the verification code shown by E*TRADE: ").strip()

    access_token, access_token_secret = get_access_token(
        consumer_key, consumer_secret, oauth_token, oauth_token_secret, verifier
    )
    print("3. Access token acquired.\n")
    return access_token, access_token_secret
