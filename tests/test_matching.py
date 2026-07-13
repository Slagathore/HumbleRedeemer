"""Unit tests for redeemer.matching -- the ownership matching tiers.

This is one of the money paths the 2026-07 audit named: a normalize()
collision at tier 2 (owned_name) silently marks an unredeemed key as owned.
Once that happens, jobs._candidate_keys excludes it from redemption
candidates, so the key is never revealed or redeemed again. Tier 3
(likely_owned) threshold behavior is the other lever deciding whether a key
gets auto redeemed or held for manual review.
"""
from redeemer import matching


# ---------------- normalize() ----------------

def test_normalize_strips_case_punctuation_and_leading_the():
    assert matching.normalize("The Witcher 3: Wild Hunt") == "witcher 3 wild hunt"
    assert matching.normalize("BASTION!!!") == "bastion"
    assert matching.normalize("  Multiple   Spaces  ") == "multiple spaces"


def test_normalize_ampersand_and_equivalent_spelling():
    assert matching.normalize("Cook, Serve, Delicious! & Sons") == "cook serve delicious and sons"
    assert matching.normalize("Sword & Sworcery") == matching.normalize("Sword and Sworcery")


def test_normalize_empty_input_is_safe():
    assert matching.normalize("") == ""
    assert matching.normalize(None) == ""


def test_normalize_collision_between_punctuation_variants():
    """Two different raw strings that only differ in punctuation or case
    collapse to the same normalized key. That is the collision surface tier
    2 relies on being a genuine product name match, not just cosmetic
    noise."""
    a = matching.normalize("Sonic Adventure-2")
    b = matching.normalize("Sonic Adventure 2")
    assert a == b == "sonic adventure 2"


def test_normalize_collision_can_conflate_different_products():
    """normalize() reduces text; it has no concept of product identity. Two
    unrelated store listings that happen to share a display name normalize
    identically -- exactly the collision the audit named: tier 2
    (owned_name) cannot tell them apart from a real duplicate."""
    listing_a = "Rush"    # e.g. an indie platformer
    listing_b = "RUSH!!"  # e.g. an unrelated, differently branded game
    assert matching.normalize(listing_a) == matching.normalize(listing_b) == "rush"


# ---------------- Matcher.match ----------------

def test_match_tier1_appid_is_definitive_even_with_mismatched_name():
    owned = {440: "Team Fortress 2"}
    m = matching.Matcher(owned)
    status, appid, name, score = m.match("Some Renamed Bundle Listing", steam_app_id=440)
    assert (status, appid, name, score) == ("owned_appid", 440, "Team Fortress 2", 100)


def test_match_tier2_owned_name_fires_on_normalized_collision():
    """A Humble listing whose name normalizes identically to something
    already owned is reported owned_name with score 100, even though
    nothing (no appid) confirms it is actually the same product. This is
    the 'normalize() collision silently parks an unredeemed key as already
    owned' path the audit flagged."""
    owned = {100: "Rush"}
    m = matching.Matcher(owned)
    status, appid, name, score = m.match("RUSH!!", steam_app_id=None)
    assert status == "owned_name"
    assert appid == 100
    assert score == 100


def test_match_tier2_requires_normalized_equality_not_substring():
    owned = {100: "Rush"}
    m = matching.Matcher(owned)
    status, _, _, _ = m.match("Rush Hour", steam_app_id=None)
    assert status != "owned_name"  # a substring match is not equality


def test_match_bad_appid_falls_through_to_name_tiers_without_raising():
    owned = {440: "Team Fortress 2"}
    m = matching.Matcher(owned)
    # an appid that is not int-able must not crash the run -- it should
    # just fall through to the name-based tiers
    status, appid, name, score = m.match("Team Fortress 2", steam_app_id="not-a-number")
    assert status == "owned_name"
    assert appid == 440


