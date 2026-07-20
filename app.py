"""Humble Steam Key Redeemer — local web app.

Run:  python app.py   (then open http://127.0.0.1:5757)
"""
import atexit
import glob
import json
import os
import signal
import socket
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

# PyInstaller bundle support: static assets live in the unpack dir, while the
# database/cookies live in the data dir. For the portable exe and source runs
# that's still right next to the app; for the installed build it's
# %LOCALAPPDATA%\HumbleRedeemer, which no install or uninstall step touches,
# so updating the app can never take your keys and sessions with it.
# See redeemer/paths.py.
from redeemer import paths

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    DATA_DIR = paths.ensure(paths.data_dir())
    paths.migrate_from(paths.program_dir(), DATA_DIR)
    os.chdir(DATA_DIR)
    STATIC_DIR = os.path.join(getattr(sys, "_MEIPASS", "."), "static")
else:
    DATA_DIR = os.getcwd()
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Windowless mode (pythonw / --windowed build): stdout doesn't exist, so keep
# print/log output in app.log next to the database instead of losing it.
if sys.stdout is None or sys.stderr is None:
    try:
        _mode = "w" if (os.path.exists("app.log")
                        and os.path.getsize("app.log") > 1_000_000) else "a"
        _log = open("app.log", _mode, buffering=1, encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = _log
        if sys.stderr is None:
            sys.stderr = _log
        print(f"--- launched {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    except OSError:
        pass  # read-only dir — run silent

# the per-request lines have no value in a desktop app and bloat app.log
import logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from redeemer.store import Store
from redeemer.humble_client import HumbleClient
from redeemer.steam_client import SteamClient
from redeemer.gog_client import GogClient
from redeemer.jobs import JobRunner
from redeemer import attention
from redeemer import update_check
from redeemer import update_install
from redeemer import steamguard as _steamguard

HOST = "127.0.0.1"
PORT = int(os.environ.get("APP_PORT", "5757"))


def _code_version():
    """Newest mtime across the source files — shown in the UI so a stale
    running process (old code in memory) is immediately visible."""
    if FROZEN:
        stamp = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(sys.executable)))
        tag = update_check.build_tag()  # release tag stamped in by CI
        return f"{tag} ({stamp})" if tag else "build " + stamp
    files = ["app.py", "static/index.html"] + glob.glob("redeemer/*.py")
    stamps = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    if not stamps:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(max(stamps)))


APP_VERSION = _code_version()

app = Flask(__name__, static_folder=STATIC_DIR)
store = Store()
humble = HumbleClient()
steam = SteamClient()
gog = GogClient()
runner = JobRunner(store, humble, steam, gog)

# One-time import of the old CSV history (idempotent)
imported = store.import_legacy_csvs()
loaded = store.bootstrap_from_master_csvs()
if loaded["humble"] or loaded["steam"]:
    print(f"Bootstrapped from master CSVs: {loaded['humble']} Humble keys, "
          f"{loaded['steam']} Steam library apps.")
if imported or loaded["humble"]:
    applied = store.apply_legacy_statuses()
    print(f"Imported {imported} rows of history from legacy CSVs "
          f"(status applied to {applied} keys).")


def _restore_sessions():
    """Try saved cookies for both services in the background at startup."""
    try:
        steam.try_cookie_login()
    except Exception:
        pass
    # GOG sign-in disabled for now
    # try:
    #     gog.try_cookie_login()
    # except Exception:
    #     pass
    try:
        humble.try_cookie_login()
    except Exception:
        pass


def _startup_update_scan():
    """Non-blocking update check at launch; result is cached for the UI."""
    status = update_check.get_status()
    if status.get("update_available"):
        behind = status.get("behind_by")
        print(f"⬆ Update available on GitHub"
              + (f" ({behind} new commit{'s' if behind != 1 else ''})" if behind else "")
              + f": {status.get('remote_message', '')}\n  {update_check.REPO_URL}")
    notice = status.get("emergency") or {}
    if status.get("update_available") and notice.get("emergency"):
        print(f"⚠ URGENT UPDATE: {notice.get('title', '')} — {notice.get('message', '')}")


