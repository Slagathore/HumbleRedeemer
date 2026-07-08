"""Humble Steam Key Redeemer — local web app.

Run:  python app.py   (then open http://127.0.0.1:5757)
"""
import glob
import os
import threading
import time
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

from redeemer.store import Store
from redeemer.humble_client import HumbleClient
from redeemer.steam_client import SteamClient
from redeemer.gog_client import GogClient
from redeemer.jobs import JobRunner
from redeemer import attention

HOST = "127.0.0.1"
PORT = 5757


def _code_version():
    """Newest mtime across the source files — shown in the UI so a stale
    running process (old code in memory) is immediately visible."""
    files = ["app.py", "static/index.html"] + glob.glob("redeemer/*.py")
    stamps = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    if not stamps:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(max(stamps)))


APP_VERSION = _code_version()

app = Flask(__name__, static_folder="static")
store = Store()
humble = HumbleClient()
steam = SteamClient()
gog = GogClient()
runner = JobRunner(store, humble, steam, gog)

# Migrate defaults from the original script if settings are empty
_settings = store.get_settings()
if not _settings.get("steam_api_key") or not _settings.get("steam_id_64"):
    try:
        import humblesteamkeysredeemer as legacy
        store.set_settings({
            "steam_api_key": _settings.get("steam_api_key") or getattr(legacy, "STEAM_API_KEY", ""),
            "steam_id_64": _settings.get("steam_id_64") or getattr(legacy, "STEAM_ID_64", ""),
        })
    except Exception:
        pass

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
    try:
        gog.try_cookie_login()
    except Exception:
        pass
    try:
        humble.try_cookie_login()
    except Exception:
        pass


threading.Thread(target=_restore_sessions, daemon=True).start()


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


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


@app.post("/api/settings")
def api_settings():
    data = request.get_json(force=True) or {}
    allowed = {"steam_api_key", "steam_id_64", "fuzzy_threshold", "per_key_delay",
               "rate_limit_wait_min", "auto_reveal", "redeem_likely_owned"}
    store.set_settings({k: v for k, v in data.items() if k in allowed})
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


@app.post("/api/login/gog")
def api_login_gog():
    """Opens a visible browser window on this machine for GOG sign-in —
    credentials never pass through the app."""
    if gog.is_logged_in() or gog.try_cookie_login():
        return jsonify({"status": "ok", "message": "Already signed in to GOG."})
    ok, msg = gog.start_interactive_login()
    return jsonify({"status": "waiting" if ok else "error",
                    "message": "A browser window opened — sign in to GOG there. "
                               "This page will update when you're done." if ok else msg})


@app.get("/api/attention")
def api_attention():
    return jsonify(attention.build_report(store))


@app.post("/api/job")
def api_job():
    data = request.get_json(force=True) or {}
    job_type = data.get("type", "")
    if job_type not in ("sync_humble", "sync_steam", "match", "reveal",
                        "redeem", "claim_choices", "verify_licenses",
                        "sync_gog", "redeem_gog", "verify_spares", "full_auto"):
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
    """Mark spares as given away (or un-mark), with an optional note of who got it."""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    given = 1 if data.get("given", True) else 0
    note = (data.get("note") or "").strip()
    for key_id in ids:
        key = store.get_key(key_id)
        if not key:
            continue
        if note:
            store.set_key_fields(key_id, given_away=given, notes=note)
        else:
            store.set_key_fields(key_id, given_away=given)
        store.log_event("given_away" if given else "given_away_undone",
                        key["gamekey"], key["machine_name"], key["human_name"],
                        detail=note)
    return jsonify({"ok": True, "count": len(ids)})


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


if __name__ == "__main__":
    print(f"Humble Steam Key Redeemer — open http://{HOST}:{PORT}")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