def test_match_tier3_fuzzy_respects_threshold_boundary():
    owned = {200: "Foo Bar Baz Quux"}
    m = matching.Matcher(owned, fuzzy_threshold=90)
    # one character short of the owned title -- fuzzy score is high (>=90)
    status_close, appid_close, _, score_close = m.match("Foo Bar Baz Quu")
    # unrelated title -- fuzzy score is low (<90)
    status_far, appid_far, _, score_far = m.match("Completely Different Title")

    assert score_close >= 90
    assert status_close == "likely_owned"
    assert appid_close == 200

    assert score_far < 90
    assert status_far == "unowned"
    assert appid_far is None


def test_match_tier3_edition_suffix_is_stripped_before_fuzzy_compare():
    owned = {300: "Great Game"}
    m = matching.Matcher(owned, fuzzy_threshold=90)
    status, appid, _, score = m.match("Great Game Deluxe Edition")
    assert status == "likely_owned"
    assert appid == 300
    assert score == 100


def test_match_unowned_when_nothing_clears_any_tier():
    owned = {400: "Totally Unrelated Game"}
    m = matching.Matcher(owned, fuzzy_threshold=90)
    status, appid, _, _ = m.match("Nothing Like It At All")
    assert status == "unowned"
    assert appid is None


def test_match_empty_name_is_unowned_and_does_not_raise():
    owned = {400: "Some Game"}
    m = matching.Matcher(owned, fuzzy_threshold=90)
    status, appid, _, score = m.match("", steam_app_id=None)
    assert status == "unowned"
    assert appid is None
    assert score == 0


# ---------------- match_all: the collision reaching storage ----------------

class _FakeStore:
    """Minimal stand-in for redeemer.store.Store -- just the surface
    match_all touches. No real database, no file I/O."""

    def __init__(self, owned_library, keys):
        self._owned = owned_library
        self._keys = keys
        self.updates = {}  # key_id -> fields written by set_key_fields

    def get_steam_library(self):
        return self._owned

    def get_keys(self):
        return self._keys

    def set_key_fields(self, key_id, **fields):
        self.updates[key_id] = fields


def test_match_all_parks_colliding_key_as_owned_name():
    """End to end through match_all: a Humble key whose name collides after
    normalize() with an owned Steam title gets written to storage as
    match_status='owned_name'. Downstream, jobs._candidate_keys excludes
    owned_name from redemption candidates, so this key is now silently
    parked and will never be revealed or redeemed -- the failure mode the
    audit named."""
    owned = {100: "Rush"}
    keys = [{
        "id": 1, "human_name": "RUSH!!", "steam_app_id": None,
        "match_status": "unmatched", "match_appid": None, "match_score": 0,
    }]
    store = _FakeStore(owned, keys)

    counts = matching.match_all(store, fuzzy_threshold=90)

    assert counts.get("owned_name") == 1
    assert store.updates[1]["match_status"] == "owned_name"
    assert store.updates[1]["match_appid"] == 100


def test_match_all_preserves_manual_review_decisions():
    """A key the user already hand classified must never be silently
    reclassified by a later match_all run."""
    owned = {}
    keys = [{
        "id": 5, "human_name": "Anything", "steam_app_id": None,
        "match_status": "owned_manual", "match_appid": None, "match_score": 0,
    }]
    store = _FakeStore(owned, keys)

    counts = matching.match_all(store, fuzzy_threshold=90)

    assert counts.get("owned_manual") == 1
    assert 5 not in store.updates


def test_match_all_skips_write_when_result_is_unchanged():
    """No pointless writes when re-running match_all doesn't change anything
    -- also pins that set_key_fields is only called on an actual change."""
    owned = {100: "Rush"}
    keys = [{
        "id": 1, "human_name": "Rush", "steam_app_id": None,
        "match_status": "owned_name", "match_appid": 100, "match_score": 100,
    }]
    store = _FakeStore(owned, keys)

    matching.match_all(store, fuzzy_threshold=90)

    assert 1 not in store.updates