if not os.environ.get("APP_NO_RESTORE"):
    threading.Thread(target=_restore_sessions, daemon=True).start()
if not os.environ.get("APP_NO_UPDATE_CHECK"):
    threading.Thread(target=_startup_update_scan, daemon=True).start()


# ---------------- lifecycle: clean exit, no orphaned browsers ----------------

_shutting_down = threading.Event()


def _cleanup():
    """Stop the worker and quit headless browsers — safe to call twice."""
    if _shutting_down.is_set():
        return
    _shutting_down.set()
    try:
        runner.stop(timeout=5)
    except Exception:
        pass
    try:
        humble.shutdown()
    except Exception:
        pass


atexit.register(_cleanup)
for _sig in ("SIGTERM", "SIGBREAK"):
    if hasattr(signal, _sig):
        try:
            signal.signal(getattr(signal, _sig), lambda *_: sys.exit(0))
        except (ValueError, OSError):
            pass  # not the main thread / unsupported — atexit still covers us


# --------------- request guard: this app is 127.0.0.1-only ---------------
# Blocks DNS-rebinding (Host must be ours) and cross-site POSTs from web pages
# (a random website must not be able to start jobs or reveal keys).

_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}


@app.before_request
def _local_only_guard():
    if request.host not in _ALLOWED_HOSTS:
        return jsonify({"ok": False, "message": "Bad Host header."}), 403
    if request.method == "POST":
        origin = request.headers.get("Origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            return jsonify({"ok": False,
                            "message": "Cross-origin request blocked."}), 403


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_DIR, "icon.ico")


@app.get("/api/state")
def api_state():
    return jsonify({
        "version": APP_VERSION,
        "metrics": store.metrics(),
        "job": runner.snapshot(),
        "logins": {
            "humble": humble.is_logged_in(),
            "steam": steam.is_logged_in(),
            "gog": gog.is_logged_in(),
            "gog_login": gog.login_state,
        },
        "settings": store.get_settings(),
    })


@app.get("/api/games")
def api_games():
    return jsonify(store.get_keys())


@app.get("/api/activity")
def api_activity():
    return jsonify(store.redemptions_by_day())


@app.get("/api/events")
def api_events():
    return jsonify(store.get_events(limit=300))


# numeric settings: (min, max) — clamped so a stray value can't crash a job
_NUMERIC_SETTINGS = {
    "fuzzy_threshold": (70, 100),
    "per_key_delay": (0, 600),
    "rate_limit_wait_min": (1, 720),
    "price_fetch_delay": (1, 30),
}


@app.post("/api/settings")
def api_settings():
    data = request.get_json(force=True) or {}
    allowed = {"steam_api_key", "steam_id_64", "fuzzy_threshold", "per_key_delay",
               "rate_limit_wait_min", "auto_reveal", "redeem_likely_owned", "tray",
               "streaming_mode", "price_fetch_delay"}
    updates = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k in _NUMERIC_SETTINGS:
            lo, hi = _NUMERIC_SETTINGS[k]
            try:
                v = min(hi, max(lo, float(v)))
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "message": f"{k} must be a number."}), 400
        elif k in ("auto_reveal", "redeem_likely_owned", "tray", "streaming_mode"):
            v = "1" if str(v) in ("1", "true", "True") else "0"
        else:
            v = str(v).strip()
        updates[k] = v
    store.set_settings(updates)
    return jsonify({"ok": True})


@app.post("/api/login/humble")
def api_login_humble():
    data = request.get_json(force=True) or {}
    result = humble.login(
        email=data.get("email"), password=data.get("password"),
        code=data.get("code"), guard=data.get("guard"))
    return jsonify(result)


@app.post("/api/login/steam")
def api_login_steam():
    data = request.get_json(force=True) or {}
    result = steam.login(
        username=data.get("username"), password=data.get("password"),
        code=data.get("code"))
    return jsonify(result)


