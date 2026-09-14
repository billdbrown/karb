#!/usr/bin/env python3
"""Union-merge one Karb database into another.

    merge_db.py SOURCE DEST [--apply]

Dry run unless --apply is given.

This exists because Karb ran in two places at once during its move off Home
Dock, and will again briefly at the real cutover: both instances accept writes,
so neither database is a superset of the other.

The merge is exact rather than a judgment call, and that is a property of the
schema rather than luck:

  - Every id is `secrets.token_hex(8)`, so two instances cannot mint the same
    id for different rows. A row present in both is genuinely the same row.
  - Diary rows carry their nutrients denormalized at log time, so a diary row
    is self-contained. Moving one between databases cannot change what it says,
    even if the item it came from has since been edited or deleted.

So `users`, `items` and `diary` merge by INSERT OR IGNORE: anything the
destination already has wins, anything only the source has is added.

`profile` is the one table where that would be wrong. It is one row per user,
edited in place, so "already present" says nothing about which version is
current. It carries an `updated` timestamp, so the newer row wins outright.
A null `updated` is treated as older than any real timestamp.

What this does NOT merge is item images. `items.image` points at a file under
the data directory, and copying rows without copying bytes leaves a row whose
image 404s. The script reports any such rows rather than pretending; copy the
files across and re-run, or accept the missing thumbnails.
"""

import os
import sqlite3
import sys

ID_TABLES = ("users", "items", "diary")   # merge by INSERT OR IGNORE
COLS_CACHE = {}


def cols(conn, table):
    key = (id(conn), table)
    if key not in COLS_CACHE:
        COLS_CACHE[key] = [r[1] for r in
                           conn.execute("PRAGMA table_info(%s)" % table)]
    return COLS_CACHE[key]


def open_ro(path):
    if not os.path.exists(path):
        sys.exit("no such database: %s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_ = "--apply" in sys.argv[1:]
    if len(args) != 2:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    src_path, dst_path = args

    src = open_ro(src_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path)
    dst.row_factory = sqlite3.Row

    # Schema drift between the two copies would silently drop columns, so stop
    # rather than guess which side is right.
    for t in ID_TABLES + ("profile",):
        if cols(src, t) != cols(dst, t):
            sys.exit("schema mismatch in %s:\n  source: %s\n  dest:   %s"
                     % (t, cols(src, t), cols(dst, t)))

    print("%s  ->  %s%s" % (src_path, dst_path,
                            "" if apply_ else "   (DRY RUN)"))
    total = 0

    for t in ID_TABLES:
        c = cols(src, t)
        have = {r[0] for r in dst.execute("SELECT id FROM %s" % t)}
        new = [r for r in src.execute("SELECT * FROM %s" % t)
               if r["id"] not in have]
        print("  %-7s source %4d, dest %4d, to add %3d"
              % (t, src.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0],
                 len(have), len(new)))
        for r in new:
            print("      + %s" % (r["item_name"] if t == "diary" else
                                  r["name"] if t == "items" else r["id"]))
            if apply_:
                dst.execute("INSERT OR IGNORE INTO %s (%s) VALUES (%s)"
                            % (t, ",".join(c), ",".join("?" * len(c))),
                            tuple(r[x] for x in c))
        total += len(new)

    # profile: newest wins, per user.
    c = cols(src, "profile")
    dst_rows = {r["user_id"]: r for r in dst.execute("SELECT * FROM profile")}
    for r in src.execute("SELECT * FROM profile"):
        cur = dst_rows.get(r["user_id"])
        if cur is not None and (cur["updated"] or 0) >= (r["updated"] or 0):
            continue
        print("  profile %s: source is newer (%s > %s)"
              % (r["user_id"], r["updated"],
                 cur["updated"] if cur is not None else "absent"))
        if apply_:
            dst.execute("INSERT OR REPLACE INTO profile (%s) VALUES (%s)"
                        % (",".join(c), ",".join("?" * len(c))),
                        tuple(r[x] for x in c))
        total += 1

    # Images are files, not rows. Report rather than silently half-migrate.
    src_dir = os.path.join(os.path.dirname(os.path.abspath(src_path)), "images")
    dst_dir = os.path.join(os.path.dirname(os.path.abspath(dst_path)), "images")
    missing = []
    for r in dst.execute("SELECT id, image FROM items WHERE image IS NOT NULL"):
        name = os.path.basename(r["image"])
        if not os.path.exists(os.path.join(dst_dir, name)):
            missing.append((r["id"], name,
                            os.path.exists(os.path.join(src_dir, name))))
    if missing:
        print("  images missing from the destination:")
        for iid, name, in_src in missing:
            print("      %s  %s%s" % (iid, name,
                                      "  (present in source)" if in_src else ""))

    if apply_:
        dst.commit()
        print("applied: %d row(s)" % total)
        print("integrity:", dst.execute("PRAGMA integrity_check").fetchone()[0])
    else:
        print("would apply: %d row(s). Re-run with --apply." % total)
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
