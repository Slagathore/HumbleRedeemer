"""Humble Bundle client — a headless browser session managed for the web app.

Login is driven by the GUI (email/password posted to the API, then guard/2FA
codes the same way) instead of console prompts. Cookies persist to the same
.humblecookies file the original script uses, so existing sessions carry over.
"""
import json
import pickle
import threading
import time
from base64 import b64encode

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

HUMBLE_LOGIN_PAGE = "https://www.humblebundle.com/login"
HUMBLE_KEYS_PAGE = "https://www.humblebundle.com/home/library"
HUMBLE_LOGIN_API = "https://www.humblebundle.com/processlogin"
HUMBLE_REDEEM_API = "https://www.humblebundle.com/humbler/redeemkey"
HUMBLE_SUB_PAGE = "https://www.humblebundle.com/subscription/"
HUMBLE_CHOOSE_CONTENT = "https://www.humblebundle.com/humbler/choosecontent"

COOKIE_FILE = ".humblecookies"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

GET_ORDERS_JS = '''
var done = arguments[arguments.length - 1];
var onlyKeys = %gamekeys%;
var getHumbleOrderDetails = async () => {
  try {
    var orders;
    if (onlyKeys && onlyKeys.length) {
      orders = onlyKeys.map(k => ({ gamekey: k }));
    } else {
      const response = await fetch('https://www.humblebundle.com/api/v1/user/order');
      orders = await response.json();
    }
    const orderDetailsPromises = orders.map(async (order) => {
      const url = `https://www.humblebundle.com/api/v1/order/${order['gamekey']}?all_tpkds=true`;
      const r = await fetch(url);
      return await r.json();
    });
    return await Promise.all(orderDetailsPromises);
  } catch (error) {
    console.error('Error:', error);
    return [];
  }
};
getHumbleOrderDetails().then(r => {done(r)});
'''

FETCH_POST_JS = '''
var done = arguments[arguments.length - 1];
var formData = new FormData();
const jsonData = JSON.parse(atob('{formData}'));
for (const key in jsonData) {{
    formData.append(key, jsonData[key])
}}
fetch("{url}", {{
  "headers": {{ "csrf-prevention-token": "{csrf}" }},
  "body": formData,
  "method": "POST",
}}).then(r => {{ r.json().then( v => {{done([r.status, v])}} ) }} );
'''

IS_LOGGED_IN_JS = '''
var done = arguments[arguments.length-1];
fetch("https://www.humblebundle.com/home/library").then(r => {done(!r.redirected)})
'''


def find_dict_keys(node, kv, parent=False):
    if isinstance(node, list):
        for i in node:
            yield from find_dict_keys(i, kv, parent)
    elif isinstance(node, dict):
        if kv in node:
            yield node if parent else node[kv]
        for j in node.values():
            yield from find_dict_keys(j, kv, parent)