# GOG sign-in disabled for now
# @app.post("/api/login/gog")
# def api_login_gog():
#     """Opens a visible browser window on this machine for GOG sign-in —
#     credentials never pass through the app."""
#     if gog.is_logged_in() or gog.try_cookie_login():
#         return jsonify({"status": "ok", "message": "Already signed in to GOG."})
#     ok, msg = gog.start_interactive_login()
#     return jsonify({"status": "waiting" if ok else "error",
#                     "message": "A browser window opened — sign in to GOG there. "
#                                "This page will update when you're done." if ok else msg})


@app.get("/api/attention")
def api_attention():
    return jsonify(attention.build_report(store))


@app.post("/api/job")
def api_job():
    data = request.get_json(force=True) or {}
    job_type = data.get("type", "")
    if job_type not in ("sync_humble", "sync_steam", "match", "reveal",
                        "redeem", "claim_choices", "verify_licenses",
                        # "sync_gog", "redeem_gog",  # GOG automation disabled for now
                        "verify_spares",
                        "scan_inventory", "price_inventory", "list_market",
                        "full_auto"):
        return jsonify({"ok": False, "message": "Unknown job type."}), 400
    ok, msg = runner.start(job_type, data.get("params") or {})
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/job/cancel")
def api_job_cancel():
    runner.cancel()
    return jsonify({"ok": True})


@app.post("/api/keys/mark")
def api_keys_mark():
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    status = data.get("status", "")
    if status not in ("owned_manual", "not_owned_manual", "unowned"):
        return jsonify({"ok": False, "message": "Bad status."}), 400
    for key_id in ids:
        key = store.get_key(key_id)
        if key:
            store.set_key_fields(key_id, match_status=status)
            store.log_event("manual_mark", key["gamekey"], key["machine_name"],
                            key["human_name"], detail=status)
    return jsonify({"ok": True, "count": len(ids)})


@app.get("/api/giveaway")
def api_giveaway():
    include_given = request.args.get("include_given") == "1"
    return jsonify(store.get_giveaway_keys(include_given))


@app.post("/api/keys/give")
def api_keys_give():
    """Mark spares as given away (or un-mark), with an optional note of who got it.

    Marking stamps when it happened and freezes the row's status at that
    moment (given_snapshot), so a later "the key didn't work" dispute can
    show exactly what we believed when it was handed out."""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    given = 1 if data.get("given", True) else 0
    note = (data.get("note") or "").strip()
    for key_id in ids:
        key = store.get_key(key_id)
        if not key:
            continue
        fields = {"given_away": given, "given_at": ""}
        if note:
            fields["notes"] = note
        if given:
            fields["given_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            fields["given_snapshot"] = json.dumps({
                "redeem_status": key.get("redeem_status", ""),
                "last_result_code": key.get("last_result_code"),
                "last_result_label": key.get("last_result_label", ""),
                "match_status": key.get("match_status", ""),
                "license_provenance": key.get("license_provenance", ""),
                "steam_license": key.get("steam_license", ""),
                "revealed": key.get("revealed", 0),
                "expires": key.get("expires", ""),
            })
        store.set_key_fields(key_id, **fields)
        store.log_event("given_away" if given else "given_away_undone",
                        key["gamekey"], key["machine_name"], key["human_name"],
                        detail=note)
    return jsonify({"ok": True, "count": len(ids)})


@app.get("/api/gifts")
def api_gifts():
    """Live scan of the Steam gifts inventory (multi-pack extra copies)."""
    try:
        sid = store.get_settings().get("steam_id_64", "")
        gifts = steam.get_gift_inventory(sid)
        return jsonify({"ok": True, "gifts": gifts})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e), "gifts": []})


@app.get("/api/inventory")
def api_inventory():
    """Cached community-inventory items joined with market prices."""
    return jsonify({
        "items": store.get_inventory(),
        "synced_at": store.inventory_synced_at(),
        "has_secret": _steamguard.has_secret(),
    })


