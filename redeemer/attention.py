"""'Needs attention' report — everything that can't just be auto-redeemed.

Categorizes the odd corners of a Humble library: non-Steam store keys (with
where/how to redeem them), exhausted or errored keys with the failure decoded,
expired keys, gift links, and the not-actually-a-game stuff (trials,
subscriptions, playtests, coupons).
"""
import re

# Where and how to redeem each store's keys
STORE_INFO = {
    "GOG": {
        "url": "https://www.gog.com/redeem",
        "note": "Sign in with the GOG chip and use 'Redeem GOG keys' to automate, "
                "or paste the code at gog.com/redeem.",
    },
    "Epic": {
        "url": "https://store.epicgames.com/redeem",
        "note": "Sign in to Epic in your browser and paste the code. "
                "(Epic's login blocks automation, so this one's manual.)",
    },
    "EA/Origin": {
        "url": "https://help.ea.com/en/help/account/origin-code-redemption/",
        "note": "Redeem inside the EA app: EA app menu → Redeem Product Code.",
    },
    "Ubisoft": {
        "url": "https://www.ubisoft.com/help",
        "note": "Redeem inside Ubisoft Connect: top-left menu → Activate a key.",
    },
    "Blizzard": {
        "url": "https://account.battle.net/redeem",
        "note": "Sign in to Battle.net and paste the code.",
    },
    "ArenaNet": {
        "url": "https://account.arena.net/welcome",
        "note": "Guild Wars content — apply the code on your ArenaNet account page.",
    },
    "Wargaming.net": {
        "url": "https://na.wargaming.net/shop/redeem/",
        "note": "Region-specific — use the Wargaming portal for your region.",
    },
    "Perfect World": {
        "url": "https://www.arcgames.com/en/redeem",
        "note": "Redeem through Arc Games (Perfect World's launcher/site).",
    },
    "Bethesda.net": {
        "url": "https://bethesda.net/en/redeem-code",
        "note": "Bethesda.net codes may have migrated to Steam — check the entry "
                "on Humble for updated instructions.",
    },
}

FALLBACK_INFO = {
    "url": "https://www.humblebundle.com/home/keys",
    "note": "Not a standard store key — open your Humble keys page; the entry "
            "there has its own instructions or claim button.",
}

# Steam result codes that mean "this key is dead", decoded for humans
ERROR_DECODE = {
    15: ("Key exhausted", "Steam says this code was already activated by a "
         "DIFFERENT account. The key is used up — get a replacement from "
         "Humble support if you never shared it."),
    14: ("Invalid code", "Steam rejected the code as invalid. Re-check it on "
         "your Humble keys page; if it looks right, contact Humble support."),
    13: ("Region locked", "Not available for activation in your country. "
         "A VPN won't help — Steam ties activation to the account region."),
    24: ("Needs base game", "This is DLC — activate the base game first, then "
         "retry this key."),
    36: ("PS3 activation", "Requires playing on PS3 first (old promo keys)."),
    50: ("Steam Wallet code", "This is wallet money, not a game — redeem at "
         "store.steampowered.com/account/redeemwalletcode"),
    53: ("Rate limited", "Steam was rate-limiting when this was tried — reset "
         "its status and redeem again."),
}

NON_GAME_PATTERN = re.compile(
    r"trial|subscription|month|playtest|coupon|% off|discount|credits?\b|"
    r"wallpaper|soundtrack|sampler|dlc pack|beta\b|demo\b", re.I)


def _store_info(service):
    return STORE_INFO.get(service, FALLBACK_INFO)


def _looks_non_game(key):
    name = key["human_name"] or ""
    service = key["service"] or ""
    if NON_GAME_PATTERN.search(name):
        return True
    # a "service" that isn't a known store usually means coupon/subscription
    known_stores = set(STORE_INFO) | {"Steam", ""}
    return service not in known_stores


