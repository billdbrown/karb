"""
Track (served at /karb) — a shared calorie/nutrition tracker for Emily & Bill.

A trimmed, AI-upgraded Cronometer: three energy rings, a handful of nutrients,
and two ways to skip typing macros by hand — describe a dish in plain language,
or photograph a nutrition-facts label (a Blue Apron card, a cereal box) and let
Claude read it. Runs as routes on the same server as the dock/kitchen (see
homedock_server.py's do_GET/do_POST for the /api/track/* wiring).

DESIGN NOTES (read before changing the data model)
  Shared vs per-user: `items` is a SHARED household library - either of you
  can log anything the other added. `profile` and `diary` are PER-USER -
  your targets and your log are your own.

  There used to be a Foods/Recipes split (then Foods/Meals), each split
  decided purely by which button was tapped (barcode/manual -> foods;
  Describe Food/Photo Log/Import URL -> recipes/meals), never by what the
  thing actually WAS - so a single scanned Snickers bar and a hand-typed
  "1 medium apple" (both one serving, no ingredient list) lived in a
  different bucket than a photographed Blue Apron dish for no principled
  reason. They're the same shape: a one-serving item is just a recipe with
  servings=1 and no ingredients. Now there's one table, `items`.

  Totals are for the WHOLE stored amount - `servings` servings, weighing
  `total_grams` (nullable) if known - not per serving. Logging a portion is
  then just arithmetic: by serving = totals * (qty / servings); by grams =
  totals * (qty / total_grams). A plain barcode-scanned food is the
  degenerate case: servings=1, total_grams=the serving weight, totals=that
  one serving's nutrition - `serving_desc` carries the human label (e.g.
  "1 bar") that a multi-serving dish doesn't need. `total_grams` is null
  when there's no gram data at all (e.g. most URL imports - schema.org
  nutrition doesn't include a dish's total weight), which falls back to
  serving-only logging. AI items (text or photo) are asked for total_grams
  directly, so both units work on those.

  Every diary row is a client-issued id, INSERT OR IGNORE'd - the *intent* is
  the same idempotent-write pattern the Kitchen/dock use elsewhere, so an
  offline queue can be layered on later without touching the schema.

  Nutrients are denormalized onto the diary row at log time (so editing an
  item later doesn't rewrite history), and barcode lookups (Open Food Facts)
  are cached into `items` so a repeat scan never re-hits the network.

  "Auth" is a name picker, not real auth - see AGENTS/PLAN notes. The frontend
  sends ?user=emily|bill (or a header) and the server trusts it. Fine for two
  people on a LAN; not what would ship to a public host.
"""
import base64
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Paths are env-overridable so this file stays byte-comparable with the copy
# still on Home Dock (LXC 106) while Karb lives on its own container. The
# defaults are Home Dock's, so an unconfigured run behaves exactly as before.
DATA = os.environ.get("KARB_DATA") or os.path.expanduser("~/track-data")
DB_PATH = os.path.join(DATA, "track.db")
IMAGES_DIR = os.path.join(DATA, "images")
SECRETS = os.environ.get("KARB_SECRETS") or os.path.expanduser("~/.homedock-secrets")
ANTHROPIC_KEY_FILE = os.path.join(SECRETS, "anthropic.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16.0 Safari/605.1.15")
FETCH_MAX = 3 * 1024 * 1024
API_TIMEOUT = 10
AI_TIMEOUT = 45          # vision/text generation is slower than a page fetch

MEALS = ("breakfast", "lunch", "dinner", "snacks")
NUTRIENTS = ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg", "sugar_g")

_lock = threading.Lock()

# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  sex TEXT NOT NULL DEFAULT 'f' CHECK (sex IN ('m','f')),
  age INTEGER,
  height_cm REAL,
  weight_kg REAL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  activity_mult REAL NOT NULL DEFAULT 1.2,
  bmr_override REAL,
  expenditure_override REAL,
  goal_deficit REAL NOT NULL DEFAULT 0,
  protein_target_g REAL,        -- null = auto (1.2 g/kg bodyweight) until the user picks a goal
  updated INTEGER
);

-- SHARED household library: either user can log anything in here. Totals are
-- for the WHOLE stored amount (`servings` servings), not per serving - see
-- module docstring.
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brand TEXT,
  barcode TEXT,
  serving_desc TEXT,             -- human label for one serving, e.g. "1 bar"; blank for a dish (shown as "N servings")
  servings REAL NOT NULL DEFAULT 1,   -- how many servings the totals below cover
  total_grams REAL,              -- total weight for `servings` worth; null = servings-only logging
  kcal REAL NOT NULL DEFAULT 0, protein_g REAL NOT NULL DEFAULT 0,
  carbs_g REAL NOT NULL DEFAULT 0, fat_g REAL NOT NULL DEFAULT 0,
  fiber_g REAL NOT NULL DEFAULT 0, sodium_mg REAL NOT NULL DEFAULT 0,
  sugar_g REAL NOT NULL DEFAULT 0,
  source TEXT,                   -- custom | off | ai-text | ai-photo | manual | <domain>
  source_url TEXT,
  image TEXT,                    -- 'images/<id>.<ext>', relative to IMAGES_DIR
  emoji TEXT,                    -- a fun, best-guess icon; shown when there's no image
  ingredients_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT,
  created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_barcode ON items(barcode);