class HumbleClient:
    def __init__(self):
        self._driver = None
        self._lock = threading.RLock()
        self._logged_in = False
        self._pending_payload = None  # login payload awaiting guard/2FA

    # ---------------- driver ----------------

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        drivers = [
            (webdriver.Chrome, webdriver.ChromeOptions),
            (webdriver.Firefox, webdriver.FirefoxOptions),
        ]
        errors = []
        for d, opt in drivers:
            try:
                options = opt()
                if d == webdriver.Chrome:
                    options.add_argument("--headless=new")
                    options.add_argument(f"user-agent={USER_AGENT}")
                else:
                    options.add_argument("-headless")
                    options.set_preference("general.useragent.override", USER_AGENT)
                self._driver = d(options=options)
                self._driver.set_script_timeout(300)
                return self._driver
            except WebDriverException as e:
                errors.append(str(e).splitlines()[0] if str(e) else repr(e))
        raise RuntimeError(
            "Could not start a headless browser (needs Chrome or Firefox installed). "
            + " | ".join(errors)
        )

    def shutdown(self):
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
                self._logged_in = False

    def _post(self, url, payload):
        driver = self._ensure_driver()
        json_payload = b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        csrf = driver.get_cookie("csrf_cookie")
        csrf = csrf["value"] if csrf else ""
        script = FETCH_POST_JS.format(formData=json_payload, url=url, csrf=csrf)
        return driver.execute_async_script(script)

    # ---------------- session ----------------

    def is_logged_in(self, recheck=False):
        # Non-blocking: if another thread holds the driver (login, sync,
        # reveal), report the last known state instead of stalling the UI.
        if not self._lock.acquire(blocking=False):
            return self._logged_in
        try:
            if self._logged_in and not recheck:
                return True
            if self._driver is None:
                return False
            if recheck:
                try:
                    self._logged_in = bool(
                        self._driver.execute_async_script(IS_LOGGED_IN_JS))
                except Exception:
                    self._logged_in = False
            return self._logged_in
        finally:
            self._lock.release()

    def try_cookie_login(self):
        """Attempt to restore a previous session from .humblecookies."""
        with self._lock:
            try:
                cookies = pickle.load(open(COOKIE_FILE, "rb"))
            except Exception:
                return False
            driver = self._ensure_driver()
            driver.get(HUMBLE_LOGIN_PAGE)
            for cookie in cookies:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
            driver.get(HUMBLE_KEYS_PAGE)
            time.sleep(2)
            ok = ("login" not in driver.current_url
                  and "onboarding" not in driver.current_url)
            self._logged_in = ok
            return ok

    def login(self, email=None, password=None, code=None, guard=None):
        """Drive the Humble login flow. Returns a dict:
        {status: 'ok' | 'needs_guard' | 'needs_2fa' | 'error', message: str}
        Call first with email+password; if needs_guard/needs_2fa, call again
        with guard= or code= (email/password not required on the retry).
        """
        with self._lock:
            driver = self._ensure_driver()
            if email and password:
                driver.get(HUMBLE_LOGIN_PAGE)
                self._pending_payload = {
                    "access_token": "",
                    "access_token_provider_id": "",
                    "goto": "/",
                    "qs": "",
                    "username": email,
                    "password": password,
                }
            if self._pending_payload is None:
                return {"status": "error", "message": "Enter email and password first."}
            payload = self._pending_payload
            if guard:
                payload["guard"] = guard.strip().upper()
            if code:
                payload["code"] = code.strip()

            status, login_json = self._post(HUMBLE_LOGIN_API, payload)
            if status not in (200, 401):
                return {"status": "error",
                        "message": f"Humble responded with HTTP {status}."}

            if isinstance(login_json, dict):
                if "errors" in login_json and "username" in login_json["errors"]:
                    return {"status": "error",
                            "message": login_json["errors"]["username"][0]}
                if login_json.get("user_terms_opt_in_data", {}).get("needs_to_opt_in"):
                    return {"status": "error",
                            "message": "Humble updated its TOS — sign in once in a "
                                       "normal browser to accept it, then retry."}
                if status != 200 and "humble_guard_required" in login_json:
                    return {"status": "needs_guard",
                            "message": "Humble emailed you a security code — enter it below."}
                if status != 200 and "two_factor_required" in login_json:
                    return {"status": "needs_2fa",
                            "message": "Enter your two-factor authentication code."}
                if status != 200 and "errors" in login_json:
                    return {"status": "error", "message": str(login_json["errors"])}

            # success
            self._pending_payload = None
            self._logged_in = True
            try:
                pickle.dump(driver.get_cookies(), open(COOKIE_FILE, "wb"))
            except Exception:
                pass
            return {"status": "ok", "message": "Signed in to Humble."}

    # ---------------- data ----------------

    def fetch_orders(self, gamekeys=None):
        """Fetch orders with full tpk details. With gamekeys, fetch only those
        orders (used to refresh months after claiming Choice games)."""
        script = GET_ORDERS_JS.replace("%gamekeys%", json.dumps(gamekeys or []))
        with self._lock:
            driver = self._ensure_driver()
            return driver.execute_async_script(script)

    def get_requests_session(self):
        """A requests.Session carrying the browser's Humble cookies — used for
        plain page fetches (Choice month pages) without going through JS."""
        import requests
        with self._lock:
            driver = self._ensure_driver()
            cookies = driver.get_cookies()
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        for c in cookies:
            session.cookies.set(c["name"], c["value"],
                                domain=c["domain"].replace("www.", ""),
                                path=c.get("path", "/"))
        return session

    def get_month_data(self, requests_session, choice_url):
        """Fetch a Choice month's page and pull out its content-choice JSON."""
        r = requests_session.get(HUMBLE_SUB_PAGE + choice_url, timeout=60)
        marker = '<script id="webpack-monthly-product-data" type="application/json">'
        if marker not in r.text:
            raise RuntimeError(f"Could not read Choice data for {choice_url} "
                               f"(HTTP {r.status_code})")
        raw = r.text.split(marker)[1].split("</script>")[0].strip()
        return json.loads(raw)["contentChoiceOptions"]

    def get_choice_months(self, order_details, progress=None):
        """Yield every Choice/Monthly order that still has claimable games.

        Each yielded month dict gains: choice_data, available_choices,
        parent_identifier, uses_choices.
        """
        months = [m for m in order_details if "choice_url" in m.get("product", {})]
        months.sort(key=lambda m: m.get("created", ""))
        session = self.get_requests_session()

        for i, month in enumerate(months):
            if progress:
                progress(i + 1, len(months), month["product"].get("human_name", ""))
            if not (month.get("choices_remaining", 0) > 0
                    or month["product"].get("is_subs_v3_product", False)):
                continue
            chosen_games = set(find_dict_keys(month.get("tpkd_dict", {}), "machine_name"))
            month["choice_data"] = self.get_month_data(
                session, month["product"]["choice_url"])
            if not month["choice_data"].get("canRedeemGames", True):
                continue

            v3 = not month["choice_data"].get("usesChoices", True)
            ccd = month["choice_data"]["contentChoiceData"]
            if v3:
                identifier = "initial"
                choice_options = ccd["game_data"]
            else:
                identifier = "initial" if "initial" in ccd else "initial-classic"
                if identifier not in ccd:
                    for key in ccd:
                        if isinstance(ccd[key], dict) and "content_choices" in ccd[key]:
                            identifier = key
                choice_options = ccd[identifier]["content_choices"]

            month["available_choices"] = [
                game for _, game in choice_options.items()
                if set(find_dict_keys(game, "machine_name")).isdisjoint(chosen_games)
            ]
            month["parent_identifier"] = identifier
            month["uses_choices"] = not v3
            if month["available_choices"]:
                yield month

    def choose_content(self, order_gamekey, parent_identifier, display_machine_name):
        """Claim one Choice game. Returns (ok, message)."""
        payload = {
            "gamekey": order_gamekey,
            "parent_identifier": parent_identifier,
            "chosen_identifiers[]": display_machine_name,
            "is_multikey_and_from_choice_modal": "false",
        }
        with self._lock:
            status, res = self._post(HUMBLE_CHOOSE_CONTENT, payload)
        if status == 200 and isinstance(res, dict) and res.get("success"):
            return True, "ok"
        msg = res.get("error_msg", str(res)) if isinstance(res, dict) else f"HTTP {status}"
        return False, str(msg)

    def reveal_key(self, gamekey, machine_name, keyindex, gift=False):
        """Reveal ('redeem' in Humble terms) a key so it exposes its value.
        With gift=True, Humble creates a one-time gift link instead of
        exposing the raw key (the same thing its "Reveal as gift" button does).
        Returns (ok, key_or_link_or_error_message).
        """
        payload = {"keytype": machine_name, "key": gamekey, "keyindex": keyindex}
        if gift:
            payload["gift"] = "true"
        with self._lock:
            status, resp = self._post(HUMBLE_REDEEM_API, payload)
        if status != 200 or not isinstance(resp, dict) or "error_msg" in resp or not resp.get("success"):
            msg = resp.get("error_msg", f"HTTP {status}") if isinstance(resp, dict) else f"HTTP {status}"
            return False, msg
        # Gift reveals return a giftkey token; normal reveals return the key
        giftkey = resp.get("giftkey", "")
        if giftkey:
            return True, f"https://www.humblebundle.com/gift?key={giftkey}"
        key = resp.get("key", "")
        if isinstance(key, dict):
            # Some entries return a nested object (e.g. gift links)
            key = key.get("key", "") or json.dumps(key)
        return True, str(key)
