"""JSON cookie persistence, replacing pickle.load()/pickle.dump() on the
session cookie files (.gogcookies, .humblecookies, .steamcookies).

pickle.load() deserializes arbitrary Python objects. A cookie file planted
or tampered with by an attacker could run code the moment the app started
and tried to restore a session, with live Humble/Steam/GOG cookies already
in scope. JSON can only decode plain str/int/float/bool/None/list/dict, so
a malformed or hostile file just fails to parse instead of running
anything.

Two cookie shapes are used in this app:
  - a list of Selenium cookie dicts (GOG, Humble) -- already JSON native.
  - a name to value mapping for a requests.Session (Steam) -- the caller
    adapts this to/from a RequestsCookieJar with
    requests.utils.dict_from_cookiejar / cookiejar_from_dict, since a
    RequestsCookieJar itself is not JSON serializable.

Existing installs have pickle files on disk from before this change. Those
are not read with pickle here (that would reintroduce the vulnerability) --
a pre-existing pickle file simply fails the JSON parse, is treated the same
as "no saved session," and the app falls back to a normal interactive
login.
"""
import json


def load_cookie_list(path):
    """Load a list of Selenium cookie dicts. Returns [] if the file is
    missing, unreadable, not valid JSON, or not shaped like a cookie list --
    including old pickle files and malformed or hostile content. Never
    raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    if not all(isinstance(c, dict) and "name" in c and "value" in c for c in data):
        return []
    return data


def save_cookie_list(path, cookies):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f)


def load_cookie_dict(path):
    """Load a {name: value} cookie mapping. Returns None on any failure
    (missing file, unreadable, invalid JSON, wrong shape) -- the caller
    treats None the same as "no saved session." Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        return None
    return data


def save_cookie_dict(path, cookie_dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookie_dict, f)