@app.post("/api/inventory/sale")
def api_inventory_sale():
    """Queue/unqueue inventory assets for sale with a target price (cents you
    receive). action: 'queue' | 'clear'. price_cents required for 'queue'."""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    action = data.get("action", "queue")
    price = data.get("price_cents")
    if action == "queue":
        try:
            price = int(price)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "A whole-cent price is required."}), 400
        if price < 1:
            return jsonify({"ok": False, "message": "Price must be at least 1 cent."}), 400
    n = 0
    for a in ids:
        it = store.get_inventory_asset(a)
        if not it:
            continue
        if action == "clear":
            store.set_inventory_fields(a, sale_state="", sale_price_cents=None, sale_note="")
        elif it["marketable"]:
            store.set_inventory_fields(a, sale_state="queued", sale_price_cents=price)
        n += 1
    return jsonify({"ok": True, "count": n})


@app.get("/api/steamguard")
def api_steamguard_status():
    return jsonify({"has_secret": _steamguard.has_secret()})


@app.post("/api/steamguard")
def api_steamguard_save():
    data = request.get_json(force=True) or {}
    try:
        sid = data.get("steamid") or store.get_settings().get("steam_id_64", "")
        _steamguard.save_secret(data.get("identity_secret", ""), sid)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/api/steamguard/clear")
def api_steamguard_clear():
    _steamguard.clear_secret()
    return jsonify({"ok": True})


