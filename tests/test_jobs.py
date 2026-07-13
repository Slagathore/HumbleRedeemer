"""Unit tests for redeemer.jobs -- the redeem run's duplicate guard and
result mapping, two of the money paths the 2026-07 audit named:

  - jobs._job_redeem's duplicate guard: if the same game shows up twice in
    one run, the second one must be short circuited as "already owned"
    instead of actually spending an activation attempt on Steam.
  - jobs._set_result's code -> status mapping: a wrong mapping here
    silently drops a real key (marks it something other than what Steam
    actually said) or wrongly marks an unredeemed key as done.

Everything here runs against fake store/steam/humble objects. Nothing
touches a real database, a real Steam session, or the repo's tracked
redeemed.csv / already_owned.csv / errored.csv output files (the autouse
fixture below chdirs every test into an isolated tmp_path first).
"""
import pytest

from redeemer.jobs import JobRunner
from redeemer.steam_client import code_to_label


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # _set_result's append_legacy_csv() writes to relative-path csv files in
    # the current directory -- never let a test touch the real repo files.
    monkeypatch.chdir(tmp_path)


DEFAULT_SETTINGS = {
    "auto_reveal": "1",
    "redeem_likely_owned": "0",
    "per_key_delay": "0",
    "rate_limit_wait_min": "0.001",
}


class FakeSteamClient:
    """Stub Steam client -- never touches the network. Records every
    redeem_key() call and answers from a canned response table."""

    def __init__(self, responses=None, always_none=False):
        self.calls = []
        self.responses = responses or {}
        self.always_none = always_none
        self.cookie_login_calls = 0

    def is_logged_in(self, recheck=False):
        return True

    def try_cookie_login(self):
        self.cookie_login_calls += 1
        return True

    def redeem_key(self, key_val):
        self.calls.append(key_val)
        if self.always_none:
            return None, "session hiccup"
        return self.responses.get(key_val, (0, "Redeemed: Test Game"))


class FakeStore:
    """Stub Store -- an in-memory dict of keys, no SQLite, no disk."""

    def __init__(self, keys, settings=None):
        self._keys = {k["id"]: k for k in keys}
        self._settings = settings or {}
        self.updates = {}       # key_id -> fields from the LAST set_key_fields call
        self.all_updates = []   # every set_key_fields call, in order
        self.events = []
        self.legacy_rows = []

    def get_settings(self):
        return dict(self._settings)

    def get_key(self, key_id):
        return self._keys.get(key_id)

    def get_keys(self, where="", params=()):
        return list(self._keys.values())

    def set_key_fields(self, key_id, **fields):
        self.updates[key_id] = fields
        self.all_updates.append((key_id, fields))

    def log_event(self, action, gamekey="", machine_name="", name="", detail="",
                  result_code=None, result_label="", ts=None):
        self.events.append({
            "action": action, "gamekey": gamekey, "machine_name": machine_name,
            "name": name, "detail": detail, "result_code": result_code,
            "result_label": result_label,
        })

    def record_legacy_row(self, status, gamekey, human_name, key_val):
        self.legacy_rows.append((status, gamekey, human_name, key_val))


def make_key(id, human_name="Test Game", steam_app_id=None,
             redeemed_key_val="AAAAA-BBBBB-CCCCC", revealed=1,
             redeem_status="", gamekey="gk", machine_name="mn"):
    return {
        "id": id, "gamekey": gamekey, "machine_name": machine_name,
        "human_name": human_name, "steam_app_id": steam_app_id,
        "redeemed_key_val": redeemed_key_val, "revealed": revealed,
        "redeem_status": redeem_status,
    }


def _runner(store, steam, humble=None):
    return JobRunner(store=store, humble=humble or object(), steam=steam, gog=None)


# ---------------- duplicate guard ----------------

def test_duplicate_guard_same_appid_short_circuits_second_key():
    k1 = make_key(1, human_name="Game A", steam_app_id=440,
                  redeemed_key_val="AAAAA-BBBBB-CCCCC")
    k2 = make_key(2, human_name="Game A (dup listing)", steam_app_id=440,
                  redeemed_key_val="DDDDD-EEEEE-FFFFF")
    store = FakeStore([k1, k2], DEFAULT_SETTINGS)
    steam = FakeSteamClient(responses={"AAAAA-BBBBB-CCCCC": (0, "Redeemed")})
    runner = _runner(store, steam)

    runner._job_redeem({"ids": [1, 2]})

    # the duplicate never reached Steam at all
    assert steam.calls == ["AAAAA-BBBBB-CCCCC"]
    assert store.updates[1]["redeem_status"] == "redeemed"
    assert store.updates[2]["redeem_status"] == "already_owned"
    assert store.updates[2]["last_result_code"] == 9


