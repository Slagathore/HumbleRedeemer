"""Steam mobile confirmation support for market listings.

Market sell listings need a mobile confirmation. Two ways to satisfy it:

  - Manual: create the listing, then approve it in the Steam mobile app
    (Confirmations screen). Needs no secret. Always available.

  - Automatic: with your authenticator's identity_secret, the app can list
    and confirm the pending confirmations itself. The secret lives in a
    local .steamguard file — same trust level as .steamcookies, which
    already grants full account access, so this is not a new exposure. It
    can't be hashed (the app has to use it to sign each confirmation) and
    unattended use means the app must read it back, so this is local-file
    protection, not true encryption against someone who has both the file
    and this code.

Getting the secret requires setting up the authenticator via Steam Desktop
Authenticator (SDA) or extracting a maFile; the stock Steam app never
exposes it.
"""
import json
import os
import time

import requests

from steam import guard as _guard

SECRET_FILE = ".steamguard"

MOBILECONF = "https://steamcommunity.com/mobileconf"
# Steam renamed the confirmation tags across app versions; try the modern
# react-flow value first, fall back to the older one.
LIST_TAGS = ("list", "conf")
ACCEPT_TAGS = ("accept", "allow")


def load_secret():
    """Return {identity_secret, steamid, device_id} or None."""
    if not os.path.exists(SECRET_FILE):
        return None
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("identity_secret") and data.get("steamid"):
            data.setdefault("device_id", _guard.generate_device_id(int(data["steamid"])))
            return data
    except Exception:
        return None
    return None


def save_secret(identity_secret, steamid):
    identity_secret = (identity_secret or "").strip()
    steamid = str(steamid or "").strip()
    if not identity_secret or not steamid.isdigit():
        raise ValueError("Need the identity_secret and your numeric SteamID64.")
    data = {"identity_secret": identity_secret, "steamid": steamid,
            "device_id": _guard.generate_device_id(int(steamid))}
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return True


def clear_secret():
    try:
        os.remove(SECRET_FILE)
        return True
    except FileNotFoundError:
        return False


def has_secret():
    return load_secret() is not None


def _params(secret, tag):
    ts = int(time.time())
    key = _guard.generate_confirmation_key(
        secret["identity_secret"], tag, ts)
    # generate_confirmation_key returns bytes (base64 already handled by lib
    # in some versions); normalize to str
    if isinstance(key, bytes):
        import base64
        key = base64.b64encode(key).decode()
    return {"p": secret["device_id"], "a": secret["steamid"],
            "k": key, "t": ts, "m": "react"}


def fetch_confirmations(session):
    """List pending mobile confirmations. Read-only — safe to call anytime,
    which makes it the way to validate a freshly saved secret. Returns a list
    of {id, nonce, type, title, summary}."""
    secret = load_secret()
    if secret is None:
        raise RuntimeError("No identity_secret saved — add it in Settings, or confirm on your phone.")
    last_err = ""
    for tag in LIST_TAGS:
        p = _params(secret, tag)
        p["tag"] = tag
        r = session.get(f"{MOBILECONF}/getlist", params=p, timeout=30)
        try:
            data = r.json()
        except ValueError:
            last_err = "Steam did not return confirmation JSON (session may lack mobile auth)."
            continue
        if data.get("success"):
            out = []
            for c in data.get("conf", []) or []:
                out.append({
                    "id": c.get("id"),
                    "nonce": c.get("nonce"),
                    "type": c.get("type"),
                    "title": (c.get("headline") or c.get("type_name") or "").strip(),
                    "summary": " ".join(c.get("summary", []) or []).strip(),
                    "creator_id": c.get("creator_id"),
                })
            return out
        last_err = data.get("message") or "Steam rejected the confirmation request."
    raise RuntimeError(last_err or "Could not fetch confirmations.")


def accept_confirmation(session, conf_id, nonce):
    """Approve a single pending confirmation."""
    secret = load_secret()
    if secret is None:
        raise RuntimeError("No identity_secret saved.")
    last_err = ""
    for tag in ACCEPT_TAGS:
        p = _params(secret, tag)
        p.update({"tag": tag, "op": "allow", "cid": conf_id, "ck": nonce})
        r = session.get(f"{MOBILECONF}/ajaxop", params=p, timeout=30)
        try:
            data = r.json()
        except ValueError:
            last_err = "Steam did not return JSON for the confirmation op."
            continue
        if data.get("success"):
            return True
        last_err = data.get("message") or "Steam rejected the confirmation."
    raise RuntimeError(last_err or "Could not accept confirmation.")


def accept_all_market_confirmations(session):
    """Approve every pending market-listing confirmation. Returns count.
    Confirmation type 3 is market listings (2 is trade offers) — we only
    touch market ones so this never auto-approves a trade."""
    confs = fetch_confirmations(session)
    done = 0
    for c in confs:
        if str(c.get("type")) in ("3", "MarketListing", "market"):
            if accept_confirmation(session, c["id"], c["nonce"]):
                done += 1
            time.sleep(0.5)
    return done, len(confs)
