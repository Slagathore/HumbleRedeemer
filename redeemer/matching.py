"""Ownership matching between Humble entries and the owned Steam library.

Three tiers, most to least certain:
  1. owned_appid  — Humble's steam_app_id is in the owned library. Definitive.
  2. owned_name   — normalized names match exactly (case, punctuation,
                    trademark glyphs, 'and'/'&', unicode forms ignored).
  3. likely_owned — high fuzzy similarity; surfaced for review in the UI,
                    never silently skipped or silently redeemed.
Everything else is 'unowned' (a redemption candidate if it's a Steam key).
"""
import re
import unicodedata

from fuzzywuzzy import fuzz

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")
_EDITION = re.compile(
    r"\b(standard|deluxe|definitive|goty|game of the year|complete|remastered"
    r"|anniversary|enhanced|ultimate|gold|digital|special) edition\b"
)


def normalize(name):
    """Reduce a title to a comparable token string."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    # "the" at the front is inconsistent between stores
    if s.startswith("the "):
        s = s[4:]
    return s


def normalize_loose(name):
    """normalize() plus edition-suffix stripping — used only for fuzzy tier."""
    s = normalize(name)
    s = _EDITION.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


class Matcher:
    def __init__(self, owned_app_details, fuzzy_threshold=90):
        """owned_app_details: {appid: name}"""
        self.owned = owned_app_details
        self.threshold = int(fuzzy_threshold)
        self.by_norm = {}
        for appid, name in owned_app_details.items():
            self.by_norm.setdefault(normalize(name), (appid, name))
        self.loose_list = [
            (normalize_loose(name), appid, name)
            for appid, name in owned_app_details.items()
        ]

    def match(self, human_name, steam_app_id=None):
        """Returns (match_status, appid, matched_name, score)."""
        # Tier 1: appid
        if steam_app_id:
            try:
                appid = int(steam_app_id)
            except (TypeError, ValueError):
                appid = None
            if appid and appid in self.owned:
                return ("owned_appid", appid, self.owned[appid], 100)

        norm = normalize(human_name)
        if not norm:
            return ("unowned", None, "", 0)

        # Tier 2: exact normalized name
        hit = self.by_norm.get(norm)
        if hit:
            return ("owned_name", hit[0], hit[1], 100)

        # Tier 3: conservative fuzzy on edition-stripped names
        loose = normalize_loose(human_name)
        best = (0, None, "")
        for cand_norm, appid, name in self.loose_list:
            if not cand_norm:
                continue
            score = fuzz.token_sort_ratio(loose, cand_norm)
            if score > best[0]:
                best = (score, appid, name)
        if best[0] >= self.threshold:
            return ("likely_owned", best[1], best[2], best[0])

        return ("unowned", None, "", best[0])


def match_all(store, fuzzy_threshold=90, progress=None):
    """Re-match every Humble key against the synced Steam library.

    Manual review decisions (owned_manual / not_owned_manual) are preserved.
    Returns counts by resulting status.
    """
    owned = store.get_steam_library()
    matcher = Matcher(owned, fuzzy_threshold)
    keys = store.get_keys()
    counts = {}
    for i, key in enumerate(keys):
        if progress:
            progress(i + 1, len(keys), key["human_name"])
        if key["match_status"] in ("owned_manual", "not_owned_manual"):
            counts[key["match_status"]] = counts.get(key["match_status"], 0) + 1
            continue
        status, appid, name, score = matcher.match(
            key["human_name"], key["steam_app_id"]
        )
        counts[status] = counts.get(status, 0) + 1
        if (status != key["match_status"] or (appid or 0) != (key["match_appid"] or 0)
                or score != key["match_score"]):
            store.set_key_fields(
                key["id"],
                match_status=status,
                match_appid=appid,
                match_name=name,
                match_score=score,
            )
    return counts
