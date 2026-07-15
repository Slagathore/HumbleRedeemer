"""SQLite-backed state for the Humble Steam Key Redeemer.

One database (redeemer.db) is the single source of truth:
  - humble_keys   : every key-bearing entry found in your Humble library
  - steam_library : your owned Steam apps (synced via Web API)
  - events        : append-only audit log of everything the app does
  - legacy_status : rows imported from the old redeemed/already_owned/errored CSVs
  - settings      : key/value app settings
"""
import csv
import json
import os
import sqlite3
import threading
import time

DB_FILE = "redeemer.db"

DEFAULT_SETTINGS = {
    "steam_api_key": "",
    "steam_id_64": "",
    "fuzzy_threshold": "90",
    "per_key_delay": "1.5",
    "rate_limit_wait_min": "30",
    "auto_reveal": "1",
    "redeem_likely_owned": "0",
    # update notifications: "" = notify, "until_next" = quiet until a NEWER
    # push lands, "forever" = never notify (emergencies still break through)
    "update_silence": "",
    "update_silence_sha": "",
    # run in the system tray (Windows) — applied at next launch
    "tray": "1",
    # hide key values everywhere on screen (copy/email still work) so the
    # app can be shown on stream without giving keys away
    "streaming_mode": "0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS humble_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gamekey TEXT NOT NULL,
    machine_name TEXT NOT NULL DEFAULT '',
    keyindex INTEGER,
    human_name TEXT NOT NULL DEFAULT '',
    key_type TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    steam_app_id INTEGER,
    redeemed_key_val TEXT NOT NULL DEFAULT '',
    is_gift INTEGER NOT NULL DEFAULT 0,
    is_expired INTEGER NOT NULL DEFAULT 0,
    revealed INTEGER NOT NULL DEFAULT 0,
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    match_appid INTEGER,
    match_name TEXT NOT NULL DEFAULT '',
    match_score INTEGER NOT NULL DEFAULT 0,
    redeem_status TEXT NOT NULL DEFAULT '',
    last_result_code INTEGER,
    last_result_label TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(gamekey, machine_name)
);
CREATE TABLE IF NOT EXISTS steam_library (
    appid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    gamekey TEXT NOT NULL DEFAULT '',
    machine_name TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    result_code INTEGER,
    result_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS legacy_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gamekey TEXT NOT NULL DEFAULT '',
    human_name TEXT NOT NULL DEFAULT '',
    key_val TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    source_file TEXT NOT NULL DEFAULT '',
    UNIQUE(gamekey, human_name, key_val, status)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gog_library (
    product_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steam_licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    acquisition TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_keys_status ON humble_keys(redeem_status);
"""

LEGACY_FILES = {
    "redeemed": "redeemed.csv",
    "already_owned": "already_owned.csv",
    "errored": "errored.csv",
}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """Thread-safe wrapper around the SQLite database."""

    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            # migrations for databases created by earlier versions
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(humble_keys)")}
            if "steam_license" not in cols:
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN steam_license TEXT NOT NULL DEFAULT ''")
            if "given_away" not in cols:
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN given_away INTEGER NOT NULL DEFAULT 0")
            if "expires" not in cols:
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN expires TEXT NOT NULL DEFAULT ''")
            if "license_provenance" not in cols:
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN license_provenance TEXT NOT NULL DEFAULT ''")
            if "given_at" not in cols:
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN given_at TEXT NOT NULL DEFAULT ''")
            if "given_snapshot" not in cols:
                # the row's status frozen at the moment it was given away, so
                # a later dispute can show what we knew when we handed it out
                self._conn.execute(
                    "ALTER TABLE humble_keys ADD COLUMN given_snapshot TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    # ---------------- settings ----------------

    def get_settings(self):
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        settings = dict(DEFAULT_SETTINGS)
        settings.update({r["key"]: r["value"] for r in rows})
        return settings

    def set_settings(self, updates):
        with self._lock:
            for k, v in updates.items():
                self._conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )
            self._conn.commit()

    # ---------------- events ----------------

    def log_event(self, action, gamekey="", machine_name="", name="", detail="",
                  result_code=None, result_label="", ts=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts,action,gamekey,machine_name,name,detail,result_code,result_label) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts or now(), action, gamekey, machine_name, name, detail, result_code, result_label),
            )
            self._conn.commit()

    def get_events(self, limit=500):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def redemptions_by_day(self):
        """Successful redemptions per day (for the activity chart)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(ts,1,10) AS day, "
                "SUM(CASE WHEN result_label='success' THEN 1 ELSE 0 END) AS success, "
                "SUM(CASE WHEN result_label IN ('already_owned','duplicate_code_other_account') THEN 1 ELSE 0 END) AS owned, "
                "SUM(CASE WHEN result_label NOT IN ('success','already_owned','duplicate_code_other_account') THEN 1 ELSE 0 END) AS errored "
                "FROM events WHERE action IN ('redeem_steam','legacy') "
                "GROUP BY day ORDER BY day",
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- humble keys ----------------

    def upsert_humble_key(self, tpk):
        """Insert or refresh one Humble tpk dict (from the orders API)."""
        gamekey = tpk.get("gamekey", "") or ""
        machine_name = tpk.get("machine_name", "") or ""
        human_name = (tpk.get("human_name", "") or "").strip()
        key_type = tpk.get("key_type_human_name", "") or ""
        redeemed_key_val = str(tpk.get("redeemed_key_val", "") or "")
        steam_app_id = tpk.get("steam_app_id")
        is_gift = 1 if tpk.get("is_gift") else 0
        is_expired = 1 if tpk.get("is_expired") else 0
        keyindex = tpk.get("keyindex")
        revealed = 1 if redeemed_key_val else 0
        service = service_from_key_type(key_type)
        expires = str(tpk.get("expiration_date") or tpk.get("expiry_date") or "")
        ts = now()
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM humble_keys WHERE gamekey=? AND machine_name=?",
                (gamekey, machine_name),
            ).fetchone()
            if cur is None and machine_name:
                # Adopt a bootstrap row (imported from a master CSV, which had
                # no machine_name) so we don't duplicate it
                cur = self._conn.execute(
                    "SELECT id FROM humble_keys WHERE gamekey=? AND human_name=? "
                    "AND machine_name LIKE 'csv-import:%'",
                    (gamekey, human_name),
                ).fetchone()
                if cur is not None:
                    self._conn.execute(
                        "UPDATE humble_keys SET machine_name=? WHERE id=?",
                        (machine_name, cur["id"]),
                    )
            if cur is None:
                self._conn.execute(
                    "INSERT INTO humble_keys(gamekey,machine_name,keyindex,human_name,key_type,"
                    "service,steam_app_id,redeemed_key_val,is_gift,is_expired,revealed,expires,"
                    "first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gamekey, machine_name, keyindex, human_name, key_type, service,
                     steam_app_id, redeemed_key_val, is_gift, is_expired, revealed,
                     expires, ts, ts),
                )
                inserted = True
            else:
                # Never blank out a key we already captured
                self._conn.execute(
                    "UPDATE humble_keys SET keyindex=?, human_name=?, key_type=?, service=?, "
                    "steam_app_id=COALESCE(?, steam_app_id), "
                    "redeemed_key_val=CASE WHEN ?='' THEN redeemed_key_val ELSE ? END, "
                    "is_gift=?, is_expired=?, expires=?, "
                    "revealed=CASE WHEN ?='' AND redeemed_key_val!='' THEN 1 ELSE ? END, "
                    "updated_at=? WHERE id=?",
                    (keyindex, human_name, key_type, service, steam_app_id,
                     redeemed_key_val, redeemed_key_val, is_gift, is_expired, expires,
                     redeemed_key_val, revealed, ts, cur["id"]),
                )
                inserted = False
            self._conn.commit()
        return inserted

    def set_key_fields(self, key_id, **fields):
        if not fields:
            return
        fields["updated_at"] = now()
        # fields' keys become raw SQL column identifiers here (f-string, not
        # a placeholder). Safe today because every caller passes explicit
        # kwargs it wrote itself -- never unpack an external/user dict into
        # this call (**user_dict), that would reopen identifier injection.
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE humble_keys SET {cols} WHERE id=?",
                (*fields.values(), key_id),
            )
            self._conn.commit()

    def get_keys(self, where="", params=()):
        sql = "SELECT * FROM humble_keys"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY human_name COLLATE NOCASE"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_key(self, key_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM humble_keys WHERE id=?", (key_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------- steam library ----------------

    def replace_steam_library(self, owned_app_details):
        ts = now()
        with self._lock:
            self._conn.execute("DELETE FROM steam_library")
            self._conn.executemany(
                "INSERT OR REPLACE INTO steam_library(appid,name,synced_at) VALUES(?,?,?)",
                [(appid, name, ts) for appid, name in owned_app_details.items()],
            )
            self._conn.commit()

    def get_steam_library(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM steam_library").fetchall()
        return {r["appid"]: r["name"] for r in rows}

    def steam_library_count(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) c FROM steam_library").fetchone()
        return row["c"]

    def steam_library_synced_at(self):
        with self._lock:
            row = self._conn.execute("SELECT MAX(synced_at) t FROM steam_library").fetchone()
        return row["t"] or ""

    # ---------------- gog library ----------------

    def replace_gog_library(self, owned):
        ts = now()
        with self._lock:
            self._conn.execute("DELETE FROM gog_library")
            self._conn.executemany(
                "INSERT OR REPLACE INTO gog_library(product_id,name,synced_at) VALUES(?,?,?)",
                [(pid, name, ts) for pid, name in owned.items()])
            self._conn.commit()

    def get_gog_library(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM gog_library").fetchall()
        return {r["product_id"]: r["name"] for r in rows}

    # ---------------- giveaway ----------------

    # A key is a giftable spare when you own the game but the key itself was
    # never consumed: never redeemed here, and not burned by another account
    # (Steam result 15). Result 9 ("already owns") leaves the key valid.
    GIVEAWAY_WHERE = (
        "service='Steam' AND is_expired=0 "
        "AND redeem_status IN ('','already_owned','gift_link') "
        "AND (last_result_code IS NULL OR last_result_code != 15) "
        "AND (match_status IN ('owned_appid','owned_name','owned_manual') "
        "     OR redeem_status='already_owned')"
    )

    def get_giveaway_keys(self, include_given=False):
        where = self.GIVEAWAY_WHERE
        if not include_given:
            where += " AND given_away=0"
        return self.get_keys(where)

    def add_manual_keys(self, entries):
        """Insert user-supplied giveaway keys that never came from Humble.

        gamekey='manual' marks them (sync purges ignore that namespace),
        match_status='owned_manual' surfaces them as spares while keeping
        them out of the redeem pipeline, license_provenance='manual' keeps
        the provenance pass from trying to trace them.
        Returns (added, skipped) — skipped covers blanks and duplicate keys."""
        ts = now()
        added = skipped = 0
        with self._lock:
            for i, entry in enumerate(entries):
                name = (entry.get("name") or "").strip()
                val = (entry.get("key") or "").strip()
                if not name or not val:
                    skipped += 1
                    continue
                dupe = self._conn.execute(
                    "SELECT 1 FROM humble_keys WHERE redeemed_key_val=?",
                    (val,)).fetchone()
                if dupe:
                    skipped += 1
                    continue
                self._conn.execute(
                    "INSERT INTO humble_keys(gamekey,machine_name,human_name,key_type,"
                    "service,redeemed_key_val,revealed,match_status,license_provenance,"
                    "first_seen_at,updated_at) "
                    "VALUES('manual',?,?,'key','Steam',?,1,'owned_manual','manual',?,?)",
                    (f"manual:{ts}:{i}", name, val, ts, ts))
                added += 1
            self._conn.commit()
        return added, skipped

    def delete_manual_key(self, key_id):
        """Delete a row, but only if it was hand-entered — Humble-synced rows
        would just come back on the next sync and shouldn't be deletable."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM humble_keys WHERE id=? AND gamekey='manual'", (key_id,))
            self._conn.commit()
        return cur.rowcount

    # ---------------- steam licenses ----------------

    def replace_steam_licenses(self, licenses):
        ts = now()
        with self._lock:
            self._conn.execute("DELETE FROM steam_licenses")
            self._conn.executemany(
                "INSERT INTO steam_licenses(date,name,acquisition,synced_at) VALUES(?,?,?,?)",
                [(l["date"], l["name"], l["acquisition"], ts) for l in licenses],
            )
            self._conn.commit()

    def get_steam_licenses(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM steam_licenses").fetchall()
        return [dict(r) for r in rows]

    # ---------------- legacy CSV import ----------------

    def record_legacy_row(self, status, gamekey, human_name, key_val):
        """Pre-register a row the app itself appended to the legacy CSVs so the
        next boot's import doesn't re-ingest it as a duplicate 'legacy' event."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO legacy_status(gamekey,human_name,key_val,status,source_file) "
                "VALUES(?,?,?,?,?)",
                (gamekey, human_name.replace(",", "."), key_val, status, "app"),
            )
            self._conn.commit()

    def import_legacy_csvs(self, directory="."):
        """One-time import of the old per-run CSVs into legacy_status + events."""
        imported = 0
        for status, filename in LEGACY_FILES.items():
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                continue
            file_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
            label = {"redeemed": "success", "already_owned": "already_owned",
                     "errored": "error"}[status]
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 2:
                        continue
                    gamekey = row[0].strip()
                    human_name = row[1].strip()
                    key_val = row[2].strip() if len(row) > 2 else ""
                    if key_val.lower() in ("", "none"):
                        key_val = ""
                    with self._lock:
                        cur = self._conn.execute(
                            "INSERT OR IGNORE INTO legacy_status(gamekey,human_name,key_val,status,source_file) "
                            "VALUES(?,?,?,?,?)",
                            (gamekey, human_name, key_val, status, filename),
                        )
                        if cur.rowcount:
                            imported += 1
                            self._conn.execute(
                                "INSERT INTO events(ts,action,gamekey,name,detail,result_label) "
                                "VALUES(?,?,?,?,?,?)",
                                (file_ts, "legacy", gamekey, human_name,
                                 f"imported from {filename}", label),
                            )
                        self._conn.commit()
        return imported

    def apply_legacy_statuses(self):
        """Stamp redeem_status onto humble_keys from imported legacy rows.

        Match precedence: exact key value, then (gamekey + human_name).
        Never overwrites a status written by the new app.
        """
        applied = 0
        with self._lock:
            legacy = self._conn.execute("SELECT * FROM legacy_status").fetchall()
            for row in legacy:
                target = None
                if row["key_val"]:
                    target = self._conn.execute(
                        "SELECT id, redeem_status FROM humble_keys WHERE redeemed_key_val=?",
                        (row["key_val"],),
                    ).fetchone()
                if target is None:
                    target = self._conn.execute(
                        "SELECT id, redeem_status FROM humble_keys WHERE gamekey=? AND human_name=?",
                        (row["gamekey"], row["human_name"]),
                    ).fetchone()
                if target is None or target["redeem_status"]:
                    continue
                self._conn.execute(
                    "UPDATE humble_keys SET redeem_status=?, last_result_label=?, updated_at=? WHERE id=?",
                    (row["status"], "legacy_" + row["status"], now(), target["id"]),
                )
                applied += 1
            self._conn.commit()
        return applied

    # ---------------- bootstrap from master CSVs ----------------

    def bootstrap_from_master_csvs(self, directory="."):
        """Pre-populate an empty database from the newest master_*.csv files
        produced by the old pipeline, so the app has data before the first
        live sync. Bootstrap rows get a synthetic machine_name that a real
        Humble sync later adopts or purges."""
        import glob
        loaded = {"humble": 0, "steam": 0}
        with self._lock:
            have_keys = self._conn.execute(
                "SELECT COUNT(*) c FROM humble_keys").fetchone()["c"]
        if not have_keys:
            files = glob.glob(os.path.join(directory, "master_humble_keys_*.csv"))
            if files:
                path = max(files, key=os.path.getmtime)
                ts = now()
                with open(path, "r", encoding="utf-8-sig", newline="") as f, self._lock:
                    for i, row in enumerate(csv.DictReader(f)):
                        key_val = (row.get("key") or "").strip()
                        status = (row.get("status") or "").strip()
                        if status not in ("redeemed", "already_owned", "errored"):
                            status = ""
                        self._conn.execute(
                            "INSERT OR IGNORE INTO humble_keys(gamekey,machine_name,human_name,"
                            "service,redeemed_key_val,revealed,redeem_status,first_seen_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (row.get("id", ""), f"csv-import:{i}",
                             (row.get("name") or "").strip(), row.get("service", ""),
                             key_val, 1 if key_val else 0, status, ts, ts),
                        )
                        loaded["humble"] += 1
                    self._conn.commit()
        if self.steam_library_count() == 0:
            files = glob.glob(os.path.join(directory, "master_steam_library_*.csv"))
            if files:
                path = max(files, key=os.path.getmtime)
                owned = {}
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        try:
                            owned[int(row["id"])] = row.get("name", "")
                        except (ValueError, KeyError):
                            continue
                if owned:
                    self.replace_steam_library(owned)
                    loaded["steam"] = len(owned)
        return loaded

    def purge_bootstrap_rows(self):
        """Remove bootstrap rows a real Humble sync didn't claim."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM humble_keys WHERE machine_name LIKE 'csv-import:%'")
            self._conn.commit()
        return cur.rowcount

    # ---------------- metrics ----------------

    def metrics(self):
        q = lambda sql, p=(): self._conn.execute(sql, p).fetchone()[0]
        with self._lock:
            total = q("SELECT COUNT(*) FROM humble_keys")
            steam = q("SELECT COUNT(*) FROM humble_keys WHERE service='Steam'")
            revealed = q("SELECT COUNT(*) FROM humble_keys WHERE revealed=1")
            unrevealed_steam = q(
                "SELECT COUNT(*) FROM humble_keys WHERE revealed=0 AND service='Steam' AND is_expired=0")
            expired = q("SELECT COUNT(*) FROM humble_keys WHERE is_expired=1")
            gift = q("SELECT COUNT(*) FROM humble_keys WHERE is_gift=1")
            redeemed = q("SELECT COUNT(*) FROM humble_keys WHERE redeem_status='redeemed'")
            already_owned = q("SELECT COUNT(*) FROM humble_keys WHERE redeem_status='already_owned'")
            errored = q("SELECT COUNT(*) FROM humble_keys WHERE redeem_status='errored'")
            gift_links = q("SELECT COUNT(*) FROM humble_keys WHERE redeem_status='gift_link'")
            # only matches that haven't already been processed, so the
            # pipeline chart's segments stay mutually exclusive
            owned_match = q(
                "SELECT COUNT(*) FROM humble_keys WHERE redeem_status='' "
                "AND match_status IN ('owned_appid','owned_name','owned_manual')")
            likely = q("SELECT COUNT(*) FROM humble_keys WHERE match_status='likely_owned'")
            candidates = q(
                "SELECT COUNT(*) FROM humble_keys WHERE service='Steam' AND is_expired=0 AND is_gift=0 "
                "AND redeem_status='' AND match_status NOT IN ('owned_appid','owned_name','owned_manual','likely_owned')")
            spare_keys = q(
                f"SELECT COUNT(*) FROM humble_keys WHERE {self.GIVEAWAY_WHERE} AND given_away=0")
            spare_suspect = q(
                f"SELECT COUNT(*) FROM humble_keys WHERE {self.GIVEAWAY_WHERE} "
                "AND given_away=0 AND license_provenance='retail_suspect'")
            given_away = q("SELECT COUNT(*) FROM humble_keys WHERE given_away=1")
            license_verified = q(
                "SELECT COUNT(*) FROM humble_keys WHERE redeem_status='redeemed' AND steam_license!=''")
            license_count = q("SELECT COUNT(*) FROM steam_licenses")
            by_service = self._conn.execute(
                "SELECT service, COUNT(*) c FROM humble_keys GROUP BY service ORDER BY c DESC").fetchall()
            by_redeem = self._conn.execute(
                "SELECT redeem_status, COUNT(*) c FROM humble_keys WHERE redeem_status!='' GROUP BY redeem_status").fetchall()
        return {
            "total_keys": total,
            "steam_keys": steam,
            "revealed": revealed,
            "unrevealed_steam": unrevealed_steam,
            "expired": expired,
            "gift": gift,
            "gift_links": gift_links,
            "license_verified": license_verified,
            "license_count": license_count,
            "spare_keys": spare_keys,
            "spare_suspect": spare_suspect,
            "given_away": given_away,
            "redeemed": redeemed,
            "already_owned": already_owned,
            "errored": errored,
            "owned_match": owned_match,
            "likely_owned": likely,
            "candidates": candidates,
            "steam_library": self.steam_library_count(),
            "steam_synced_at": self.steam_library_synced_at(),
            "by_service": [{"service": r["service"] or "Unknown", "count": r["c"]} for r in by_service],
            "by_redeem_status": [{"status": r["redeem_status"], "count": r["c"]} for r in by_redeem],
        }


def service_from_key_type(key_type_human_name):
    if not key_type_human_name:
        return ""
    lowered = key_type_human_name.lower()
    if "steam" in lowered:
        return "Steam"
    if "epic" in lowered:
        return "Epic"
    if "origin" in lowered or lowered.startswith("ea"):
        return "EA/Origin"
    if "uplay" in lowered or "ubisoft" in lowered:
        return "Ubisoft"
    if "gog" in lowered:
        return "GOG"
    return key_type_human_name
