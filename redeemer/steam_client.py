"""Steam client — web session login, owned-library sync, key redemption.

Login is GUI-driven (username/password posted to the API; Steam Guard code on a
second call). Sessions persist to the same .steamcookies file the original
script used.
"""
import re
import threading
import time

import requests
import steam.webauth as wa

from .cookie_store import load_cookie_dict, save_cookie_dict

STEAM_KEYS_PAGE = "https://store.steampowered.com/account/registerkey"
STEAM_LICENSES_PAGE = "https://store.steampowered.com/account/licenses/"
STEAM_REDEEM_API = "https://store.steampowered.com/account/ajaxregisterkey/"
STEAM_OWNED_GAMES_API = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STEAM_USERDATA_API = "https://store.steampowered.com/dynamicstore/userdata/"
# app 753 (Steam) context 1 is the gifts inventory — multi-pack extras,
# guest passes, tradable gift copies
STEAM_GIFT_INVENTORY_API = "https://steamcommunity.com/inventory/{steamid}/753/1"
# context 6 is the Community inventory — trading cards, backgrounds,
# emoticons, booster packs, gems: the sellable/tradable trinkets
STEAM_COMMUNITY_INVENTORY_API = "https://steamcommunity.com/inventory/{steamid}/753/6"
STEAM_MARKET_PRICE_API = "https://steamcommunity.com/market/priceoverview/"
STEAM_MARKET_SELL_API = "https://steamcommunity.com/market/sellitem/"

COOKIE_FILE = ".steamcookies"

RESULT_LABELS = {
    0: "success",
    9: "already_owned",
    13: "region_locked",
    14: "invalid_code",
    15: "duplicate_code_other_account",
    24: "dlc_missing_base_game",
    36: "ps3_link_required",
    50: "wallet_code_use_store",
    53: "rate_limited",
}

RESULT_MESSAGES = {
    0: "Redeemed successfully.",
    9: "This Steam account already owns this product.",
    13: "Not available for purchase in your country (region locked).",
    14: "The product code is not valid.",
    15: "This code was already activated by a different Steam account.",
    24: "Requires ownership of another product first (DLC without base game).",
    36: "Requires PS3 activation first.",
    50: "This is a Steam Wallet code — redeem it on the Steam store instead.",
    53: "Rate limited — too many recent activation attempts.",
}


def code_to_label(code):
    if code is None:
        return "unknown_error"
    return RESULT_LABELS.get(code, f"error_{code}")


def valid_steam_key(key):
    return bool(re.fullmatch(r"[0-9A-Za-z]{5}-[0-9A-Za-z]{5}-[0-9A-Za-z]{5}", key or ""))


