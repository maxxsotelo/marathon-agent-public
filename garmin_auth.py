"""
garmin_auth.py — Headless Garmin Connect login
==============================================
Every script that talks to Garmin must get its client from get_client().
It never prompts for input, so it is safe in cloud sessions with no human
present (Claude Code cloud, GitHub Actions, Colab).

How it works:
  1. The saved token is read from the GARMIN_TOKENS environment variable
     (the contents of ~/.garminconnect/garmin_tokens.json, either as raw JSON
     or base64-encoded) and written to ~/.garminconnect/garmin_tokens.json.
     If GARMIN_TOKENS is unset, an existing token file on disk is used.
  2. The client logs in from that token only. No email/password is passed,
     so garminconnect can never fall back to an SSO login or an MFA prompt.
  3. If the token is missing, malformed, expired or revoked, the script
     exits with a clear message instead of hanging on input().

To create GARMIN_TOKENS, see "Garmin token" in CLAUDE.md.
"""

import base64
import binascii
import json
import os
import sys

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

TOKEN_ENV_VAR = "GARMIN_TOKENS"
TOKEN_STORE = os.path.expanduser("~/.garminconnect")
TOKEN_FILE = os.path.join(TOKEN_STORE, "garmin_tokens.json")

REGENERATE_HINT = (
    "Regenerate the token on a machine where you can enter the Garmin MFA code "
    "(see 'Garmin token' in CLAUDE.md), then update the GARMIN_TOKENS environment "
    "variable in the cloud environment settings."
)


def _fail(message: str) -> None:
    """Exit non-zero with a readable message. SystemExit is not caught by the
    scripts' broad `except Exception` handlers, so the run stops here."""
    raise SystemExit(f"[GARMIN AUTH ERROR] {message}\n{REGENERATE_HINT}")


def _decode_token(raw: str) -> str:
    """Return the token as a JSON string, accepting raw JSON or base64 JSON."""
    raw = raw.strip()
    if not raw.startswith("{") and not raw.startswith("["):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError):
            _fail(f"{TOKEN_ENV_VAR} is neither JSON nor base64-encoded JSON.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _fail(f"{TOKEN_ENV_VAR} does not contain valid JSON.")

    # garminconnect < 0.3 (garth) saved [oauth1, oauth2] tokens; 0.3+ cannot use them.
    if isinstance(data, list) or (isinstance(data, dict) and "oauth_token" in data):
        _fail(
            f"{TOKEN_ENV_VAR} holds an old garth-format token (garminconnect < 0.3), "
            "which garminconnect 0.3+ cannot load."
        )
    if not isinstance(data, dict) or not data.get("di_refresh_token"):
        _fail(
            f"{TOKEN_ENV_VAR} is missing 'di_refresh_token'. It must be the contents "
            "of ~/.garminconnect/garmin_tokens.json."
        )
    return json.dumps(data)


def restore_token() -> None:
    """Write GARMIN_TOKENS into ~/.garminconnect/garmin_tokens.json (mode 0600)."""
    raw = os.getenv(TOKEN_ENV_VAR)
    if not raw or not raw.strip():
        if os.path.isfile(TOKEN_FILE):
            return  # Local machine with a token already on disk.
        _fail(f"{TOKEN_ENV_VAR} is not set and no token file exists at {TOKEN_FILE}.")

    token_json = _decode_token(raw)
    os.makedirs(TOKEN_STORE, mode=0o700, exist_ok=True)
    os.chmod(TOKEN_STORE, 0o700)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token_json)
    os.chmod(TOKEN_FILE, 0o600)


def _no_mfa() -> str:
    _fail("Garmin asked for an MFA code, which cannot be entered in a headless run.")


def get_client() -> Garmin:
    """Restore the saved token and return a logged-in Garmin client."""
    restore_token()
    client = Garmin(prompt_mfa=_no_mfa)
    try:
        client.login(TOKEN_STORE)
    except GarminConnectTooManyRequestsError as e:
        _fail(f"Garmin rate-limited the login (HTTP 429). Wait and retry later. ({e})")
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
        _fail(
            "Garmin rejected the saved token: it is expired, revoked or invalid "
            f"(or Garmin could not be reached). Details: {e}"
        )
    return client