@app.get("/api/steamguard/test")
def api_steamguard_test():
    """Read-only validation: fetch (not accept) pending confirmations. Proves
    the saved secret authenticates without touching anything."""
    if not (steam.is_logged_in() or steam.try_cookie_login()):
        return jsonify({"ok": False, "message": "Sign in to Steam first."}), 401
    try:
        confs = _steamguard.fetch_confirmations(steam._session)
        return jsonify({"ok": True, "pending": len(confs)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.post("/api/keys/manual")
def api_keys_manual():
    """Add hand-entered keys (from anywhere) straight to the giveaway list."""
    data = request.get_json(force=True) or {}
    entries = data.get("entries") or []
    added, skipped = store.add_manual_keys(entries)
    if added:
        store.log_event("manual_add", detail=f"{added} keys added, {skipped} skipped")
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.post("/api/keys/manual/delete")
def api_keys_manual_delete():
    data = request.get_json(force=True) or {}
    n = store.delete_manual_key(data.get("id"))
    if n:
        store.log_event("manual_remove", detail=str(data.get("id")))
    return jsonify({"ok": bool(n),
                    "message": "" if n else "Only manually added keys can be removed."})


@app.post("/api/keys/giftlink")
def api_keys_giftlink():
    """Turn an unrevealed spare into a Humble gift link (one key at a time)."""
    data = request.get_json(force=True) or {}
    key = store.get_key(data.get("id"))
    if not key:
        return jsonify({"ok": False, "message": "Unknown key."}), 404
    if key["revealed"] or key["redeemed_key_val"]:
        return jsonify({"ok": False, "message": "Key is already revealed."}), 400
    if not key["machine_name"] or key["machine_name"].startswith("csv-import:") \
            or key["keyindex"] is None:
        return jsonify({"ok": False, "message": "Needs a Humble sync first."}), 400
    if not (humble.is_logged_in() or humble.try_cookie_login()):
        return jsonify({"ok": False, "message": "Not signed in to Humble."}), 401
    ok, value = humble.reveal_key(key["gamekey"], key["machine_name"],
                                  key["keyindex"], gift=True)
    store.log_event("gift_link", key["gamekey"], key["machine_name"],
                    key["human_name"], detail=value if not ok else "",
                    result_label="success" if ok else "error")
    if not ok:
        return jsonify({"ok": False, "message": f"Humble refused: {value}"})
    store.set_key_fields(key["id"], redeemed_key_val=value, revealed=1,
                         redeem_status="gift_link", last_result_label="gift_link")
    return jsonify({"ok": True, "link": value})


@app.post("/api/keys/reset")
def api_keys_reset():
    """Clear redeem_status so entries re-enter the candidate pool (e.g. keys
    the old pipeline marked errored without ever revealing them)."""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    if data.get("all_errored"):
        ids = [k["id"] for k in store.get_keys("redeem_status='errored'")]
    count = 0
    for key_id in ids:
        key = store.get_key(key_id)
        if key and key["redeem_status"] != "redeemed":
            store.set_key_fields(key_id, redeem_status="",
                                 last_result_code=None, last_result_label="")
            store.log_event("reset_status", key["gamekey"], key["machine_name"],
                            key["human_name"])
            count += 1
    return jsonify({"ok": True, "count": count})


@app.get("/api/update")
def api_update():
    """Update status merged with the user's silence preference.

    `show` / `emergency_show` are computed here so the UI stays dumb:
      - silence 'forever'     — never show a normal banner again
      - silence 'until_next'  — quiet only while the remote head is still the
                                one that was silenced; a new push re-notifies
      - emergency             — repo's update_notice.json overrides both
    """
    status = update_check.get_status(force=request.args.get("refresh") == "1")
    settings = store.get_settings()
    mode = settings.get("update_silence", "")
    silenced_sha = settings.get("update_silence_sha", "")
    silenced = (mode == "forever"
                or (mode == "until_next" and status.get("remote_sha")
                    and status["remote_sha"] == silenced_sha))
    notice = status.get("emergency") or {}
    emergency_show = bool(status.get("ok") and status.get("update_available")
                          and notice.get("emergency"))
    show = emergency_show or bool(status.get("ok")
                                  and status.get("update_available")
                                  and not silenced)
    can_install, install_note = update_install.supported()
    return jsonify({**status, "silence_mode": mode, "silenced": bool(silenced),
                    "show": show, "emergency_show": emergency_show,
                    "can_install": can_install, "install_note": install_note,
                    "install": update_install.status(),
                    "repo_url": update_check.REPO_URL})


@app.get("/api/update/install")
def api_update_install_status():
    """Where the install attempt actually is: idle / checking / downloading /
    verifying / ready / installing / failed. Nothing here ever reports
    progress the download hasn't made."""
    can_install, note = update_install.supported()
    return jsonify({**update_install.status(),
                    "can_install": can_install, "install_note": note})


@app.post("/api/update/install")
def api_update_install():
    """Download the release's installer and verify it. Does not run it --
    that's a separate, explicit step, and it's only allowed once this one has
    proved the download's checksum and signature."""
    can_install, note = update_install.supported()
    if not can_install:
        return jsonify({"ok": False, "state": "unsupported", "message": note,
                        "can_install": False, "install_note": note}), 400
    return jsonify({"ok": True, **update_install.start(), "can_install": True})


@app.post("/api/update/install/run")
def api_update_install_run():
    """Launch the verified installer and quit so it can replace the exe."""
    can_install, note = update_install.supported()
    if not can_install:
        return jsonify({"ok": False, "message": note}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    if runner.snapshot()["running"] and not force:
        return jsonify({"ok": False,
                        "message": "A job is running — cancel it first."}), 409
    try:
        state = update_install.launch()
    except update_install.VerificationError as e:
        return jsonify({"ok": False, "message": str(e),
                        **update_install.status()}), 409
    except Exception as e:
        message = f"Couldn't start the installer ({e.__class__.__name__})."
        update_install._fail(message)
        return jsonify({"ok": False, "message": message}), 500
    _exit_soon(1.5)  # give Inno time to come up before we drop the process
    return jsonify({"ok": True, **state})


@app.post("/api/update/silence")
def api_update_silence():
    mode = (request.get_json(force=True) or {}).get("mode", "")
    if mode == "off":
        mode = ""
    if mode not in ("", "until_next", "forever"):
        return jsonify({"ok": False, "message": "Bad mode."}), 400
    status = update_check.get_status()
    store.set_settings({"update_silence": mode,
                        "update_silence_sha": status.get("remote_sha") or ""})
    return jsonify({"ok": True, "mode": mode})


def _exit_soon(delay=0.4):
    """Quit the process in the background, after the current response has had
    time to reach the browser."""
    def _go():
        time.sleep(delay)
        # Cleanup must finish BEFORE the tray stops: stopping the icon
        # unblocks the main thread, which exits the process and would kill
        # this thread mid-driver.quit(), orphaning chromedriver — which
        # inherits our server socket and holds the port hostage.
        _cleanup()
        icon = _TRAY.get("icon")
        if icon is not None:
            try:
                icon.visible = False
                icon.stop()
            except Exception:
                pass
        os._exit(0)

    threading.Thread(target=_go, daemon=True).start()


@app.post("/api/shutdown")
def api_shutdown():
    """Graceful quit from the UI: refuses while a job runs unless forced."""
    force = bool((request.get_json(silent=True) or {}).get("force"))
    if runner.snapshot()["running"] and not force:
        return jsonify({"ok": False,
                        "message": "A job is running — cancel it first."}), 409
    _exit_soon()
    return jsonify({"ok": True, "message": "Shutting down."})


# ---------------- system tray (Windows) ----------------

_TRAY: dict = {"icon": None}


def _make_tray():
    """Build the tray icon, or None if pystray/Pillow aren't usable here.
    With the tray, closing the browser tab just 'minimizes' the app: it keeps
    running by the clock, and Open/Quit live in the icon's menu."""
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(os.path.join(STATIC_DIR, "icon-64.png"))
    except Exception:
        return None
    url = f"http://{HOST}:{PORT}"

    def _open(icon=None, item=None):
        webbrowser.open(url)

    def _quit(icon, item):
        icon.visible = False
        icon.stop()  # unblocks icon.run() in main; cleanup happens there

    try:
        return pystray.Icon(
            "HumbleRedeemer", img, f"Humble Steam Key Redeemer — {url}",
            pystray.Menu(
                pystray.MenuItem("Open dashboard", _open, default=True),
                pystray.MenuItem("Quit", _quit),
            ))
    except Exception:
        return None


def _port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def _serve():
    # make_server instead of app.run so we can mark the listening socket
    # non-inheritable: werkzeug marks it inheritable for its reloader, and
    # any child process spawned afterwards (selenium's chromedriver) would
    # inherit the handle and keep the port bound after we exit.
    from werkzeug.serving import make_server
    server = make_server(HOST, PORT, app, threaded=True)
    try:
        server.socket.set_inheritable(False)
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    if _port_in_use():
        # Second launch: don't crash with a bind traceback — just bring up
        # the already-running app (or tell the user who's squatting the port).
        print(f"Port {PORT} is already in use — the app looks like it's "
              f"already running. Opening http://{HOST}:{PORT} …\n"
              f"(If something else owns the port, set APP_PORT to another "
              f"number and relaunch.)")
        if not os.environ.get("APP_NO_BROWSER"):
            webbrowser.open(f"http://{HOST}:{PORT}")
        sys.exit(0)

    print(f"Humble Steam Key Redeem...er — open http://{HOST}:{PORT}")
    if not os.environ.get("APP_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    # Tray mode (default on where pystray works): the server runs by the
    # clock; closing the browser tab leaves it running, quit from the icon.
    use_tray = os.environ.get(
        "APP_TRAY", store.get_settings().get("tray", "1")) == "1"
    tray_icon = _make_tray() if use_tray else None
    _TRAY["icon"] = tray_icon

    if tray_icon is not None:
        threading.Thread(target=_serve, daemon=True).start()
        print("Running in the system tray — right-click the key icon to quit.")
        try:
            tray_icon.run()  # blocks main thread until Quit
        except KeyboardInterrupt:
            pass
        finally:
            _cleanup()
        sys.exit(0)

    try:
        _serve()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()
