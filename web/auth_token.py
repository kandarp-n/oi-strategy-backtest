"""Dhan TOTP-based access token generator.

Dhan supports programmatic token generation if TOTP is enabled on the user's
account. We POST to https://auth.dhan.co/app/generateAccessToken with the
client ID + PIN + current TOTP code and receive a fresh 24-hour token.

Setup:
  1. On web.dhan.co -> My Profile -> Access DhanHQ APIs -> Enable TOTP
  2. Scan the QR with Google Authenticator / Authy / 1Password
  3. Note your Dhan client ID and PIN
  4. Use this module (or the web UI) to fetch fresh tokens

Saved token is written to the .env file in the project root and
returned to the caller.
"""
from __future__ import annotations

import os, sys, re
from typing import Optional
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_token(dhan_client_id: str, pin: str, totp: str, timeout: int = 20) -> dict:
    """Return the full Dhan response on success. Raises on failure.

    Response contains: accessToken, dhanClientId, dhanClientName, expiryTime.
    """
    url = "https://auth.dhan.co/app/generateAccessToken"
    params = {
        "dhanClientId": str(dhan_client_id).strip(),
        "pin": str(pin).strip(),
        "totp": str(totp).strip(),
    }
    r = requests.post(url, params=params, timeout=timeout)
    if not r.ok:
        try:
            err = r.json()
        except Exception:
            err = {"errorMessage": r.text}
        raise RuntimeError(f"Token generation failed ({r.status_code}): {err}")
    return r.json()


def update_env_with_token(access_token: str, client_id: str | None = None,
                           env_path: str | None = None) -> str:
    """Update the .env file with a fresh access token. Preserves other keys."""
    env_path = env_path or os.path.join(ROOT, ".env")
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            if client_id:
                f.write(f"DHAN_CLIENT_ID={client_id}\n")
            f.write(f"DHAN_ACCESS_TOKEN={access_token}\n")
        return env_path

    with open(env_path, "r") as f:
        lines = f.readlines()

    out = []
    found_token = False
    found_client = False
    for line in lines:
        if line.strip().startswith("DHAN_ACCESS_TOKEN"):
            out.append(f"DHAN_ACCESS_TOKEN={access_token}\n")
            found_token = True
        elif line.strip().startswith("DHAN_CLIENT_ID") and client_id:
            out.append(f"DHAN_CLIENT_ID={client_id}\n")
            found_client = True
        else:
            out.append(line)
    if not found_token:
        out.append(f"DHAN_ACCESS_TOKEN={access_token}\n")
    if client_id and not found_client:
        out.append(f"DHAN_CLIENT_ID={client_id}\n")

    with open(env_path, "w") as f:
        f.writelines(out)
    return env_path


def decode_token_expiry(jwt_token: str) -> Optional[str]:
    """Parse the JWT to get the expiry time. Returns ISO-string or None."""
    import base64, json
    from datetime import datetime
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp)).isoformat(timespec="seconds")
    except Exception:
        return None
    return None


def get_current_token_info() -> dict:
    """Read the .env and return info about the current token."""
    env_path = os.path.join(ROOT, ".env")
    info = {"client_id": None, "token_present": False, "expiry": None, "expired": True}
    if not os.path.exists(env_path):
        return info
    with open(env_path, "r") as f:
        for line in f:
            if line.strip().startswith("DHAN_CLIENT_ID="):
                info["client_id"] = line.split("=", 1)[1].strip()
            elif line.strip().startswith("DHAN_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token and token != "your_jwt_access_token_here":
                    info["token_present"] = True
                    info["expiry"] = decode_token_expiry(token)
                    if info["expiry"]:
                        from datetime import datetime
                        info["expired"] = datetime.fromisoformat(info["expiry"]) < datetime.now()
    return info


if __name__ == "__main__":
    print("Current token info:", get_current_token_info())