-- PER-USER log. Nutrients + item_name are denormalized at write time, so
-- editing or deleting an item later never rewrites history.
CREATE TABLE IF NOT EXISTS diary (
  id TEXT PRIMARY KEY,          -- client-issued; INSERT OR IGNORE makes writes idempotent
  user_id TEXT NOT NULL,
  day TEXT NOT NULL,            -- 'YYYY-MM-DD', the CLIENT's local date - never derive server-side
  meal TEXT NOT NULL,
  item_type TEXT NOT NULL,      -- vestigial - historical rows may say food/recipe, new rows say 'item'; ids are globally unique so lookups never need it
  item_id TEXT,
  item_name TEXT NOT NULL,
  qty REAL NOT NULL,
  unit TEXT NOT NULL,           -- serving | gram
  kcal REAL NOT NULL DEFAULT 0, protein_g REAL NOT NULL DEFAULT 0,
  carbs_g REAL NOT NULL DEFAULT 0, fat_g REAL NOT NULL DEFAULT 0,
  fiber_g REAL NOT NULL DEFAULT 0, sodium_mg REAL NOT NULL DEFAULT 0,
  sugar_g REAL NOT NULL DEFAULT 0,
  note TEXT,
  created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diary_user_day ON diary(user_id, day);
"""


def db():
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Idempotent: safe to call on every server start."""
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    # One-time merge of the old foods/recipes split into the unified `items`
    # table (see module docstring). Both used secrets.token_hex(8) ids, so
    # there's no collision risk, and diary.item_id references stay valid
    # unchanged - only this migration + the DROPs ever need to run, guarded
    # by the old tables still existing so a crash mid-migration just retries
    # cleanly (INSERT OR IGNORE covers a partial insert from that retry).
    tables = {r["name"] for r in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "foods" in tables or "recipes" in tables:
        with _lock:
            if "foods" in tables:
                conn.execute(
                    """INSERT OR IGNORE INTO items
                       (id,name,brand,barcode,serving_desc,servings,total_grams,
                        kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,
                        source,emoji,created_by,created)
                       SELECT id,name,brand,barcode,serving_desc,1,serving_grams,
                        kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,
                        source,emoji,created_by,created FROM foods""")
                conn.execute("DROP TABLE foods")
            if "recipes" in tables:
                conn.execute(
                    """INSERT OR IGNORE INTO items
                       (id,name,total_grams,servings,source,source_url,image,emoji,
                        ingredients_json,created_by,created,
                        kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g)
                       SELECT id,title,total_grams,servings,source,source_url,image,emoji,
                        ingredients_json,created_by,created,
                        kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g FROM recipes""")
                conn.execute("DROP TABLE recipes")
            conn.commit()
    # The Custom Meals combo-builder and Repeats features were dropped from
    # the code earlier, but their tables were never physically removed -
    # clean those up too now that a migration pass is already happening here.
    for stmt in ("DROP TABLE IF EXISTS meals", "DROP TABLE IF EXISTS repeats"):
        conn.execute(stmt)
        conn.commit()
    # CREATE TABLE IF NOT EXISTS doesn't retroactively add a column to a
    # table that already existed - tolerate "already there".
    try:
        conn.execute("ALTER TABLE profile ADD COLUMN protein_target_g REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        now = int(time.time())
        with _lock:
            conn.executemany(
                "INSERT INTO users (id, display_name, sex, created) VALUES (?,?,?,?)",
                [("emily", "Emily", "f", now), ("bill", "Bill", "m", now)])
            conn.executemany(
                "INSERT INTO profile (user_id, activity_mult, goal_deficit, updated) "
                "VALUES (?,?,?,?)",
                [("emily", 1.375, 0, now), ("bill", 1.375, 0, now)])
            conn.commit()
    conn.close()


def require_user(uid):
    if not uid:
        raise ValueError("missing user")
    conn = db()
    row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        raise ValueError("unknown user")
    return uid


def list_users():
    conn = db()
    rows = conn.execute("SELECT id, display_name FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- targets
def bmr_mifflin(sex, age, height_cm, weight_kg):
    if not (age and height_cm and weight_kg):
        return None
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "m" else base - 161


def targets_get(user_id):
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    p = conn.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    bmr = (p["bmr_override"] if p and p["bmr_override"] else
           bmr_mifflin(u["sex"], u["age"], u["height_cm"], u["weight_kg"]))
    expenditure = (p["expenditure_override"] if p and p["expenditure_override"]
                   else (bmr * (p["activity_mult"] or 1.2) if bmr and p else None))
    deficit = (p["goal_deficit"] if p else 0) or 0
    target = (expenditure - deficit) if expenditure is not None else None
    # Protein is the one macro that's personal, not a flat RDA-style number -
    # a manual gram target if set, else a sensible auto-default scaled to
    # bodyweight (1.2 g/kg, a general "moderately active" baseline) so there's
    # still a reasonable bar to fill before anyone picks an explicit goal.
    protein_target = (p["protein_target_g"] if p and p["protein_target_g"]
                       else (u["weight_kg"] * 1.2 if u["weight_kg"] else 150))
    return {
        "user": dict(u), "profile": dict(p) if p else None,
        "bmr": round(bmr, 1) if bmr is not None else None,
        "expenditure": round(expenditure, 1) if expenditure is not None else None,
        "target": round(target, 1) if target is not None else None,
        "protein_target": round(protein_target, 1),
    }


def targets_save(user_id, body):
    now = int(time.time())
    conn = db()
    with _lock:
        conn.execute(
            "UPDATE users SET sex=?, age=?, height_cm=?, weight_kg=? WHERE id=?",
            (body.get("sex") or "f", body.get("age"), body.get("height_cm"),
             body.get("weight_kg"), user_id))
        conn.execute(
            """INSERT INTO profile (user_id, activity_mult, bmr_override,
               expenditure_override, goal_deficit, protein_target_g, updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 activity_mult=excluded.activity_mult, bmr_override=excluded.bmr_override,
                 expenditure_override=excluded.expenditure_override,
                 goal_deficit=excluded.goal_deficit,
                 protein_target_g=excluded.protein_target_g, updated=excluded.updated""",
            (user_id, body.get("activity_mult") or 1.2, body.get("bmr_override"),
             body.get("expenditure_override"), body.get("goal_deficit") or 0,
             body.get("protein_target_g"), now))
        conn.commit()
    conn.close()
    return targets_get(user_id)


# ---------------------------------------------------------------- diary
def _row_nutrients(row):
    return {k: row[k] for k in NUTRIENTS}


def _scaled(base, factor):
    return {k: round((base.get(k) or 0) * factor, 2) for k in NUTRIENTS}


def get_item(iid):
    conn = db()
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    conn.close()
    return row


def _resolve_item(item_id, qty, unit):
    """Returns (item_name, nutrients_dict) for a diary line, or raises ValueError."""
    qty = float(qty or 0)
    if qty <= 0:
        raise ValueError("amount must be greater than 0")

    it = get_item(item_id)
    if not it:
        raise ValueError("that item no longer exists")
    totals = _row_nutrients(it)
    if unit == "gram":
        if not it["total_grams"]:
            raise ValueError(
                "this item has no weight on file — log it by servings instead")
        factor = qty / it["total_grams"]
    else:
        factor = qty / (it["servings"] or 1)
    return it["name"], _scaled(totals, factor)


def diary_list(user_id, day):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM diary WHERE user_id=? AND day=? ORDER BY created",
        (user_id, day)).fetchall()
    conn.close()
    return {"day": day, "items": [dict(r) for r in rows]}


def diary_add(user_id, body):
    day = (body.get("day") or "").strip()
    meal = (body.get("meal") or "").strip().lower()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError("bad day (want the client's local YYYY-MM-DD)")
    if meal not in MEALS:
        raise ValueError("meal must be one of %s" % (MEALS,))
    name, nutrients = _resolve_item(body.get("item_id"), body.get("qty"), body.get("unit"))
    rid = body.get("id") or secrets.token_hex(8)
    now = int(time.time())
    conn = db()
    with _lock:
        conn.execute(
            """INSERT OR IGNORE INTO diary
               (id,user_id,day,meal,item_type,item_id,item_name,qty,unit,
                kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,note,created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, user_id, day, meal, "item", body.get("item_id"), name,
             float(body.get("qty")), body.get("unit"),
             nutrients["kcal"], nutrients["protein_g"], nutrients["carbs_g"],
             nutrients["fat_g"], nutrients["fiber_g"], nutrients["sodium_mg"],
             nutrients["sugar_g"], body.get("note"), now))
        conn.commit()
    row = conn.execute("SELECT * FROM diary WHERE id=?", (rid,)).fetchone()
    conn.close()
    return dict(row)


def diary_household(body):
    """Log the same item into BOTH diaries at once (e.g. a shared dinner)."""
    out = []
    for uid in ("emily", "bill"):
        entry = dict(body)
        entry.pop("id", None)          # each user gets their own row id
        entry["id"] = secrets.token_hex(8)
        out.append(diary_add(uid, entry))
    return {"items": out}


def diary_delete(user_id, entry_id):
    conn = db()
    with _lock:
        conn.execute("DELETE FROM diary WHERE id=? AND user_id=?", (entry_id, user_id))
        conn.commit()
    conn.close()
    return {"ok": True}


def diary_move(user_id, entry_id, meal):
    if meal not in MEALS:
        raise ValueError("bad meal")
    conn = db()
    with _lock:
        conn.execute("UPDATE diary SET meal=? WHERE id=? AND user_id=?",
                     (meal, entry_id, user_id))
        conn.commit()
    conn.close()
    return {"ok": True}


def summary(user_id, day):
    conn = db()
    rows = conn.execute("SELECT * FROM diary WHERE user_id=? AND day=?",
                        (user_id, day)).fetchall()
    conn.close()
    consumed = {k: 0.0 for k in NUTRIENTS}
    for r in rows:
        for k in NUTRIENTS:
            consumed[k] += r[k]
    consumed = {k: round(v, 1) for k, v in consumed.items()}
    t = targets_get(user_id)
    expenditure, target = t["expenditure"], t["target"]
    remaining = (target - consumed["kcal"]) if target is not None else None
    return {
        "day": day, "consumed": consumed,
        "bmr": t["bmr"], "expenditure": expenditure, "target": target,
        "protein_target": t["protein_target"],
        "remaining": round(remaining, 1) if remaining is not None else None,
    }


_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def calendar_range(user_id, start, end):
    """Per-day totals across a date window - one row per day that has entries.

    The calendar wants a month at a time. Calling summary() 31 times would give
    the same answer, at the cost of 31 round trips and 31 identical
    targets_get() calls for a number that cannot change between them. This is
    one GROUP BY and one target.

    Days with nothing logged are absent rather than zero-filled: "logged
    nothing" and "logged a 0 kcal day" are different facts, and only the client
    knows which days it is drawing.
    """
    if not (_YMD.match(start or "") and _YMD.match(end or "")):
        raise ValueError("from/to must be YYYY-MM-DD")
    if start > end:
        start, end = end, start
    conn = db()
    rows = conn.execute(
        "SELECT day, SUM(kcal) AS kcal, SUM(protein_g) AS protein_g, "
        "COUNT(*) AS entries FROM diary "
        "WHERE user_id=? AND day>=? AND day<=? GROUP BY day ORDER BY day",
        (user_id, start, end)).fetchall()
    conn.close()
    t = targets_get(user_id)
    return {
        "from": start, "to": end,
        "target": t["target"], "protein_target": t["protein_target"],
        "days": [{"day": r["day"],
                  "kcal": round(r["kcal"] or 0, 1),
                  "protein_g": round(r["protein_g"] or 0, 1),
                  "entries": r["entries"]} for r in rows],
    }


# ---------------------------------------------------------------- items
def _item_row(row):
    d = dict(row)
    d["ingredients"] = json.loads(d.pop("ingredients_json") or "[]")
    return d


def items_list(user_id=None):
    """The library is shared, but its ORDER is personal: each of you sees
    your own most-recently-logged items first (falling back to when an
    item was added, for anything you've never logged), rather than a single
    shared order that doesn't reflect either person's actual habits."""
    conn = db()
    if user_id:
        rows = conn.execute(
            """SELECT i.*, MAX(d.created) AS last_logged
               FROM items i
               LEFT JOIN diary d ON d.item_id = i.id AND d.user_id = ?
               GROUP BY i.id
               ORDER BY (last_logged IS NULL), last_logged DESC, i.created DESC""",
            (user_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM items ORDER BY created DESC").fetchall()
    conn.close()
    return [_item_row(r) for r in rows]


def item_get(iid):
    row = get_item(iid)
    if not row:
        return {"error": "not found"}
    return _item_row(row)


def items_save(body):
    """Create a new item, or edit an existing one. Only source == 'off'
    (Open Food Facts reference data) is locked - a fresh barcode scan would
    silently overwrite an edit anyway, so it'd just confuse the shared
    library. Everything else (custom, manual, AI, URL-imported) is freely
    editable. Ids come from the client on edit (the item's existing id)."""
    iid = body.get("id")
    existing = get_item(iid) if iid else None
    if existing and existing["source"] == "off":
        raise ValueError(
            "this one came from Open Food Facts, not something you entered — it can't be "
            "edited directly (a fresh scan would just overwrite your change)")
    iid = iid or secrets.token_hex(8)
    name = body.get("name") or "Untitled"
    # Picked once at creation, kept on edit - a name tweak doesn't need a new
    # icon, and re-picking on every save would mean a Claude call per edit.
    emoji = existing["emoji"] if existing else _pick_emoji(name)
    now = int(time.time())
    conn = db()
    with _lock:
        conn.execute(
            """INSERT INTO items (id,name,brand,barcode,serving_desc,servings,total_grams,
               kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,source,source_url,
               image,emoji,ingredients_json,created_by,created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, brand=excluded.brand, barcode=excluded.barcode,
                 serving_desc=excluded.serving_desc, servings=excluded.servings,
                 total_grams=excluded.total_grams, kcal=excluded.kcal,
                 protein_g=excluded.protein_g, carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,
                 fiber_g=excluded.fiber_g, sodium_mg=excluded.sodium_mg, sugar_g=excluded.sugar_g,
                 ingredients_json=excluded.ingredients_json""",
            (iid, name, body.get("brand"), body.get("barcode"), body.get("serving_desc"),
             body.get("servings") or 1, body.get("total_grams") or None,
             body.get("kcal") or 0, body.get("protein_g") or 0, body.get("carbs_g") or 0,
             body.get("fat_g") or 0, body.get("fiber_g") or 0, body.get("sodium_mg") or 0,
             body.get("sugar_g") or 0,
             existing["source"] if existing else (body.get("source") or "custom"),
             existing["source_url"] if existing else body.get("source_url"),
             existing["image"] if existing else None, emoji,
             json.dumps(body.get("ingredients") or (
                 json.loads(existing["ingredients_json"]) if existing else [])),
             body.get("user"), existing["created"] if existing else now))
        conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    conn.close()
    if existing and existing["name"] != row["name"]:
        _rename_in_diary(iid, row["name"])
    return _item_row(row)


def _rename_in_diary(item_id, new_name):
    """A rename is corrective, not a nutrition change - propagate the new
    display name to already-logged diary rows, but leave their frozen
    nutrients alone (editing macros later must NOT retroactively rewrite
    history, which is the whole reason diary rows are denormalized). Matched
    by item_id alone - ids are globally unique random tokens, so item_type
    isn't needed to disambiguate."""
    conn = db()
    with _lock:
        conn.execute("UPDATE diary SET item_name=? WHERE item_id=?", (new_name, item_id))
        conn.commit()
    conn.close()


def items_delete(iid):
    """Deletable regardless of source: diary rows already froze their own copy
    of the nutrients/name at log time, so removing an item from the shared
    library (even a scanned one) can't corrupt history - a later scan of the
    same barcode just re-fetches and re-caches it."""
    row = get_item(iid)
    conn = db()
    with _lock:
        conn.execute("DELETE FROM items WHERE id=?", (iid,))
        conn.commit()
    conn.close()
    if row and row["image"]:
        try:
            os.remove(os.path.join(DATA, row["image"]))
        except OSError:
            pass
    return {"ok": True}


def items_barcode(code):
    code = (code or "").strip()
    if not code:
        raise ValueError("no barcode")
    conn = db()
    row = conn.execute("SELECT * FROM items WHERE barcode=?", (code,)).fetchone()
    conn.close()
    fresh = off_lookup(code)
    if fresh:
        return _upsert_ref_item("off:%s" % code, fresh, code)
    if row:
        return _item_row(row)          # stale but better than nothing
    return {"error": "not_found"}


def _upsert_ref_item(iid, f, barcode=None):
    """Cache an Open Food Facts result (reference data - always refreshed on a
    fresh scan, unlike everything else). The emoji is picked once on first
    scan and kept thereafter, same reasoning as items_save."""
    existing = get_item(iid)
    emoji = existing["emoji"] if existing else _pick_emoji(f["name"])
    now = int(time.time())
    conn = db()
    with _lock:
        conn.execute(
            """INSERT INTO items (id,name,brand,barcode,serving_desc,servings,total_grams,
               kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,source,emoji,
               ingredients_json,created_by,created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, brand=excluded.brand, serving_desc=excluded.serving_desc,
                 total_grams=excluded.total_grams, kcal=excluded.kcal,
                 protein_g=excluded.protein_g, carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,
                 fiber_g=excluded.fiber_g, sodium_mg=excluded.sodium_mg, sugar_g=excluded.sugar_g""",
            (iid, f["name"], f.get("brand"), barcode, f["serving_desc"], 1, f["serving_grams"],
             f["kcal"], f["protein_g"], f["carbs_g"], f["fat_g"], f["fiber_g"],
             f["sodium_mg"], f["sugar_g"], f["source"], emoji, "[]", None, now))
        conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    conn.close()
    return _item_row(row)


# ---- Open Food Facts (no key) --------------------------------------------
def off_lookup(barcode):
    url = "https://world.openfoodfacts.org/api/v2/product/%s.json" % urllib.parse.quote(barcode)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = json.load(urllib.request.urlopen(req, timeout=API_TIMEOUT))
    except Exception:
        return None
    if data.get("status") != 1:
        return None
    p = data.get("product") or {}
    n = p.get("nutriments") or {}
    if not p.get("product_name"):
        return None
    # Prefer the product's own stated per-serving values when OFF has them -
    # a per-100g reference badly overstates a small-format item (a 65g bar
    # isn't "1 serving = 100g"; logging it as one silently inflates the
    # calories by however much smaller the real serving is). Only fall back
    # to the always-present per-100g figures when OFF has no serving data
    # for this product at all.
    serving_qty = n.get("energy-kcal_serving")
    if serving_qty is not None and p.get("serving_quantity"):
        grams = p["serving_quantity"]
        desc = p.get("serving_size") or ("%sg" % round(grams))
        suffix = "_serving"
    else:
        grams = 100
        desc = "100 g"
        suffix = "_100g"
    return {
        "name": p.get("product_name"), "brand": p.get("brands"),
        "serving_desc": desc, "serving_grams": grams, "source": "off",
        "kcal": n.get("energy-kcal" + suffix) or 0, "protein_g": n.get("proteins" + suffix) or 0,
        "carbs_g": n.get("carbohydrates" + suffix) or 0, "fat_g": n.get("fat" + suffix) or 0,
        "fiber_g": n.get("fiber" + suffix) or 0, "sugar_g": n.get("sugars" + suffix) or 0,
        # OFF reports sodium in grams; we store mg.
        "sodium_mg": round((n.get("sodium" + suffix) or 0) * 1000, 1),
    }


# ---------------------------------------------------------------- AI items
def _sum_ingredients(ingredients):
    """Sum a structured per-ingredient breakdown into whole-dish totals -
    done here in Python, not trusted to the model's own arithmetic, and
    impossible to disagree with the itemization by construction (unlike
    asking for an independent top-level total alongside the ingredient
    list, which could silently drift from what the ingredients actually
    add up to)."""
    total_grams = 0.0
    totals = {k: 0.0 for k in NUTRIENTS}
    for ing in ingredients:
        total_grams += ing.get("grams") or 0
        for k in NUTRIENTS:
            totals[k] += ing.get(k) or 0
    return total_grams, {k: round(v, 1) for k, v in totals.items()}


def _save_ai_item(result, source):
    iid = secrets.token_hex(8)
    name = result.get("title") or "Untitled"
    ingredients = result.get("ingredients") or []
    total_grams, totals = _sum_ingredients(ingredients)
    display_ingredients = [
        "%s (%sg)" % (ing["name"], round(ing["grams"])) if ing.get("grams") else ing.get("name")
        for ing in ingredients if ing.get("name")]
    now = int(time.time())
    conn = db()
    with _lock:
        conn.execute(
            """INSERT INTO items (id,name,servings,total_grams,source,emoji,created,
               kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,ingredients_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, name, result.get("servings") or 1, total_grams or None,
             source, _pick_emoji(name), now,
             totals["kcal"], totals["protein_g"], totals["carbs_g"], totals["fat_g"],
             totals["fiber_g"], totals["sodium_mg"], totals["sugar_g"],
             json.dumps(display_ingredients)))
        conn.commit()
    return item_get(iid)


INGREDIENT_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "grams": {"type": "number"},
        "kcal": {"type": "number"}, "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"}, "fat_g": {"type": "number"},
        "fiber_g": {"type": "number"}, "sodium_mg": {"type": "number"},
        "sugar_g": {"type": "number"},
    },
    "required": ["name", "grams", "kcal", "protein_g", "carbs_g", "fat_g",
                 "fiber_g", "sodium_mg", "sugar_g"],
    "additionalProperties": False,
}

ITEM_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "servings": {"type": "number"},
        "ingredients": {"type": "array", "items": INGREDIENT_AI_SCHEMA},
        "matched_item_id": {"type": ["string", "null"]},
        "matched_grams": {"type": ["number", "null"]},
    },
    "required": ["title", "servings", "ingredients", "matched_item_id", "matched_grams"],
    "additionalProperties": False,
}


def _existing_items_context():
    """A cheap (id: name)-only list of the library's gram-weighed items, so
    Describe Food/Photo Log can recognize "4 oz almond milk" as the SAME
    thing as an "8 oz almond milk" already on file (just a different
    amount) instead of creating a new item every time the wording/quantity
    varies - the way a repeated barcode scan already never duplicates."""
    conn = db()
    rows = conn.execute(
        "SELECT id, name FROM items WHERE total_grams IS NOT NULL ORDER BY name").fetchall()
    conn.close()
    if not rows:
        return ""
    lines = "\n".join("- %s: %s" % (r["id"], r["name"]) for r in rows)
    return (
        "\n\nHousehold's existing food library (id: name). ONLY set matched_item_id if this "
        "is unmistakably the exact same specific product as one of these - same brand, same "
        "specific item - not merely a similar type of food. Two different candy bars, two "
        "different frozen treats, two different brands of the same food, etc. are NOT a "
        "match just because they're in the same category - being 'a mini frozen dessert "
        "bar' is not enough, the actual product must be the same one. A wrong match here "
        "silently logs a completely different food's calories, which is far worse than "
        "creating an extra library entry, so when in doubt, leave matched_item_id null and "
        "estimate fresh instead. If it genuinely is the same product, matched_grams = your "
        "best estimate of the weight in grams of the amount just described.\n"
        + lines)


_MATCH_STOPWORDS = {"the", "a", "an", "with", "and", "of", "for", "in", "on", "bar", "bars",
                    "mini", "minis", "serving", "servings", "cal", "calorie", "calories"}


def _plausible_match(title, existing_name):
    """A cheap, independent sanity check on Claude's own proposed match -
    require some real shared vocabulary (a brand, a distinctive product
    word) between what it currently thinks this food is and the item it
    wants to match, so "both are a mini frozen dessert bar" can't silently
    substitute a completely different product's calories. This caught a
    real case: a Yasso mint-chocolate-chip bar matched to an existing
    "Snickers Minis Ice Cream Bar" on category resemblance alone. A
    rejected match just falls through to creating a new item - cheap
    insurance against the much costlier failure of logging the wrong food."""
    def words(s):
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
                if w not in _MATCH_STOPWORDS and len(w) > 2}
    return bool(words(title) & words(existing_name))


def _finish_ai_item(result, source):
    """Shared by items_ai_text/items_ai_photo: if Claude matched this to an
    existing item, log against it (no new row - the described amount is
    just this instance's serving) instead of creating a near-duplicate."""
    mid = result.get("matched_item_id")
    if mid:
        existing = get_item(mid)
        if existing and _plausible_match(result.get("title"), existing["name"]):
            it = _item_row(existing)
            it["matched_existing"] = True
            it["log_grams"] = result.get("matched_grams") or existing["total_grams"] or 100
            return it
    it = _save_ai_item(result, source)
    it["matched_existing"] = False
    it["log_grams"] = it["total_grams"] or 100
    return it


def anthropic_key():
    try:
        with open(ANTHROPIC_KEY_FILE) as f:
            return json.load(f)["api_key"]
    except Exception:
        return None


def _call_claude(content, schema):
    key = anthropic_key()
    if not key:
        raise ValueError(
            "AI features need an Anthropic API key — add "
            "%s with {\"api_key\": \"sk-ant-...\"}" % ANTHROPIC_KEY_FILE)
    body = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1536,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=AI_TIMEOUT)
        data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("Claude request failed (%d): %s" % (e.code, detail))
    except Exception as e:
        raise ValueError("could not reach Claude: %s" % e)
    try:
        return json.loads(data["content"][0]["text"])
    except Exception:
        raise ValueError("Claude returned something unparseable")


EMOJI_SCHEMA = {"type": "object", "properties": {"emoji": {"type": "string"}},
               "required": ["emoji"], "additionalProperties": False}


def _pick_emoji(name):
    """A fun, best-guess icon for a food's list thumbnail. Best-effort only -
    never blocks creating the food: falls back to a generic plate if no key
    is configured or the call fails for any reason."""
    try:
        result = _call_claude(
            "Reply with exactly one emoji that best represents this food: %s" % name,
            EMOJI_SCHEMA)
        e = (result.get("emoji") or "").strip()
        return e or "\U0001F37D"
    except Exception:
        return "\U0001F37D"


def items_ai_text(text, image_b64=None, media_type=None):
    """Text is the common case, but an optional photo can ride along - e.g. a
    screenshot of a recipe's ingredients/nutrition page, corrected by the
    text ("but I actually got tilapia instead of salmon"). Neither is
    required alone; at least one of the two must be given."""
    text = (text or "").strip()
    if not text and not image_b64:
        raise ValueError("describe the food, attach a photo, or both")
    prompt = (
        "You are a careful nutrition estimator. Given a description of a food, dish, or "
        "recipe (anything from a single piece of fruit to a full recipe with rough "
        "ingredient quantities), and/or a photo (a screenshot of a recipe/ingredients page, "
        "a nutrition label, or the food itself), break it down into its individual "
        "ingredients and estimate each one separately - do not jump straight to a total. "
        "For each ingredient, give its name, your best-estimate weight in grams for the "
        "quantity actually present (the whole batch/dish, not per serving), and that "
        "ingredient's own kcal/protein_g/carbs_g/fat_g/fiber_g/sugar_g/sodium_mg for that "
        "weight (not per 100g). The totals for the whole thing are computed automatically "
        "by summing your ingredient list, so the ingredient list IS the answer - list every "
        "meaningful component (protein, starch, vegetables, sauce, cheese, oil/butter used "
        "in cooking, etc.), not just the headline item. servings is how many servings that "
        "whole ingredient list should divide into (1 for a single item like one apple).\n\n"
        "If the text describes ANY substitution relative to the photo (a different "
        "ingredient, a different quantity): IGNORE the photo's printed Nutrition Facts "
        "numbers ENTIRELY - do not read them, do not use them as a starting point, do not "
        "let them anchor your answer in any way. A printed label is only accurate for the "
        "exact food it describes, and once that food changed, those numbers are simply "
        "wrong. Write the substituted ingredient as its own line using your own nutrition "
        "knowledge for what it ACTUALLY is, not what the label says. Even similar-seeming "
        "swaps can differ a lot for the same weight (e.g. salmon is a fatty fish at roughly "
        "13g fat/100g cooked, while tilapia is lean at roughly 3g fat/100g cooked) - a real "
        "substitution MUST change that ingredient's numbers from what the label says, never "
        "leave them matching. Use the photo only for what the text does NOT contradict: the "
        "unchanged ingredients, and their weights." +
        ("\n\nDescription:\n" + text if text else "\n\n(No text description - estimate from the photo alone.)")
        + _existing_items_context())
    if image_b64:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type or "image/jpeg",
                                         "data": image_b64}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt
    result = _call_claude(content, ITEM_AI_SCHEMA)
    return _finish_ai_item(result, "ai-text")


def items_ai_photo(image_b64, media_type):
    if not image_b64:
        raise ValueError("no image provided")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type or "image/jpeg",
                                     "data": image_b64}},
        {"type": "text", "text": (
            "First figure out what this photo shows, then estimate its nutrition by "
            "breaking it down into ingredients - do not jump straight to a total:\n\n"
            "- If it's a nutrition-facts label, meal-kit card (e.g. Blue Apron), or "
            "packaged-food label: read it carefully, including servings per container and "
            "amount per serving. The label's printed numbers are already precise, so don't "
            "guess sub-components that would only add noise - output ONE ingredient line "
            "representing the whole product, with grams = the full weight described (serving "
            "size times servings per container, or the stated net weight) and that line's "
            "own kcal/protein_g/etc = the label's totals for that same full weight.\n"
            "- If it's a photo of the food itself with no label (a plate of food, a piece of "
            "fruit, a home-cooked dish, a restaurant meal): visually identify each real "
            "component (protein, starch, vegetables, sauce, cheese, visible oil, etc.) and "
            "give each its own estimated weight in grams and its own nutrition for that "
            "weight, the same way you would from a written description.\n\n"
            "Either way, the totals for the whole thing are computed automatically by "
            "summing your ingredient list, so the ingredient list IS the answer. servings = "
            "servings per container for a label (use 1 for a single-serving item or a single "
            "dish/plate of food). Give it a short title - from the label/packaging if "
            "visible, or briefly describing the food if not (e.g. \"Grilled Chicken Salad\"); "
            "fall back to \"Photo Log\" only if you truly can't tell what it is."
            + _existing_items_context())},
    ]
    result = _call_claude(content, ITEM_AI_SCHEMA)
    return _finish_ai_item(result, "ai-photo")


# ---- URL import (schema.org/Recipe JSON-LD) - trimmed port of the Kitchen's
# parse_recipe/fetch_url_safe (homedock_server.py). Track doesn't need
# steps/course/cuisine, but DOES need whole-recipe nutrient totals, which
# schema.org nutrition is per-serving, so multiply by `servings` here.
class _RaiseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _host_is_public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def fetch_url_safe(url):
    """SSRF-guarded fetch of a user-supplied URL. Returns (content_type, bytes)."""
    opener = urllib.request.build_opener(_RaiseRedirect)
    cur = url
    for _hop in range(4):
        parts = urllib.parse.urlparse(cur)
        if parts.scheme not in ("http", "https"):
            raise ValueError("only http/https URLs are allowed")
        host = parts.hostname
        if not host or not _host_is_public(host):
            raise ValueError("that address isn't allowed")
        req = urllib.request.Request(cur, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            r = opener.open(req, timeout=API_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise ValueError("redirect without a location")
                cur = urllib.parse.urljoin(cur, loc)
                continue
            raise ValueError("the site returned HTTP %d" % e.code)
        except Exception:
            raise ValueError("could not reach that URL")
        data = r.read(FETCH_MAX + 1)
        if len(data) > FETCH_MAX:
            raise ValueError("that page is too large to import")
        return r.headers.get("Content-Type", ""), data
    raise ValueError("too many redirects")


_LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    re.S | re.I)


def _clean(s):
    import html as _html
    return _html.unescape(s).strip() if isinstance(s, str) else s


def _first(v):
    return (v[0] if v else None) if isinstance(v, list) else v


def _find_recipe_node(data):
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "@graph" in node and isinstance(node["@graph"], list):
                stack.extend(node["@graph"])
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and "Recipe" in x for x in types):
                if node.get("recipeIngredient") or node.get("recipeInstructions"):
                    return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None


def items_import_url(url):
    url = (url or "").strip()
    if not url:
        raise ValueError("no URL given")
    _ctype, raw = fetch_url_safe(url)
    page = raw.decode("utf-8", "replace")
    node = None
    for m in _LD_RE.finditer(page):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        node = _find_recipe_node(data)
        if node:
            break
    if not node:
        raise ValueError(
            "couldn't find recipe data on that page — try Describe Food instead")

    title = _clean(node.get("name")) or "Untitled"
    ry = node.get("recipeYield")
    if isinstance(ry, list):
        ry = ry[0] if ry else None
    servings = _num(ry) or 1
    ingredients = [_clean(x) for x in (node.get("recipeIngredient") or [])
                   if isinstance(x, str) and x.strip()]

    nutr = node.get("nutrition") or {}
    # schema.org nutrition is conventionally PER SERVING; we store WHOLE-RECIPE
    # totals, so scale by servings. Sites are inconsistent about this - treat
    # it as an estimate, same spirit as the rest of the hybrid nutrition system.
    per = {
        "kcal": _num(nutr.get("calories")) or 0,
        "protein_g": _num(nutr.get("proteinContent")) or 0,
        "carbs_g": _num(nutr.get("carbohydrateContent")) or 0,
        "fat_g": _num(nutr.get("fatContent")) or 0,
        "fiber_g": _num(nutr.get("fiberContent")) or 0,
        "sugar_g": _num(nutr.get("sugarContent")) or 0,
        "sodium_mg": _num(nutr.get("sodiumContent")) or 0,
    }
    totals = {k: round(v * servings, 1) for k, v in per.items()}

    iid = secrets.token_hex(8)
    now = int(time.time())
    emoji = _pick_emoji(title)
    conn = db()
    with _lock:
        conn.execute(
            """INSERT INTO items (id,name,total_grams,servings,source,source_url,image,
               emoji,created_by,created,kcal,protein_g,carbs_g,fat_g,fiber_g,sodium_mg,sugar_g,
               ingredients_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, title, None, servings,
             urllib.parse.urlparse(url).netloc.replace("www.", ""), url, None, emoji, None, now,
             totals["kcal"], totals["protein_g"], totals["carbs_g"], totals["fat_g"],
             totals["fiber_g"], totals["sodium_mg"], totals["sugar_g"], json.dumps(ingredients)))
        conn.commit()
    result = item_get(iid)
    if all(totals[k] == 0 for k in totals):
        result["warning"] = ("no nutrition info found on that page — edit the totals, or "
                             "delete and use Describe Food for an AI estimate")
    return result


def get_image_path(iid):
    row = get_item(iid)
    if not row or not row["image"]:
        return None
    path = os.path.join(DATA, row["image"])
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------- routing
def today_str():
    return time.strftime("%Y-%m-%d")


def handle_get(path, q):
    sub = path[len("/api/track"):] or "/"
    user = (q.get("user") or [None])[0]

    if sub == "/users":
        return {"items": list_users()}
    if sub == "/diary":
        return diary_list(require_user(user), (q.get("day") or [today_str()])[0])
    if sub == "/summary":
        return summary(require_user(user), (q.get("day") or [today_str()])[0])
    if sub == "/items":
        return {"items": items_list(user)}
    if sub == "/item":
        return item_get((q.get("id") or [""])[0])
    if sub == "/items/barcode":
        return items_barcode((q.get("code") or [""])[0])
    if sub == "/targets":
        return targets_get(require_user(user))
    if sub == "/calendar":
        return calendar_range(require_user(user),
                              (q.get("from") or [""])[0], (q.get("to") or [""])[0])
    raise ValueError("unknown route: " + path)


def handle_post(path, body):
    sub = path[len("/api/track"):] or "/"

    if sub == "/diary":
        return diary_add(require_user(body.get("user")), body)
    if sub == "/diary/delete":
        return diary_delete(require_user(body.get("user")), body.get("id"))
    if sub == "/diary/move":
        return diary_move(require_user(body.get("user")), body.get("id"), body.get("meal"))
    if sub == "/diary/household":
        return diary_household(body)
    if sub == "/items":
        return items_save(body)
    if sub == "/items/delete":
        return items_delete(body.get("id"))
    if sub == "/items/ai_text":
        return items_ai_text(body.get("text"), body.get("image_b64"), body.get("media_type"))
    if sub == "/items/ai_photo":
        return items_ai_photo(body.get("image_b64"), body.get("media_type"))
    if sub == "/items/import_url":
        return items_import_url(body.get("url"))
    if sub == "/targets":
        return targets_save(require_user(body.get("user")), body)
    raise ValueError("unknown route: " + path)