def test_duplicate_guard_uses_normalized_name_when_no_appid():
    k1 = make_key(1, human_name="Some Game!!", steam_app_id=None,
                  redeemed_key_val="AAAAA-BBBBB-CCCCC")
    k2 = make_key(2, human_name="SOME GAME", steam_app_id=None,
                  redeemed_key_val="DDDDD-EEEEE-FFFFF")
    store = FakeStore([k1, k2], DEFAULT_SETTINGS)
    steam = FakeSteamClient(responses={"AAAAA-BBBBB-CCCCC": (0, "Redeemed")})
    runner = _runner(store, steam)

    runner._job_redeem({"ids": [1, 2]})

    assert steam.calls == ["AAAAA-BBBBB-CCCCC"]
    assert store.updates[2]["redeem_status"] == "already_owned"


def test_duplicate_guard_does_not_fire_for_different_games():
    k1 = make_key(1, human_name="Game A", steam_app_id=440,
                  redeemed_key_val="AAAAA-BBBBB-CCCCC")
    k2 = make_key(2, human_name="Game B", steam_app_id=620,
                  redeemed_key_val="DDDDD-EEEEE-FFFFF")
    store = FakeStore([k1, k2], DEFAULT_SETTINGS)
    steam = FakeSteamClient(responses={
        "AAAAA-BBBBB-CCCCC": (0, "Redeemed A"),
        "DDDDD-EEEEE-FFFFF": (0, "Redeemed B"),
    })
    runner = _runner(store, steam)

    runner._job_redeem({"ids": [1, 2]})

    assert steam.calls == ["AAAAA-BBBBB-CCCCC", "DDDDD-EEEEE-FFFFF"]
    assert store.updates[1]["redeem_status"] == "redeemed"
    assert store.updates[2]["redeem_status"] == "redeemed"


# ---------------- non Steam-key values never reach Steam ----------------

def test_gift_link_value_never_reaches_steam():
    k1 = make_key(1, redeemed_key_val="https://www.humblebundle.com/gift?key=abc123")
    store = FakeStore([k1], DEFAULT_SETTINGS)
    steam = FakeSteamClient()
    runner = _runner(store, steam)

    runner._job_redeem({"ids": [1]})

    assert steam.calls == []
    assert store.updates[1]["redeem_status"] == "gift_link"


# ---------------- session death must not drop a false verdict on any key ----------------

def test_session_death_marks_no_key_and_stops_the_run():
    """code=None means the session died / Steam replied garbage -- it is
    NOT a verdict on the key. The run must retry once, then stop without
    marking that key or any key after it (they would all be falsely
    'errored' otherwise)."""
    k1 = make_key(1, redeemed_key_val="AAAAA-BBBBB-CCCCC")
    k2 = make_key(2, redeemed_key_val="DDDDD-EEEEE-FFFFF")
    store = FakeStore([k1, k2], DEFAULT_SETTINGS)
    steam = FakeSteamClient(always_none=True)
    runner = _runner(store, steam)

    runner._job_redeem({"ids": [1, 2]})

    assert store.updates == {}  # neither key got a status written
    # k1 attempted twice (initial + one reconnect retry), k2 never reached
    assert steam.calls == ["AAAAA-BBBBB-CCCCC", "AAAAA-BBBBB-CCCCC"]
    assert steam.cookie_login_calls == 1
    assert any(e["result_label"] == "session_lost" for e in store.events)


# ---------------- _set_result: code -> status mapping ----------------

@pytest.mark.parametrize("code,expected_status", [
    (0, "redeemed"),
    (9, "already_owned"),
    (15, "already_owned"),
    (14, "errored"),           # invalid code
    (13, "errored"),           # region locked
    (53, "errored"),           # rate limited (never reaches _set_result in
                                # the normal run loop, but the mapping must
                                # still be correct if it ever does)
    (None, "errored"),         # unknown / session weirdness
])
def test_set_result_code_to_status_mapping(code, expected_status):
    store = FakeStore([], {})
    steam = FakeSteamClient()
    runner = _runner(store, steam)
    key = make_key(1)

    runner._set_result(key, code, "some message")

    assert store.updates[1]["redeem_status"] == expected_status
    assert store.updates[1]["last_result_code"] == code
    assert store.updates[1]["last_result_label"] == code_to_label(code)


def test_set_result_records_legacy_row_for_giveaway_tracking():
    store = FakeStore([], {})
    steam = FakeSteamClient()
    runner = _runner(store, steam)
    key = make_key(1, gamekey="gk1", human_name="Some Game",
                   redeemed_key_val="AAAAA-BBBBB-CCCCC")

    runner._set_result(key, 0, "Redeemed")

    assert store.legacy_rows == [("redeemed", "gk1", "Some Game", "AAAAA-BBBBB-CCCCC")]
