"""Optional API token protection.

The app is meant for a home LAN, so it runs open by default.  Setting
``RIPCD_TOKEN`` (or the token field in the UI) locks everything behind a token
that is sent either as ``X-API-Token``, ``?token=`` or a cookie.
"""

from __future__ import annotations

import hmac
from typing import Any

TOKEN_HEADER = "x-api-token"
TOKEN_COOKIE = "ripcd_token"
TOKEN_QUERY = "token"

LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RipCD - locked</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        background:#0b1220; color:#e6edf7; font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
 form { background:#111c31; border:1px solid #1f2c46; padding:28px 26px; border-radius:14px; width:min(92vw,360px);
        box-shadow:0 20px 60px rgba(0,0,0,.45); }
 h1 { margin:0 0 4px; font-size:20px; }
 p { margin:0 0 18px; color:#93a3bd; font-size:14px; }
 input[type=password] { width:100%; padding:11px 12px; border-radius:9px; border:1px solid #263755;
        background:#0b1220; color:#e6edf7; font-size:15px; box-sizing:border-box; }
 button { margin-top:14px; width:100%; padding:11px; border:0; border-radius:9px; font-weight:600; font-size:15px;
        background:#3b82f6; color:#fff; cursor:pointer; }
 button:hover { background:#2f6fe0; }
 small { display:block; margin-top:14px; color:#6d7f9c; }
</style></head>
<body>
 <form method="get" action="/">
   <h1>RipCD</h1>
   <p>This instance is protected by an API token.</p>
   <input type="password" name="token" placeholder="API token" autofocus autocomplete="current-password">
   <button type="submit">Unlock</button>
   <small>Set with the RIPCD_TOKEN environment variable.</small>
 </form>
</body></html>
"""


def token_matches(expected: str, provided: str | None) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))


def extract_token(headers: Any, query_params: Any = None, cookies: Any = None) -> str:
    """Pull the token out of the request in any of the supported places."""
    try:
        value = headers.get(TOKEN_HEADER)
    except Exception:  # noqa: BLE001 - headers may be a plain dict
        value = None
    if not value and query_params is not None:
        try:
            value = query_params.get(TOKEN_QUERY)
        except Exception:  # noqa: BLE001
            value = None
    if not value and cookies is not None:
        try:
            value = cookies.get(TOKEN_COOKIE)
        except Exception:  # noqa: BLE001
            value = None
    return value or ""


def authorized(settings_token: str, headers: Any, query_params: Any = None, cookies: Any = None) -> bool:
    return token_matches(settings_token, extract_token(headers, query_params, cookies))


def is_login_attempt(path: str, headers: Any, query_params: Any = None) -> bool:
    """True when the request carries a token to log in with."""
    if is_api(path):
        return False
    try:
        provided = query_params.get(TOKEN_QUERY) if query_params is not None else ""
    except Exception:  # noqa: BLE001
        provided = ""
    return bool(provided)


def is_api(path: str) -> bool:
    return path.startswith("/api/")
