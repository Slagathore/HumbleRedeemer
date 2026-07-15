"""Background job runner. One worker thread; the UI polls its status."""
import threading
import time
import traceback
from collections import deque

from . import matching
from .steam_client import code_to_label, valid_steam_key


class JobRunner:
    def __init__(self, store, humble, steam, gog=None):
        self.store = store
        self.humble = humble
        self.steam = steam
        self.gog = gog
        self._lock = threading.RLock()
        self._thread = None
        self._cancel = threading.Event()
        self.status = {
            "job": None,
            "phase": "",
            "message": "",
            "current": 0,
            "total": 0,
            "rate_limit_until": 0,
            "running": False,
            "finished_at": "",
            "last_error": "",
        }
        self.log = deque(maxlen=400)

    # ---------------- public API ----------------

    def start(self, job_type, params=None):
        with self._lock:
            if self.status["running"]:
                return False, "A job is already running."
            self._cancel.clear()
            self.status.update({
                "job": job_type, "phase": "starting", "message": "",
                "current": 0, "total": 0, "rate_limit_until": 0,
                "running": True, "finished_at": "", "last_error": "",
            })
            self._thread = threading.Thread(
                target=self._run, args=(job_type, params or {}), daemon=True)
            self._thread.start()
            return True, "started"

    def cancel(self):
        self._cancel.set()

    def stop(self, timeout=5):
        """Cancel and wait briefly for the worker — used on app shutdown so
        we don't yank the process out from under a half-written key result."""
        self._cancel.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def snapshot(self):
        with self._lock:
            snap = dict(self.status)
            snap["log"] = list(self.log)[-100:]
            return snap

    # ---------------- internals ----------------

    def _say(self, msg, phase=None):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"[{ts}] {msg}")
            self.status["message"] = msg
            if phase:
                self.status["phase"] = phase

    def _progress(self, current, total):
        with self._lock:
            self.status["current"] = current
            self.status["total"] = total

    def _run(self, job_type, params):
        try:
            handler = {
                "sync_humble": self._job_sync_humble,
                "sync_steam": self._job_sync_steam,
                "match": self._job_match,
                "reveal": self._job_reveal,
                "redeem": self._job_redeem,
                "claim_choices": self._job_claim_choices,
                "verify_licenses": self._job_verify_licenses,
                "sync_gog": self._job_sync_gog,
                "redeem_gog": self._job_redeem_gog,
                "verify_spares": self._job_verify_spares,
                "scan_inventory": self._job_scan_inventory,
                "price_inventory": self._job_price_inventory,
                "list_market": self._job_list_market,
                "full_auto": self._job_full_auto,
            }[job_type]
            handler(params)
            if self._cancel.is_set():
                self._say("Job cancelled.")
        except Exception as e:
            traceback.print_exc()
            with self._lock:
                self.status["last_error"] = str(e)
            self._say(f"ERROR: {e}")
        finally:
            with self._lock:
                self.status["running"] = False
                self.status["phase"] = "idle"
                self.status["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------- jobs ----------------

    def _require_humble(self):
        if self.humble.is_logged_in():
            return True
        self._say("Trying saved Humble session...")
        if self.humble.try_cookie_login():
            return True
        raise RuntimeError("Not signed in to Humble — use the Sign in button first.")

    def _require_steam(self):
        if self.steam.is_logged_in(recheck=True):
            return True
        self._say("Trying saved Steam session...")
        if self.steam.try_cookie_login():
            return True
        raise RuntimeError("Not signed in to Steam — use the Sign in button first.")

    def _job_sync_humble(self, params):
        self._require_humble()
        self._say("Fetching your entire Humble library (this can take a minute)...",
                  phase="sync_humble")
        orders = self.humble.fetch_orders()
        if not orders:
            raise RuntimeError("Humble returned no orders — try signing in again.")
        tpks = list(matching_find_tpks(orders))
        self._say(f"Found {len(orders)} orders containing {len(tpks)} key entries. Saving...")
        new = 0
        for i, tpk in enumerate(tpks):
            if self.store.upsert_humble_key(tpk):
                new += 1
            if i % 100 == 0:
                self._progress(i + 1, len(tpks))
        self._progress(len(tpks), len(tpks))
        purged = self.store.purge_bootstrap_rows()
        if purged:
            self._say(f"Cleaned up {purged} placeholder rows from the CSV bootstrap.")
        applied = self.store.apply_legacy_statuses()
        self.store.log_event("sync_humble",
                             detail=f"{len(tpks)} entries, {new} new, legacy applied to {applied}")
        self._say(f"Humble sync complete: {len(tpks)} entries ({new} new). "
                  f"Applied historical status to {applied} entries.")

    def _job_sync_steam(self, params):
        settings = self.store.get_settings()
        api_key = settings.get("steam_api_key", "").strip()
        steam_id = settings.get("steam_id_64", "").strip()
        self._say("Fetching owned Steam games...", phase="sync_steam")
        if not (api_key and steam_id):
            self._require_steam()
        owned = self.steam.get_owned_games(api_key, steam_id)
        self.store.replace_steam_library(owned)
        self.store.log_event("sync_steam", detail=f"{len(owned)} owned apps")
        self._say(f"Steam library synced: {len(owned)} owned apps.")

    def _job_match(self, params):
        if self.store.steam_library_count() == 0:
            self._job_sync_steam({})
        settings = self.store.get_settings()
        threshold = int(float(settings.get("fuzzy_threshold", "90")))
        self._say("Matching Humble entries against your Steam library...", phase="match")
        counts = matching.match_all(
            self.store, threshold,
            progress=lambda c, t, name: self._progress(c, t))
        self.store.log_event("match", detail=str(counts))
        pretty = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        self._say(f"Matching complete — {pretty}")

    def _candidate_keys(self, include_likely=False):
        """Steam keys with no terminal status that we don't think you own."""
        statuses = "('unowned','unmatched','not_owned_manual')"
        if include_likely:
            statuses = "('unowned','unmatched','not_owned_manual','likely_owned')"
        return self.store.get_keys(
            "service='Steam' AND is_expired=0 AND is_gift=0 AND redeem_status='' "
            f"AND match_status IN {statuses}")

    def _job_reveal(self, params):
        self._require_humble()
        ids = params.get("ids")
        if ids:
            keys = [k for k in (self.store.get_key(i) for i in ids) if k]
            keys = [k for k in keys if not k["revealed"]]
        elif params.get("scope") == "all":
            # every unrevealed, unexpired key of any service — the user wants
            # the raw values (this forgoes Humble gift links for these keys)
            keys = self.store.get_keys("revealed=0 AND is_expired=0")
        else:
            keys = [k for k in self._candidate_keys() if not k["revealed"]]
        skipped = [k for k in keys if not can_reveal(k)]
        keys = [k for k in keys if can_reveal(k)]
        if skipped:
            self._say(f"{len(skipped)} entries came from the CSV bootstrap and need a "
                      "Humble sync before they can be revealed — run Sync Humble.")
        self._say(f"Revealing {len(keys)} keys on Humble...", phase="reveal")
        done = 0
        for i, key in enumerate(keys):
            if self._cancel.is_set():
                break
            self._progress(i + 1, len(keys))
            ok, value = self.humble.reveal_key(
                key["gamekey"], key["machine_name"], key["keyindex"])
            if ok and value:
                self.store.set_key_fields(key["id"], redeemed_key_val=value, revealed=1)
                self.store.log_event("reveal", key["gamekey"], key["machine_name"],
                                     key["human_name"], result_label="success")
                done += 1
                self._say(f"Revealed: {key['human_name']}")
            else:
                self.store.log_event("reveal", key["gamekey"], key["machine_name"],
                                     key["human_name"], detail=value, result_label="error")
                self._say(f"Could not reveal {key['human_name']}: {value}")
            time.sleep(0.75)
        self._say(f"Reveal complete: {done}/{len(keys)} keys revealed.")

    def _job_redeem(self, params):
        settings = self.store.get_settings()
        auto_reveal = settings.get("auto_reveal", "1") == "1"
        include_likely = (params.get("include_likely")
                          or settings.get("redeem_likely_owned", "0") == "1")
        delay = float(settings.get("per_key_delay", "1.5"))
        wait_min = float(settings.get("rate_limit_wait_min", "30"))

        self._require_steam()

        ids = params.get("ids")
        if ids:
            keys = [k for k in (self.store.get_key(i) for i in ids) if k]
            keys = [k for k in keys if not k["redeem_status"]]
        else:
            keys = self._candidate_keys(include_likely)

        needs_reveal = [k for k in keys if not k["revealed"]]
        if needs_reveal and auto_reveal:
            self._require_humble()
        elif needs_reveal:
            self._say(f"Skipping {len(needs_reveal)} unrevealed keys (auto-reveal is off).")
            keys = [k for k in keys if k["revealed"]]

        self._say(f"Redeeming {len(keys)} keys on Steam...", phase="redeem")
        stats = {"success": 0, "already_owned": 0, "gift_link": 0, "errored": 0}
        seen_this_run = set()

        for i, key in enumerate(keys):
            if self._cancel.is_set():
                break
            self._progress(i + 1, len(keys))

            # Duplicate guard: same game twice in one run burns the failure limit
            ident = key["steam_app_id"] or matching.normalize(key["human_name"])
            if ident in seen_this_run:
                self._set_result(key, 9, "duplicate in this run — treated as owned")
                stats["already_owned"] += 1
                continue
            seen_this_run.add(ident)

            key_val = key["redeemed_key_val"]
            if not key_val and auto_reveal and not can_reveal(key):
                self._say(f"{key['human_name']}: from CSV bootstrap — run Sync Humble "
                          "before this key can be revealed. Skipped.")
                continue
            if not key_val and auto_reveal:
                ok, value = self.humble.reveal_key(
                    key["gamekey"], key["machine_name"], key["keyindex"])
                if ok and value:
                    key_val = value
                    self.store.set_key_fields(key["id"], redeemed_key_val=value, revealed=1)
                    self.store.log_event("reveal", key["gamekey"], key["machine_name"],
                                         key["human_name"], result_label="success")
                else:
                    self.store.log_event("reveal", key["gamekey"], key["machine_name"],
                                         key["human_name"], detail=value, result_label="error")
                    self.store.set_key_fields(key["id"], redeem_status="errored",
                                              last_result_label="reveal_failed",
                                              last_attempt_at=now_str())
                    stats["errored"] += 1
                    self._say(f"Could not reveal {key['human_name']}: {value}")
                    continue

            if not valid_steam_key(key_val):
                # Usually a Humble gift link rather than a raw Steam key
                self.store.set_key_fields(key["id"], redeem_status="gift_link",
                                          last_result_label="not_a_steam_key",
                                          last_attempt_at=now_str())
                self.store.log_event("redeem_steam", key["gamekey"], key["machine_name"],
                                     key["human_name"], detail=str(key_val)[:80],
                                     result_label="gift_link")
                stats["gift_link"] += 1
                self._say(f"{key['human_name']}: not a raw Steam key (likely a gift link) — skipped.")
                continue

            self._say(f"Redeeming {key['human_name']}...")
            code, msg = self.steam.redeem_key(key_val)

            # code None = session died / Steam replied garbage, NOT a verdict
            # on the key. Reconnect once; if that fails, stop the run without
            # touching this or later keys (they'd all be falsely 'errored').
            if code is None:
                self._say(f"Steam session hiccup ({msg}) — reconnecting...")
                if self.steam.try_cookie_login():
                    code, msg = self.steam.redeem_key(key_val)
            if code is None:
                self.store.log_event("redeem_steam", key["gamekey"],
                                     key["machine_name"], key["human_name"],
                                     detail=msg, result_label="session_lost")
                self._say(f"Steam session is gone ({msg}) — stopping. "
                          f"{key['human_name']} and the keys after it were "
                          "NOT marked; sign in to Steam and run Redeem again.")
                break

            while code == 53 and not self._cancel.is_set():
                until = time.time() + wait_min * 60
                with self._lock:
                    self.status["rate_limit_until"] = until
                self._say(f"Steam rate limit hit — waiting {wait_min:.0f} min "
                          f"before retrying {key['human_name']}...", phase="rate_limited")
                while time.time() < until and not self._cancel.is_set():
                    time.sleep(2)
                with self._lock:
                    self.status["rate_limit_until"] = 0
                    self.status["phase"] = "redeem"
                if self._cancel.is_set():
                    break
                code, msg = self.steam.redeem_key(key_val)

            if code == 53:
                break  # cancelled mid rate-limit wait

            self._set_result(key, code, msg)
            label = code_to_label(code)
            if code == 0:
                stats["success"] += 1
            elif code in (9, 15):
                stats["already_owned"] += 1
            else:
                stats["errored"] += 1
            self._say(f"{key['human_name']}: {label}")
            time.sleep(delay)

        self.store.log_event("redeem_run", detail=str(stats))
        self._say("Redemption run finished — "
                  + ", ".join(f"{k}: {v}" for k, v in stats.items()))

    def _job_claim_choices(self, params):
        """Claim every unclaimed game in every Humble Choice/Monthly month."""
        self._require_humble()
        self._say("Fetching your orders to find Choice months...", phase="claim_choices")
        orders = self.humble.fetch_orders()
        if not orders:
            raise RuntimeError("Humble returned no orders — try signing in again.")

        claimed = 0
        manual = []
        touched_orders = []
        months_with_games = 0

        months = self.humble.get_choice_months(
            orders, progress=lambda c, t, name: self._progress(c, t))
        for month in months:
            if self._cancel.is_set():
                break
            month_name = month["product"].get("human_name", month["product"]["choice_url"])
            choices = month["available_choices"]
            remaining = (month.get("choices_remaining", 0)
                         if month.get("uses_choices") else len(choices))
            months_with_games += 1
            self._say(f"{month_name}: {len(choices)} unclaimed games "
                      f"({remaining} choices available)")

            for choice in choices:
                if self._cancel.is_set():
                    break
                title = choice.get("title", choice.get("display_item_machine_name", "?"))
                if month.get("uses_choices") and remaining <= 0:
                    self._say(f"{month_name}: out of choices — skipping the rest.")
                    break
                if "tpkds" not in choice:
                    manual.append((month_name, title))
                    self.store.log_event("choice_claim", month.get("gamekey", ""),
                                         choice.get("display_item_machine_name", ""),
                                         title, detail=f"{month_name}: must be claimed on "
                                         "the Humble website directly",
                                         result_label="manual_needed")
                    continue
                ok, msg = self.humble.choose_content(
                    choice["tpkds"][0]["gamekey"],
                    month["parent_identifier"],
                    choice["display_item_machine_name"])
                if ok:
                    claimed += 1
                    remaining -= 1
                    self._say(f"Claimed: {title} ({month_name})")
                    self.store.log_event("choice_claim", month.get("gamekey", ""),
                                         choice.get("display_item_machine_name", ""),
                                         title, detail=month_name, result_label="success")
                    if month.get("gamekey") and month["gamekey"] not in touched_orders:
                        touched_orders.append(month["gamekey"])
                else:
                    self._say(f"Could not claim {title}: {msg}")
                    self.store.log_event("choice_claim", month.get("gamekey", ""),
                                         choice.get("display_item_machine_name", ""),
                                         title, detail=f"{month_name}: {msg}",
                                         result_label="error")
                time.sleep(0.5)

        if touched_orders and not self._cancel.is_set():
            self._say("Refreshing the claimed months to pick up their new keys...")
            updated = self.humble.fetch_orders(gamekeys=touched_orders)
            new = sum(self.store.upsert_humble_key(tpk)
                      for tpk in matching_find_tpks(updated))
            self._say(f"Saved {new} new key entries from claimed games.")

        summary = (f"Choice claiming done: {months_with_games} months had unclaimed games, "
                   f"{claimed} games claimed.")
        if manual:
            summary += (f" {len(manual)} need claiming on the Humble site directly: "
                        + "; ".join(f"{t} ({m})" for m, t in manual[:10]))
        self.store.log_event("claim_choices_run",
                             detail=f"claimed={claimed}, manual={len(manual)}")
        self._say(summary)

    def _job_verify_licenses(self, params):
        """Cross-reference Steam's account licenses against redeemed keys."""
        self._require_steam()
        self._say("Fetching your Steam account licenses...", phase="verify_licenses")
        licenses = self.steam.get_licenses()
        self.store.replace_steam_licenses(licenses)
        # Product-key activations show as "Retail" on the licenses page
        # (older layouts said "Activated as CD Key")
        cd_keys = [l for l in licenses
                   if l["acquisition"].lower() == "retail"
                   or "cd key" in l["acquisition"].lower()]
        self._say(f"Found {len(licenses)} licenses, {len(cd_keys)} activated via "
                  "product key (Retail). Matching against redeemed Humble keys...")

        by_norm = {}
        for lic in cd_keys:
            by_norm.setdefault(matching.normalize(lic["name"]), lic)

        keys = self.store.get_keys("redeem_status='redeemed'")
        verified = 0
        unverified = []
        for i, key in enumerate(keys):
            self._progress(i + 1, len(keys))
            lic = by_norm.get(matching.normalize(key["human_name"]))
            if lic is None:
                # fuzzy fallback — license names often carry edition suffixes
                loose = matching.normalize_loose(key["human_name"])
                best = (0, None)
                for lic_cand in cd_keys:
                    score = matching.fuzz.token_set_ratio(
                        loose, matching.normalize_loose(lic_cand["name"]))
                    if score > best[0]:
                        best = (score, lic_cand)
                if best[0] >= 93:
                    lic = best[1]
            if lic is not None:
                verified += 1
                self.store.set_key_fields(
                    key["id"],
                    steam_license=f"{lic['name']} — {lic['acquisition']} ({lic['date']})")
            else:
                unverified.append(key["human_name"])
                if key["steam_license"]:
                    self.store.set_key_fields(key["id"], steam_license="")

        self.store.log_event("verify_licenses",
                             detail=f"licenses={len(licenses)}, cd_keys={len(cd_keys)}, "
                                    f"verified={verified}/{len(keys)}")
        summary = f"License check: {verified} of {len(keys)} redeemed keys verified in Steam."
        if unverified:
            summary += (f" {len(unverified)} redeemed keys have no matching CD-key license "
                        f"(first few: {', '.join(unverified[:8])}) — these may have been "
                        "redeemed to a different account, be DLC bundled under another "
                        "license name, or just be named differently.")
        self._say(summary)
        self._annotate_spare_provenance(licenses)

    def _annotate_spare_provenance(self, licenses):
        """Stamp each giveaway spare with how its game entered the account.

        A game owned via purchase/gift/free grant can't have consumed the
        spare key. A Retail (product key) license that no tracked redemption
        accounts for is the dangerous case: the "spare" itself may be the key
        that created the ownership (e.g. an untracked pre-app run)."""
        self._say("Tracing giveaway spares against license history...",
                  phase="verify_licenses")

        all_by_norm = {}
        for lic in licenses:
            all_by_norm.setdefault(matching.normalize(lic["name"]), []).append(lic)
        loose_all = [(matching.normalize_loose(l["name"]), l) for l in licenses]

        red_norm, red_loose = set(), []
        for k in self.store.get_keys("redeem_status='redeemed'"):
            for n in (k["human_name"], k["match_name"]):
                if n:
                    red_norm.add(matching.normalize(n))
                    red_loose.append(matching.normalize_loose(n))

        def is_retail(lic):
            a = lic["acquisition"].lower()
            return a == "retail" or "cd key" in a

        def retail_explained(lic):
            if matching.normalize(lic["name"]) in red_norm:
                return True
            ll = matching.normalize_loose(lic["name"])
            return any(matching.fuzz.token_set_ratio(ll, rl) >= 93
                       for rl in red_loose)

        spares = self.store.get_giveaway_keys(include_given=True)
        counts = {}
        for i, key in enumerate(spares):
            self._progress(i + 1, len(spares))
            if key["gamekey"] == "manual":
                continue  # hand-entered keys have no Humble/license story to trace
            names = [key["human_name"]]
            if key["match_name"]:
                names.append(key["match_name"])
            hits = []
            for n in names:
                hits.extend(all_by_norm.get(matching.normalize(n), []))
            if not hits:
                best = (0, None)
                for n in names:
                    ln = matching.normalize_loose(n)
                    for lnorm, lic in loose_all:
                        score = matching.fuzz.token_set_ratio(ln, lnorm)
                        if score > best[0]:
                            best = (score, lic)
                if best[0] >= 93:
                    hits = [best[1]]
            if not hits:
                prov, shown = "unknown", None
            else:
                retail = [l for l in hits if is_retail(l)]
                shown = (retail or hits)[0]
                if not retail:
                    prov = "not_from_key"
                elif all(retail_explained(l) for l in retail):
                    prov = "retail_explained"
                else:
                    prov = "retail_suspect"
            counts[prov] = counts.get(prov, 0) + 1
            self.store.set_key_fields(
                key["id"], license_provenance=prov,
                steam_license=(f"{shown['name']} — {shown['acquisition']} "
                               f"({shown['date']})" if shown else ""))
        self.store.log_event("spare_provenance", detail=str(counts))
        self._say("Spare ownership traced — "
                  + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    def _require_gog(self):
        if self.gog is None:
            raise RuntimeError("GOG support not initialized.")
        if self.gog.is_logged_in():
            return True
        self._say("Trying saved GOG session...")
        if self.gog.try_cookie_login():
            return True
        raise RuntimeError("Not signed in to GOG — click the GOG chip to sign in.")

    def _job_sync_gog(self, params):
        self._require_gog()
        self._say("Fetching your GOG library...", phase="sync_gog")
        owned = self.gog.get_owned()
        self.store.replace_gog_library(owned)
        self.store.log_event("sync_gog", detail=f"{len(owned)} owned products")
        self._say(f"GOG library synced: {len(owned)} owned products.")

    def _job_redeem_gog(self, params):
        """Redeem revealed GOG keys via the gog.com/redeem page, skipping
        games already in the GOG library."""
        self._require_gog()
        try:
            self._job_sync_gog({})
        except Exception as e:
            self._say(f"GOG library sync failed ({e}) — continuing without ownership check.")
        owned_norm = {matching.normalize(n) for n in self.store.get_gog_library().values()}

        keys = self.store.get_keys(
            "service='GOG' AND is_expired=0 AND redeem_status='' AND revealed=1 "
            "AND redeemed_key_val!=''")
        unrevealed = self.store.get_keys(
            "service='GOG' AND is_expired=0 AND redeem_status='' AND revealed=0")
        if unrevealed:
            self._say(f"{len(unrevealed)} GOG keys are still unrevealed — reveal them first.")
        todo = []
        for key in keys:
            if matching.normalize(key["human_name"]) in owned_norm:
                self.store.set_key_fields(key["id"], redeem_status="already_owned",
                                          last_result_label="owned_on_gog",
                                          last_attempt_at=now_str())
                self.store.log_event("redeem_gog", key["gamekey"], key["machine_name"],
                                     key["human_name"], detail="already in GOG library",
                                     result_label="already_owned")
                self._say(f"{key['human_name']}: already in your GOG library — kept as a spare.")
            else:
                todo.append(key)

        if not todo:
            self._say("No GOG keys left to redeem.")
            return
        self._say(f"Redeeming {len(todo)} GOG keys via gog.com/redeem "
                  "(a headless browser drives the page)...", phase="redeem_gog")
        by_id = {k["id"]: k for k in todo}
        codes = [(k["id"], k["redeemed_key_val"]) for k in todo]
        stats = {}
        for ident, status, msg in self.gog.redeem_codes(
                codes, progress=lambda c, t: self._progress(c, t)):
            if self._cancel.is_set():
                break
            key = by_id[ident]
            stats[status] = stats.get(status, 0) + 1
            mapped = {"success": "redeemed", "already_owned": "already_owned",
                      "used": "errored", "invalid": "errored",
                      "error": "", "unknown": ""}[status]
            if mapped:
                self.store.set_key_fields(key["id"], redeem_status=mapped,
                                          last_result_label=f"gog_{status}",
                                          last_attempt_at=now_str())
            self.store.log_event("redeem_gog", key["gamekey"], key["machine_name"],
                                 key["human_name"], detail=msg, result_label=status)
            self._say(f"{key['human_name']}: {status} — {msg}")
        self.store.log_event("redeem_gog_run", detail=str(stats))
        self._say("GOG redemption finished — "
                  + ", ".join(f"{k}: {v}" for k, v in stats.items()))

    def _job_verify_spares(self, params):
        """Resolve legacy 'verify first' spares by re-attempting them on Steam.

        Old runs logged Steam codes 9 (you own it — key still valid) and 15
        (key consumed by another account) into the same CSV without recording
        which. Re-attempting is safe: 9 refuses again without consuming the
        key, 15 is already dead. Rarely a key actually redeems (code 0) — that
        means the old record was wrong and you now own the game.
        """
        self._require_steam()
        settings = self.store.get_settings()
        wait_min = float(settings.get("rate_limit_wait_min", "30"))

        keys = [k for k in self.store.get_keys(
            "redeem_status='already_owned' AND last_result_code IS NULL "
            "AND given_away=0 AND is_expired=0")
            if valid_steam_key(k["redeemed_key_val"])]
        self._say(f"Verifying {len(keys)} ambiguous spare keys against Steam. "
                  "Failed attempts are heavily rate-limited (~10/hour), so this "
                  "runs long — cancel any time, progress is saved per key.",
                  phase="verify_spares")
        stats = {"spare_confirmed": 0, "dead": 0, "redeemed": 0, "other": 0}

        for i, key in enumerate(keys):
            if self._cancel.is_set():
                break
            self._progress(i + 1, len(keys))
            code, msg = self.steam.redeem_key(key["redeemed_key_val"])

            # session death is not a key verdict — don't reclassify the spare
            if code is None:
                self._say(f"Steam session hiccup ({msg}) — reconnecting...")
                if self.steam.try_cookie_login():
                    code, msg = self.steam.redeem_key(key["redeemed_key_val"])
            if code is None:
                self._say(f"Steam session is gone ({msg}) — stopping; "
                          f"{key['human_name']} was left untouched.")
                break

            while code == 53 and not self._cancel.is_set():
                until = time.time() + wait_min * 60
                with self._lock:
                    self.status["rate_limit_until"] = until
                self._say(f"Rate limited — waiting {wait_min:.0f} min "
                          f"({stats['spare_confirmed']} spares confirmed, "
                          f"{stats['dead']} dead so far)...", phase="rate_limited")
                while time.time() < until and not self._cancel.is_set():
                    time.sleep(2)
                with self._lock:
                    self.status["rate_limit_until"] = 0
                    self.status["phase"] = "verify_spares"
                if not self._cancel.is_set():
                    code, msg = self.steam.redeem_key(key["redeemed_key_val"])
            if code == 53:
                break  # cancelled during the wait

            if code == 9:
                stats["spare_confirmed"] += 1
                self.store.set_key_fields(key["id"], last_result_code=9,
                                          last_result_label="already_owned",
                                          last_attempt_at=now_str())
                self._say(f"{key['human_name']}: confirmed spare (you own it, key unused).")
            elif code == 15:
                stats["dead"] += 1
                self.store.set_key_fields(key["id"], last_result_code=15,
                                          last_result_label="duplicate_code_other_account",
                                          last_attempt_at=now_str())
                self._say(f"{key['human_name']}: key was used by another account — removed from giveaway.")
            elif code == 0:
                stats["redeemed"] += 1
                self._set_result(key, code, msg)
                self._say(f"{key['human_name']}: old record was wrong — key was still "
                          "valid and just redeemed to your account.")
            else:
                # invalid / region-locked / anything unexpected: not a giftable
                # spare — reclassify as errored so it shows decoded on the
                # Attention page instead of lurking in the giveaway list
                stats["other"] += 1
                self.store.set_key_fields(key["id"], redeem_status="errored",
                                          last_result_code=code,
                                          last_result_label=code_to_label(code),
                                          last_attempt_at=now_str())
                self._say(f"{key['human_name']}: {code_to_label(code)} — {msg}")
            self.store.log_event("verify_spare", key["gamekey"], key["machine_name"],
                                 key["human_name"], detail=msg,
                                 result_code=code, result_label=code_to_label(code))
            time.sleep(2)

        self.store.log_event("verify_spares_run", detail=str(stats))
        self._say("Spare verification "
                  + ("cancelled" if self._cancel.is_set() else "finished")
                  + f" — {stats['spare_confirmed']} confirmed spares, {stats['dead']} dead, "
                    f"{stats['redeemed']} unexpectedly redeemed, {stats['other']} other.")

    # ---------------- steam inventory & market ----------------

    def _job_scan_inventory(self, params):
        self._require_steam()
        settings = self.store.get_settings()
        sid = settings.get("steam_id_64", "").strip()
        self._say("Scanning your Steam community inventory (cards, backgrounds, "
                  "emoticons, boosters, gems)...", phase="scan_inventory")
        items = self.steam.get_community_inventory(sid)
        n = self.store.replace_steam_inventory(items)
        marketable = sum(1 for it in items if it["marketable"])
        self.store.log_event("scan_inventory",
                             detail=f"{n} items, {marketable} marketable")
        self._say(f"Inventory scanned: {n} items ({marketable} marketable). "
                  "Run 'Refresh prices' to fetch market values.")

    def _job_price_inventory(self, params):
        """Sweep market prices for every distinct marketable item. Steam
        rate-limits priceoverview hard, so this is slow and cancellable, and
        it skips prices fetched within the last day unless forced."""
        settings = self.store.get_settings()
        delay = float(settings.get("price_fetch_delay", "3.5"))
        force = bool(params.get("force"))
        names = self.store.distinct_market_names(marketable_only=True)
        if not force:
            fresh = self.store.price_age_map()
            today = time.strftime("%Y-%m-%d")
            names = [n for n in names
                     if not (fresh.get(n, "").startswith(today))]
        self._say(f"Fetching market prices for {len(names)} distinct items "
                  f"(~{delay:.0f}s each to respect Steam's rate limit — this is "
                  "slow; cancel any time, prices are saved as they arrive).",
                  phase="price_inventory")
        got = rl = 0
        for i, name in enumerate(names):
            if self._cancel.is_set():
                break
            self._progress(i + 1, len(names))
            res = self.steam.get_market_price(name)
            if res.get("rate_limited"):
                rl += 1
                self._say(f"Rate limited on '{name}' — backing off 60s "
                          f"({got} priced so far).", phase="rate_limited")
                for _ in range(30):
                    if self._cancel.is_set():
                        break
                    time.sleep(2)
                with self._lock:
                    self.status["phase"] = "price_inventory"
                continue
            if "error" not in res:
                self.store.upsert_price(name, res.get("lowest_cents"),
                                        res.get("median_cents"), res.get("volume", 0))
                got += 1
            time.sleep(delay)
        self.store.log_event("price_inventory", detail=f"{got} priced, {rl} rate-limits")
        self._say(("Price sweep cancelled" if self._cancel.is_set() else "Price sweep done")
                  + f" — {got} items priced.")

    def _job_list_market(self, params):
        """List queued inventory items for sale, then approve the pending
        confirmations (auto if an identity_secret is saved, otherwise leave
        them for the phone). params.ids optionally limits to specific assets."""
        from . import steamguard
        self._require_steam()
        ids = params.get("ids")
        if ids:
            queued = [self.store.get_inventory_asset(a) for a in ids]
            queued = [q for q in queued if q and q["sale_price_cents"]]
        else:
            queued = self.store.queued_for_sale()
        if not queued:
            self._say("No items queued for sale. Set a price on inventory items first.")
            return
        self._say(f"Listing {len(queued)} items on the Steam market...",
                  phase="list_market")
        listed = failed = 0
        for i, it in enumerate(queued):
            if self._cancel.is_set():
                break
            self._progress(i + 1, len(queued))
            ok, msg, needs_conf = self.steam.create_market_listing(
                it["assetid"], it["sale_price_cents"])
            if ok:
                listed += 1
                self.store.set_inventory_fields(
                    it["assetid"],
                    sale_state="pending" if needs_conf else "listed",
                    listed_at=now_str())
            else:
                failed += 1
                self.store.set_inventory_fields(
                    it["assetid"], sale_state="error", sale_note=msg[:200])
                self._say(f"{it['name']}: {msg}")
            time.sleep(1.5)
        self.store.log_event("list_market", detail=f"{listed} listed, {failed} failed")

        # confirmations
        confirmed = 0
        if steamguard.has_secret():
            self._say("Approving pending market confirmations...", phase="confirm_market")
            try:
                session = self.steam._session
                confirmed, total = steamguard.accept_all_market_confirmations(session)
                for it in queued:
                    if it["assetid"] and self.store.get_inventory_asset(
                            it["assetid"])["sale_state"] == "pending":
                        self.store.set_inventory_fields(it["assetid"], sale_state="listed")
                self._say(f"Auto-confirmed {confirmed} of {total} pending confirmations.")
            except Exception as e:
                self._say(f"Auto-confirm failed ({e}) — approve the listings in your "
                          "Steam mobile app instead.")
        else:
            self._say(f"{listed} items listed and waiting for confirmation — open the "
                      "Steam mobile app, go to Confirmations, and approve them. "
                      "(Add your identity_secret in Settings to auto-confirm.)")
        self._say(f"Market listing finished — {listed} listed, {failed} failed"
                  + (f", {confirmed} auto-confirmed" if confirmed else "") + ".")

    def _job_full_auto(self, params):
        self._job_claim_choices({})
        if self._cancel.is_set():
            return
        self._job_sync_humble({})
        if self._cancel.is_set():
            return
        self._job_sync_steam({})
        if self._cancel.is_set():
            return
        self._job_match({})
        if self._cancel.is_set():
            return
        self._job_redeem(params)
        if self._cancel.is_set():
            return
        try:
            self._job_verify_licenses({})
        except Exception as e:
            # verification is a nice-to-have; don't fail the whole run on it
            self._say(f"License verification skipped: {e}")
        if self.gog is not None and (self.gog.is_logged_in() or self.gog.try_cookie_login()):
            try:
                self._job_redeem_gog({})
            except Exception as e:
                self._say(f"GOG redemption skipped: {e}")

    def _set_result(self, key, code, msg):
        label = code_to_label(code)
        if code == 0:
            status = "redeemed"
        elif code in (9, 15):
            status = "already_owned"
        else:
            status = "errored"
        self.store.set_key_fields(key["id"], redeem_status=status,
                                  last_result_code=code, last_result_label=label,
                                  last_attempt_at=now_str())
        self.store.log_event("redeem_steam", key["gamekey"], key["machine_name"],
                             key["human_name"], detail=msg,
                             result_code=code, result_label=label)
        append_legacy_csv(status, key)
        self.store.record_legacy_row(status, key["gamekey"], key["human_name"],
                                     key["redeemed_key_val"])


def can_reveal(key):
    """Bootstrap rows imported from master CSVs lack the machine_name/keyindex
    that Humble's reveal API requires — a live sync fills those in."""
    return (key["machine_name"]
            and not key["machine_name"].startswith("csv-import:")
            and key["keyindex"] is not None)


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def append_legacy_csv(status, key):
    """Keep the old redeemed/already_owned/errored CSVs in sync so the
    original scripts still see the app's activity."""
    filename = {"redeemed": "redeemed.csv",
                "already_owned": "already_owned.csv",
                "errored": "errored.csv"}.get(status)
    if not filename:
        return
    try:
        with open(filename, "a", encoding="utf-8-sig") as f:
            name = (key["human_name"] or "").replace(",", ".")
            f.write(f"{key['gamekey']},{name},{key['redeemed_key_val']}\n")
    except OSError:
        pass


def matching_find_tpks(orders):
    """Every key-bearing node in the order details."""
    from .humble_client import find_dict_keys
    return find_dict_keys(orders, "key_type_human_name", parent=True)
