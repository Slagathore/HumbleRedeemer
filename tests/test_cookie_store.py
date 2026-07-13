"""Unit tests for redeemer.cookie_store -- the JSON cookie persistence that
replaced pickle.load()/pickle.dump() on the session cookie files (H6).

pickle deserializes arbitrary Python objects, so a planted or tampered
cookie file used to mean code execution with live store session cookies in
scope the moment the app tried to restore a session. These tests prove the
replacement can't do that: a malformed or hostile file is rejected and
treated as "no saved session," never executed.
"""
import json
import pickle

from redeemer import cookie_store


# ---------------- round trip: behavior preserved ----------------

def test_cookie_list_round_trip(tmp_path):
    path = str(tmp_path / ".cookies")
    cookies = [
        {"name": "sessionid", "value": "abc123", "domain": ".gog.com", "path": "/"},
        {"name": "gog-al", "value": "xyz789", "domain": ".gog.com", "path": "/"},
    ]
    cookie_store.save_cookie_list(path, cookies)
    assert cookie_store.load_cookie_list(path) == cookies


def test_cookie_dict_round_trip(tmp_path):
    path = str(tmp_path / ".cookies")
    cookies = {"sessionid": "abc123", "steamLoginSecure": "765%7C%7Ctoken"}
    cookie_store.save_cookie_dict(path, cookies)
    assert cookie_store.load_cookie_dict(path) == cookies


# ---------------- missing file ----------------

def test_missing_cookie_list_returns_empty_list(tmp_path):
    assert cookie_store.load_cookie_list(str(tmp_path / "nope")) == []


def test_missing_cookie_dict_returns_none(tmp_path):
    assert cookie_store.load_cookie_dict(str(tmp_path / "nope")) is None


# ---------------- malformed content is rejected, not executed ----------------

def test_garbage_bytes_rejected_as_cookie_list(tmp_path):
    path = tmp_path / ".cookies"
    path.write_bytes(b"\x00\x01not json at all\xff\xfe")
    assert cookie_store.load_cookie_list(str(path)) == []


def test_garbage_bytes_rejected_as_cookie_dict(tmp_path):
    path = tmp_path / ".cookies"
    path.write_bytes(b"\x00\x01not json at all\xff\xfe")
    assert cookie_store.load_cookie_dict(str(path)) is None


def test_wrong_shape_json_rejected_as_cookie_list(tmp_path):
    path = tmp_path / ".cookies"
    path.write_text(json.dumps({"this": "is a dict, not a cookie list"}), encoding="utf-8")
    assert cookie_store.load_cookie_list(str(path)) == []


def test_wrong_shape_json_rejected_as_cookie_dict(tmp_path):
    path = tmp_path / ".cookies"
    path.write_text(json.dumps(["this", "is a list, not a cookie dict"]), encoding="utf-8")
    assert cookie_store.load_cookie_dict(str(path)) is None


def test_cookie_list_entries_missing_name_or_value_rejected(tmp_path):
    path = tmp_path / ".cookies"
    path.write_text(json.dumps([{"domain": ".gog.com"}]), encoding="utf-8")
    assert cookie_store.load_cookie_list(str(path)) == []


def test_cookie_dict_with_non_string_value_rejected(tmp_path):
    path = tmp_path / ".cookies"
    # not a plain str -> str mapping -- reject rather than pass an odd shape
    # downstream into requests.utils.cookiejar_from_dict / driver.add_cookie
    path.write_text(json.dumps({"sessionid": 12345}), encoding="utf-8")
    assert cookie_store.load_cookie_dict(str(path)) is None


# ---------------- hostile pickle payload is rejected, not executed ----------------

def test_hostile_pickle_payload_is_not_executed_as_cookie_list(tmp_path):
    """A file planted by an attacker (or simply left on disk from before this
    app switched off pickle) would run code the moment the OLD pickle.load()
    touched it. The new JSON loader must reject it outright, not run it."""
    marker = tmp_path / "pwned_list.marker"
    cookie_file = tmp_path / ".hostilecookies_list"

    class Exploit:
        def __reduce__(self):
            # if pickle.load() ever runs this, it calls open(marker, "w"),
            # which creates the marker file as a side effect of unpickling
            return (open, (str(marker), "w"))

    with open(cookie_file, "wb") as f:
        pickle.dump(Exploit(), f)

    # Sanity check: unpickling this file the OLD way really does execute
    # code. This proves the test below would catch a regression back to
    # pickle, not just that our new code happens to fail on this input.
    assert not marker.exists()
    with open(cookie_file, "rb") as f:
        pickle.load(f)
    assert marker.exists(), "sanity check failed: exploit payload did not fire"
    marker.unlink()

    # The real code path must not execute the payload -- it fails the JSON
    # parse and comes back empty, same as any other malformed file.
    assert cookie_store.load_cookie_list(str(cookie_file)) == []
    assert not marker.exists(), "load_cookie_list executed the hostile payload"


def test_hostile_pickle_payload_is_not_executed_as_cookie_dict(tmp_path):
    marker = tmp_path / "pwned_dict.marker"
    cookie_file = tmp_path / ".hostilecookies_dict"

    class Exploit:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    with open(cookie_file, "wb") as f:
        pickle.dump(Exploit(), f)

    assert not marker.exists()
    with open(cookie_file, "rb") as f:
        pickle.load(f)
    assert marker.exists(), "sanity check failed: exploit payload did not fire"
    marker.unlink()

    assert cookie_store.load_cookie_dict(str(cookie_file)) is None
    assert not marker.exists(), "load_cookie_dict executed the hostile payload"