def build_report(store):
    """Returns {bucket: [entries]} — each key lands in exactly one bucket."""
    keys = store.get_keys()
    report = {
        "expired": [],       # key can never be redeemed
        "exhausted": [],     # code consumed elsewhere / invalid / region locked
        "retryable": [],     # errored but plausibly recoverable
        "gift_links": [],    # value is a link, not a key
        "non_game": [],      # trials, subs, coupons, playtests…
        "other_stores": [],  # real games on non-Steam stores
        "manual_choice": [], # Choice games Humble makes you claim on the site
    }

    for k in keys:
        entry = {
            "id": k["id"], "human_name": k["human_name"], "service": k["service"],
            "key": k["redeemed_key_val"], "revealed": k["revealed"],
            "expires": (k["expires"] or "")[:10],
            "redeem_status": k["redeem_status"],
            "last_result_code": k["last_result_code"],
            "note": "", "url": "", "given_away": k["given_away"],
        }

        if k["is_expired"]:
            entry["note"] = ("This key expired before it was revealed/redeemed. "
                             "Humble support occasionally replaces recent ones.")
            entry["url"] = "https://support.humblebundle.com/hc/en-us"
            report["expired"].append(entry)
            continue

        if k["service"] != "Steam":
            # non-Steam keys route by store regardless of old error status —
            # the fix is always "redeem it at its own store"
            info = _store_info(k["service"])
            entry["url"] = info["url"]
            entry["note"] = info["note"]
            if k["redeem_status"] == "errored":
                entry["note"] = ("An old run mistakenly tried this non-Steam key "
                                 "on Steam. ") + info["note"]
            if _looks_non_game(k):
                report["non_game"].append(entry)
            else:
                report["other_stores"].append(entry)
            continue

        if k["redeem_status"] == "errored":
            code = k["last_result_code"]
            if code in ERROR_DECODE:
                title, note = ERROR_DECODE[code]
                entry["note"] = f"{title}: {note}"
                bucket = "retryable" if code in (53, 24) else "exhausted"
            else:
                label = k["last_result_label"] or "unknown error"
                if "reveal" in label:
                    entry["note"] = ("Revealing this key on Humble failed — run "
                                     "Sync Humble and try again, or check the entry "
                                     "on your Humble keys page.")
                else:
                    entry["note"] = (f"Failed in an old run ({label}) with no "
                                     "recorded reason — reset its status and retry.")
                bucket = "retryable"
            entry["url"] = entry["url"] or "https://www.humblebundle.com/home/keys"
            report[bucket].append(entry)
            continue

        if k["redeem_status"] == "gift_link":
            entry["note"] = ("The value isn't a raw Steam key — usually a Humble "
                             "gift link. Open it to claim or forward it to someone.")
            entry["url"] = k["redeemed_key_val"] if str(
                k["redeemed_key_val"]).startswith("http") else \
                "https://www.humblebundle.com/home/keys"
            report["gift_links"].append(entry)
            continue

        if _looks_non_game(k) and not k["redeem_status"]:
            entry["note"] = ("Steam entry that doesn't look like a normal game "
                             "(playtest/demo/promo) — redeem normally or ignore.")
            entry["url"] = "https://store.steampowered.com/account/registerkey"
            report["non_game"].append(entry)

    # Choice games that must be claimed on the Humble site (from the audit log)
    seen = set()
    for e in store.get_events(limit=2000):
        if e["action"] == "choice_claim" and e["result_label"] == "manual_needed":
            ident = (e["name"], e["gamekey"])
            if ident in seen:
                continue
            seen.add(ident)
            month_slug = ""
            detail = e["detail"] or ""
            if ":" in detail:
                month_name = detail.split(":")[0]
                month_slug = month_name.lower().replace(" humble choice", "")\
                    .strip().replace(" ", "-")
            report["manual_choice"].append({
                "id": None, "human_name": e["name"], "service": "Humble Choice",
                "key": "", "revealed": 0, "expires": "",
                "redeem_status": "", "last_result_code": None,
                "given_away": 0,
                "note": f"Humble requires claiming this one on the site directly ({detail}).",
                "url": (f"https://www.humblebundle.com/subscription/{month_slug}"
                        if month_slug else "https://www.humblebundle.com/subscription/home"),
            })

    return report