class SteamClient:
    def __init__(self):
        # annotated because steam.webauth's login() has no type stubs
        self._session: "requests.Session | None" = None
        self._lock = threading.RLock()
        self._webauth = None  # pending login awaiting a code
        self._poll_stop = None  # Event that halts the approval poller thread

    # ---------------- session ----------------

    def _session_valid(self, session):
        try:
            r = session.get(STEAM_KEYS_PAGE, allow_redirects=False, timeout=30)
            return r.status_code not in (301, 302, 401, 403)
        except requests.RequestException:
            return False

    def is_logged_in(self, recheck=False):
        if self._session is None:
            return False
        if not recheck:
            return True
        with self._lock:
            if self._session is None:
                return False
            ok = self._session_valid(self._session)
            if not ok:
                self._session = None
            return ok

    def try_cookie_login(self):
        with self._lock:
            cookie_dict = load_cookie_dict(COOKIE_FILE)
            if cookie_dict is None:
                return False
            session = requests.Session()
            session.cookies.update(requests.utils.cookiejar_from_dict(cookie_dict))
            if self._session_valid(session):
                self._session = session
                return True
            return False

    def _guard_prompt(self):
        allowed = set(getattr(self._webauth, "allowed_confirmations", []) or [])
        if wa.EAuthSessionGuardType.DeviceConfirmation in allowed:
            msg = "Approve the sign-in in your Steam mobile app — it will be "\
                  "picked up automatically."
            if wa.EAuthSessionGuardType.DeviceCode in allowed:
                msg += " (Or enter your Steam Guard code below as a backup.)"
        elif wa.EAuthSessionGuardType.DeviceCode in allowed:
            msg = "Enter your Steam Guard mobile authenticator code."
        elif wa.EAuthSessionGuardType.EmailCode in allowed:
            msg = "Steam emailed you a Guard code — enter it below."
        else:
            msg = "Confirm the sign-in with Steam Guard, then press Sign in "\
                  "again (leave the code empty)."
        return msg

    # -------- background approval poller (mobile-app confirmations) --------

    def _cancel_poller(self):
        if self._poll_stop is not None:
            self._poll_stop.set()
            self._poll_stop = None

    def _start_approval_poller(self, w):
        """Poll Steam every few seconds so that approving the sign-in in the
        Steam mobile app completes the login without any further clicks.
        Runs for up to 5 minutes; a manual Guard-code submit still works as a
        backup and simply wins the race."""
        stop = threading.Event()
        self._poll_stop = stop
        deadline = time.monotonic() + 300

        def _poll():
            while not stop.wait(5) and time.monotonic() < deadline:
                with self._lock:
                    if self._webauth is not w or self._session is not None:
                        return
                try:
                    w._pollLoginStatus()  # raises until the user approves
                except wa.TwoFactorAuthNotProvided:
                    continue
                except Exception:
                    return  # network/API hiccup — fall back to manual code
                with self._lock:
                    if self._webauth is w and self._session is None:
                        w._finalizeLogin()
                        self._finish_login(w.session)
                return

        threading.Thread(target=_poll, daemon=True).start()

    def _finish_login(self, session):
        self._session = session
        self._webauth = None
        self._cancel_poller()
        try:
            save_cookie_dict(COOKIE_FILE, requests.utils.dict_from_cookiejar(session.cookies))
        except Exception:
            pass
        return {"status": "ok", "message": "Signed in to Steam."}

    def login(self, username=None, password=None, code=None):
        """Returns {status: 'ok'|'needs_code'|'error', message: str}.

        First call: username+password. If Steam Guard is needed, call again
        with just `code` (or an empty code after approving in the app) —
        the pending auth session is continued, mirroring cli_login's flow.
        """
        with self._lock:
            try:
                if username and password:
                    self._cancel_poller()
                    self._webauth = wa.WebAuth(username, password)
                    try:
                        session = self._webauth.login()
                        return self._finish_login(session)
                    except wa.TwoFactorAuthNotProvided:
                        allowed = set(getattr(self._webauth,
                                              "allowed_confirmations", []) or [])
                        if wa.EAuthSessionGuardType.DeviceConfirmation in allowed:
                            self._start_approval_poller(self._webauth)
                        return {"status": "needs_code", "message": self._guard_prompt()}
                    except wa.LoginIncorrect as e:
                        self._webauth = None
                        return {"status": "error", "message": f"Login incorrect: {e}"}

                # Second phase: continue the pending session with a guard code
                # (or poll for an in-app approval when the code is empty).
                if self._webauth is None:
                    if self._session is not None:
                        # the background poller caught the in-app approval first
                        return {"status": "ok", "message": "Signed in to Steam."}
                    return {"status": "error",
                            "message": "Enter username and password first."}
                w = self._webauth
                allowed = set(getattr(w, "allowed_confirmations", []) or [])
                try:
                    if code and code.strip():
                        guard_type = (wa.EAuthSessionGuardType.EmailCode
                                      if wa.EAuthSessionGuardType.EmailCode in allowed
                                      else wa.EAuthSessionGuardType.DeviceCode)
                        w._update_login_token(code.strip(), guard_type)
                    w._pollLoginStatus()
                except wa.TwoFactorAuthNotProvided:
                    return {"status": "needs_code",
                            "message": "Code invalid or approval not seen yet — try again."}
                w._finalizeLogin()
                return self._finish_login(w.session)
            except wa.WebAuthException as e:
                return {"status": "error", "message": f"Steam login failed: {e}"}
            except Exception as e:
                return {"status": "error", "message": f"Steam login failed: {e!r}"}

    # ---------------- library ----------------

    def get_owned_games(self, api_key="", steam_id=""):
        """Fetch {appid: name}. Prefers the Web API (complete, has names);
        falls back to the store session's dynamicstore data (appids only)."""
        if api_key and steam_id:
            params = {
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
                "format": "json",
            }
            r = requests.get(STEAM_OWNED_GAMES_API, params=params, timeout=60)
            r.raise_for_status()
            games = r.json().get("response", {}).get("games", []) or []
            if games:
                return {g["appid"]: g.get("name", "") for g in games if g.get("name")}
            raise RuntimeError(
                "Steam Web API returned no games — check the API key and SteamID64 "
                "in Settings, and that your game details aren't set to private.")
        # Fallback: dynamicstore (needs logged-in session; returns appids w/o names)
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("Set a Steam Web API key + SteamID64 in Settings, "
                               "or sign in to Steam first.")
        r = session.get(STEAM_USERDATA_API, timeout=60)
        r.raise_for_status()
        data = r.json()
        owned = data.get("rgOwnedApps", []) or []
        if not owned:
            raise RuntimeError("Steam returned an empty owned list — session may be stale.")
        return {appid: f"appid {appid}" for appid in owned}

    # ---------------- licenses ----------------

    def get_licenses(self):
        """Scrape the account licenses page. Returns a list of
        {date, name, acquisition} — acquisition 'Retail' marks licenses that
        came from a redeemed product key (e.g. Humble).

        Steam paginates this page (~100 rows) with a continuationToken cursor;
        each page embeds the link to the next, so we walk the chain until it
        runs out."""
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("Not signed in to Steam.")
        licenses = []
        strip_tags = lambda s: re.sub(r"<[^>]+>", " ", s)
        import html as html_mod
        url = STEAM_LICENSES_PAGE
        seen_tokens = set()
        for _ in range(500):  # backstop against a pager loop
            r = session.get(url, timeout=60)
            r.raise_for_status()
            if "login" in r.url:
                raise RuntimeError("Steam session expired — sign in again.")
            html = r.text
            rows = re.findall(
                r'<td\s+class="license_date_col">(.*?)</td>(.*?)'
                r'<td\s+class="license_acquisition_col">(.*?)</td>',
                html, re.S)
            for date, item, acq in rows:
                # free licenses embed a "Remove" link inside the item cell
                item = re.sub(r'<div class="free_license_remove_link">.*?</div>',
                              " ", item, flags=re.S)
                name = html_mod.unescape(strip_tags(item))
                name = re.sub(r"\s+", " ", name).strip()
                acq = html_mod.unescape(strip_tags(acq))
                acq = re.sub(r"\s+", " ", acq).strip()
                licenses.append({
                    "date": html_mod.unescape(date).strip(),
                    "name": name,
                    "acquisition": acq,
                })
            # Next-page cursor. Must anchor on the paginator link — the raw
            # token also appears all over the page in language-switcher URLs
            # pointing back at the CURRENT page. Colon may arrive as %3A,
            # the ampersand as &amp;.
            m = re.search(
                r'license_paginator_next[^>]*href="\?continuationToken='
                r'([0-9]+(?:%3[Aa]|:)[0-9]+)&(?:amp;)?offset=(\d+)"', html)
            if not rows or not m:
                break
            token = re.sub(r"%3[Aa]", ":", m.group(1))
            if token in seen_tokens:
                break
            seen_tokens.add(token)
            url = (f"{STEAM_LICENSES_PAGE}?continuationToken={token}"
                   f"&offset={m.group(2)}")
            time.sleep(0.4)
        if not licenses:
            raise RuntimeError("Could not parse any licenses from the Steam "
                               "licenses page — Steam may have changed its layout.")
        return licenses

    # ---------------- gift inventory ----------------

    def _steamid_from_cookies(self, sess):
        # steamLoginSecure is "<steamid>||<token>"; the requests jar may hold
        # it decoded (||) or URL-encoded (%7C%7C) depending on how it was saved
        for c in sess.cookies:
            if c.name == "steamLoginSecure":
                v = (c.value or "").replace("%7C", "|")
                if "|" in v and v.split("|")[0].isdigit():
                    return v.split("|")[0]
        return ""

    def get_gift_inventory(self, steam_id=""):
        """Gift copies sitting in the Steam community inventory (multi-pack
        extras, guest passes). Public inventories need no session; private
        ones need the signed-in cookies to happen to carry community auth."""
        with self._lock:
            session = self._session
        sess = session or requests.Session()
        sid = (steam_id or "").strip() or self._steamid_from_cookies(sess)
        if not sid:
            raise RuntimeError("Need your SteamID64 — set it in Settings, or sign in to Steam.")
        gifts = []
        start = None
        for _ in range(10):  # 2000 items/page; gifts never get near this
            params = {"l": "english", "count": 2000}
            if start:
                params["start_assetid"] = start
            r = sess.get(STEAM_GIFT_INVENTORY_API.format(steamid=sid),
                         params=params, timeout=30)
            if r.status_code in (401, 403):
                raise RuntimeError(
                    "Steam says this inventory is private. Set inventory privacy "
                    "to Public (Steam profile > Privacy Settings > Inventory), "
                    "then scan again.")
            r.raise_for_status()
            data = r.json() or {}
            descs = {f"{d.get('classid')}_{d.get('instanceid')}": d
                     for d in data.get("descriptions") or []}
            for a in data.get("assets") or []:
                d = descs.get(f"{a.get('classid')}_{a.get('instanceid')}")
                if d is None:
                    continue
                gifts.append({"name": d.get("name", "?"),
                              "type": d.get("type", ""),
                              "assetid": a.get("assetid", "")})
            if data.get("more_items"):
                start = data.get("last_assetid")
            else:
                break
        return gifts

    # ---------------- community inventory (cards & collectibles) ----------------

    def get_community_inventory(self, steam_id=""):
        """Trading cards, backgrounds, emoticons, booster packs, gems (app 753
        context 6). Returns one dict per asset (selling is per-asset):
        {assetid, classid, instanceid, market_hash_name, name, type, game,
         game_appid, marketable, tradable, commodity, icon}."""
        with self._lock:
            session = self._session
        sess = session or requests.Session()
        sid = (steam_id or "").strip() or self._steamid_from_cookies(sess)
        if not sid:
            raise RuntimeError("Need your SteamID64 — set it in Settings, or sign in to Steam.")
        items = []
        start = None
        for _ in range(30):  # 2000/page; large card hoards can span pages
            params = {"l": "english", "count": 2000}
            if start:
                params["start_assetid"] = start
            r = sess.get(STEAM_COMMUNITY_INVENTORY_API.format(steamid=sid),
                         params=params, timeout=30)
            if r.status_code in (401, 403):
                raise RuntimeError(
                    "Steam says this inventory is private. Set inventory privacy "
                    "to Public (Steam profile > Privacy Settings > Inventory), "
                    "then scan again.")
            r.raise_for_status()
            data = r.json() or {}
            descs = {f"{d.get('classid')}_{d.get('instanceid')}": d
                     for d in data.get("descriptions") or []}
            for a in data.get("assets") or []:
                d = descs.get(f"{a.get('classid')}_{a.get('instanceid')}")
                if d is None:
                    continue
                # the owning game's appid is carried on market_fee_app (cards)
                # or parsed from market_hash_name's leading "<appid>-"
                game_appid = d.get("market_fee_app") or ""
                mhn = d.get("market_hash_name", "")
                if not game_appid and "-" in mhn and mhn.split("-", 1)[0].isdigit():
                    game_appid = mhn.split("-", 1)[0]
                icon = d.get("icon_url", "")
                # tags carry the owning game under category 'Game'; other tags
                # are rarity/class/border, so match by category rather than [0]
                game = ""
                for t in d.get("tags", []) or []:
                    if t.get("category") == "Game":
                        game = t.get("localized_tag_name", "")
                        break
                items.append({
                    "assetid": a.get("assetid", ""),
                    "classid": a.get("classid", ""),
                    "instanceid": a.get("instanceid", ""),
                    "market_hash_name": mhn,
                    "name": d.get("name", "?"),
                    "type": d.get("type", ""),
                    "game": game,
                    "game_appid": str(game_appid),
                    "marketable": int(d.get("marketable", 0)),
                    "tradable": int(d.get("tradable", 0)),
                    "commodity": int(d.get("commodity", 0)),
                    "icon": icon,
                })
            if data.get("more_items"):
                start = data.get("last_assetid")
            else:
                break
            time.sleep(0.4)
        return items

    def get_market_price(self, market_hash_name, appid=753, currency=1):
        """priceoverview: {lowest_cents, median_cents, volume} or an error.
        Steam rate-limits this hard — callers must space requests out."""
        with self._lock:
            session = self._session
        sess = session or requests.Session()
        r = sess.get(STEAM_MARKET_PRICE_API, params={
            "appid": appid, "currency": currency,
            "market_hash_name": market_hash_name}, timeout=30)
        if r.status_code == 429:
            return {"rate_limited": True}
        try:
            data = r.json()
        except ValueError:
            return {"error": "no price data"}
        if not data.get("success"):
            return {"error": "no listings"}

        def cents(s):
            # "$0.15" / "0,15€" / "$1,234.56" -> integer cents. Take the last
            # two digit-groups as whole/fraction; a lone group is whole units.
            if not s:
                return None
            groups = re.findall(r"\d+", s)
            if not groups:
                return None
            if len(groups) == 1:
                return int(groups[0]) * 100
            whole = int("".join(groups[:-1]))
            frac = int(groups[-1][:2].ljust(2, "0"))
            return whole * 100 + frac

        return {
            "lowest_cents": cents(data.get("lowest_price")),
            "median_cents": cents(data.get("median_price")),
            "volume": int(re.sub(r"[^\d]", "", data.get("volume", "0")) or 0),
        }

    def create_market_listing(self, assetid, price_cents, amount=1,
                              appid=753, contextid=6):
        """List one asset for sale. price_cents is what YOU receive; Steam adds
        its fee on top for the buyer. Returns (ok, message, needs_confirmation).
        The listing lands pending until a mobile confirmation approves it."""
        with self._lock:
            session = self._session
        if session is None:
            return False, "Not signed in to Steam.", False
        sid = session.cookies.get_dict().get("sessionid")
        if not sid:
            session.get(STEAM_KEYS_PAGE, timeout=30)
            sid = session.cookies.get_dict().get("sessionid")
        my_sid = self._steamid_from_cookies(session)
        r = session.post(STEAM_MARKET_SELL_API, data={
            "sessionid": sid, "appid": appid, "contextid": contextid,
            "assetid": assetid, "amount": amount, "price": int(price_cents),
        }, headers={"Referer": f"https://steamcommunity.com/profiles/"
                    f"{my_sid}/inventory"},
           timeout=30)
        try:
            data = r.json()
        except ValueError:
            return False, f"Steam returned non-JSON (HTTP {r.status_code}).", False
        if data.get("success"):
            return True, "", bool(data.get("requires_confirmation"))
        return False, data.get("message", "Steam refused the listing."), False

    # ---------------- redemption ----------------

    def redeem_key(self, key):
        """Register a product key. Returns (code, message)."""
        with self._lock:
            session = self._session
        if session is None:
            return None, "Not signed in to Steam."
        session_id = session.cookies.get_dict().get("sessionid")
        if not session_id:
            # prime the cookie
            session.get(STEAM_KEYS_PAGE, timeout=30)
            session_id = session.cookies.get_dict().get("sessionid")
        r = session.post(STEAM_REDEEM_API,
                         data={"product_key": key, "sessionid": session_id},
                         timeout=60)
        try:
            blob = r.json()
        except ValueError:
            return None, f"Steam returned a non-JSON response (HTTP {r.status_code})."

        if blob.get("success") == 1:
            items = [i.get("line_item_description", "")
                     for i in blob.get("purchase_receipt_info", {}).get("line_items", [])]
            return 0, "Redeemed: " + ", ".join(filter(None, items))

        code = blob.get("purchase_result_details")
        if code is None:
            receipt = blob.get("purchase_receipt_info") or {}
            code = receipt.get("result_detail")
        code = code if code is not None else 53
        return code, RESULT_MESSAGES.get(code, f"Unexpected Steam error (code {code}).")
