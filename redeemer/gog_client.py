"""GOG client — interactive login, library sync, browser-driven redemption.

GOG's redeem API sits behind reCAPTCHA (403 "Invalid or no captcha" without
one), so redemption drives the real gog.com/redeem page in a browser, where
the page's own invisible-captcha JS does the work. Login opens a VISIBLE
browser window on this machine — you sign in there (password never touches
this app) and the session cookies are saved to .gogcookies.
"""
import pickle
import threading
import time

import requests
from selenium import webdriver

GOG_HOME = "https://www.gog.com/en/"
GOG_REDEEM_PAGE = "https://www.gog.com/redeem"
GOG_USERDATA = "https://embed.gog.com/userData.json"
GOG_OWNED_IDS = "https://embed.gog.com/user/data/games"
GOG_PRODUCTS = "https://embed.gog.com/account/getFilteredProducts"

COOKIE_FILE = ".gogcookies"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

# Same-origin on purpose: embed.gog.com's CORS allows the www origin but NOT
# credentials, so a cross-subdomain credentialed fetch always fails silently.
CHECK_LOGIN_JS = """
var done = arguments[arguments.length - 1];
fetch('/userData.json', {credentials: 'same-origin'})
  .then(r => r.json()).then(j => done(!!j.isLoggedIn)).catch(() => done(false));
"""


class GogClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._session = None
        self.login_state = {"status": "idle", "message": ""}

    # ---------------- session ----------------

    def _make_session(self, cookies):
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        for c in cookies:
            try:
                s.cookies.set(c["name"], c["value"],
                              domain=c.get("domain", ".gog.com"),
                              path=c.get("path", "/"))
            except Exception:
                pass
        return s

    def _session_logged_in(self, session):
        try:
            r = session.get(GOG_USERDATA, timeout=30)
            return bool(r.json().get("isLoggedIn"))
        except Exception:
            return False

    def is_logged_in(self):
        with self._lock:
            return self._session is not None

    def try_cookie_login(self):
        with self._lock:
            try:
                cookies = pickle.load(open(COOKIE_FILE, "rb"))
            except Exception:
                return False
            session = self._make_session(cookies)
            if self._session_logged_in(session):
                self._session = session
                return True
            return False

    def start_interactive_login(self):
        """Open a visible browser window for the user to sign in to GOG.
        Runs in a background thread; poll login_state / is_logged_in()."""
        with self._lock:
            if self.login_state["status"] == "waiting":
                return False, "A GOG sign-in window is already open."
            self.login_state = {"status": "waiting",
                                "message": "Browser window opened — sign in to GOG there. "
                                           "(If the login form didn't pop up, click Sign in "
                                           "at the top right of the page.)"}
        threading.Thread(target=self._interactive_login, daemon=True).start()
        return True, "opened"

    def _interactive_login(self):
        driver = None
        try:
            opts = webdriver.ChromeOptions()
            opts.add_argument("--window-size=1100,850")
            opts.add_experimental_option("excludeSwitches", ["enable-logging"])
            driver = webdriver.Chrome(options=opts)
            driver.get(GOG_HOME)
            time.sleep(4)
            # Use the site's own sign-in flow (a modal) rather than guessing
            # auth URLs — GOG rejects unregistered client/redirect combos.
            try:
                driver.find_element(
                    "css selector", "#CybotCookiebotDialogBodyButtonDecline").click()
                time.sleep(1)
            except Exception:
                pass
            try:
                driver.execute_script(
                    """const b = document.querySelector(
                         '[class*="anonymous-header__btn--sign"], a[href*="login"]');
                       if (b) b.click();""")
            except Exception:
                pass  # user can click "Sign in" themselves
            deadline = time.time() + 600  # 10 minutes to sign in
            logged_in = False
            tick = 0
            while time.time() < deadline:
                try:
                    url = driver.current_url
                    # primary: same-origin page check from any www.gog.com page
                    if "www.gog.com" in url:
                        driver.set_script_timeout(20)
                        if driver.execute_async_script(CHECK_LOGIN_JS):
                            logged_in = True
                            break
                    # fallback every ~10s: test the browser's cookies directly
                    # with requests — immune to page/origin/CORS state
                    tick += 1
                    if tick % 4 == 0:
                        cookies = driver.get_cookies()
                        if any(c["name"] in ("gog-al", "gog_us") for c in cookies):
                            if self._session_logged_in(self._make_session(cookies)):
                                logged_in = True
                                break
                except Exception:
                    # window closed or navigating — check if it's gone
                    try:
                        _ = driver.current_url
                    except Exception:
                        break
                time.sleep(2.5)
            if not logged_in:
                self.login_state = {"status": "error",
                                    "message": "GOG sign-in window closed or timed out."}
                return
            cookies = driver.get_cookies()
            session = self._make_session(cookies)
            if not self._session_logged_in(session):
                self.login_state = {"status": "error",
                                    "message": "Signed in, but the session didn't carry over — try again."}
                return
            with self._lock:
                self._session = session
            try:
                pickle.dump(cookies, open(COOKIE_FILE, "wb"))
            except Exception:
                pass
            self.login_state = {"status": "ok", "message": "Signed in to GOG."}
        except Exception as e:
            self.login_state = {"status": "error", "message": f"GOG login failed: {e!r}"}
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    # ---------------- library ----------------

    def get_owned(self):
        """Return {product_id: title} for the GOG library."""
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("Not signed in to GOG.")
        owned = {}
        page, total_pages = 1, 1
        while page <= total_pages and page <= 50:
            r = session.get(GOG_PRODUCTS,
                            params={"mediaType": 1, "page": page}, timeout=60)
            r.raise_for_status()
            data = r.json()
            total_pages = data.get("totalPages", 1)
            for p in data.get("products", []):
                if p.get("id") and p.get("title"):
                    owned[int(p["id"])] = p["title"]
            page += 1
        if not owned:
            # fall back to bare IDs so ownership-by-id still works
            r = session.get(GOG_OWNED_IDS, timeout=60)
            r.raise_for_status()
            for pid in r.json().get("owned", []):
                owned[int(pid)] = f"gog product {pid}"
        return owned

    # ---------------- redemption (browser-driven) ----------------

    def _redeem_driver(self):
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument(f"user-agent={USER_AGENT}")
        opts.add_experimental_option("excludeSwitches", ["enable-logging"])
        driver = webdriver.Chrome(options=opts)
        driver.get(GOG_HOME)
        try:
            cookies = pickle.load(open(COOKIE_FILE, "rb"))
            for c in cookies:
                c.pop("sameSite", None)
                try:
                    driver.add_cookie(c)
                except Exception:
                    pass
        except Exception:
            pass
        return driver

    def _page_text(self, driver):
        try:
            return driver.execute_script(
                "return document.body.innerText").strip()
        except Exception:
            return ""

    def redeem_codes(self, codes, progress=None):
        """Redeem a list of (identifier, code) via the gog.com/redeem page.
        Yields (identifier, status, message) — status in
        ('success', 'already_owned', 'invalid', 'used', 'unknown', 'error').
        """
        driver = self._redeem_driver()
        try:
            # confirm the browser session is actually signed in
            driver.get(GOG_REDEEM_PAGE)
            time.sleep(4)
            driver.set_script_timeout(20)
            if not driver.execute_async_script(CHECK_LOGIN_JS):
                for ident, _ in codes:
                    yield ident, "error", "GOG session not signed in — sign in again."
                return
            self._dismiss_cookiebot(driver)

            for i, (ident, code) in enumerate(codes):
                if progress:
                    progress(i + 1, len(codes))
                yield (ident, *self._redeem_one(driver, code))
                time.sleep(1.5)
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def _dismiss_cookiebot(self, driver):
        try:
            driver.find_element(
                "css selector", "#CybotCookiebotDialogBodyButtonDecline").click()
            time.sleep(1)
        except Exception:
            pass

    def _redeem_one(self, driver, code):
        try:
            driver.get(GOG_REDEEM_PAGE)
            time.sleep(3)
            self._dismiss_cookiebot(driver)
            box = driver.find_element("css selector", "#codeInput")
            box.clear()
            box.send_keys(code)
            driver.find_element(
                "xpath", "//button[contains(@class,'primary')]").click()
            # the page checks the code (invisible captcha) then shows contents
            result = self._await_change(driver, timeout=25)
            verdict = self._classify(result)
            if verdict[0] != "unknown":
                return verdict
            # A contents screen usually needs one more confirm click
            try:
                driver.find_element(
                    "xpath", "//button[contains(@class,'primary')]").click()
                result = self._await_change(driver, timeout=25)
                return self._classify(result, final=True)
            except Exception:
                return "unknown", result[:300]
        except Exception as e:
            return "error", f"{e.__class__.__name__}: {e}"[:300]

    def _await_change(self, driver, timeout=25):
        base = self._page_text(driver)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            text = self._page_text(driver)
            if text != base and "enter your code below" not in text.lower():
                return text
            lowered = text.lower()
            if any(k in lowered for k in ("invalid", "already", "expired",
                                          "added to your account", "success")):
                return text
        return self._page_text(driver)

    def _classify(self, text, final=False):
        t = text.lower()
        if "added to your account" in t or ("success" in t and "redeem" in t):
            return "success", "Added to your GOG account."
        if "already own" in t or "already have" in t:
            return "already_owned", "You already own this on GOG."
        if "already been used" in t or "already redeemed" in t:
            return "used", "Code already used."
        if "invalid" in t or "not valid" in t or "doesn't exist" in t:
            return "invalid", "GOG says the code is invalid."
        if "expired" in t:
            return "used", "Code expired."
        if "captcha" in t:
            return "error", "Blocked by captcha — redeem this one manually at gog.com/redeem."
        if final:
            return "unknown", text[:300]
        return "unknown", text[:300]
