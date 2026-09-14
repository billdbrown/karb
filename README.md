# Karb

A shared nutrition tracker for two people. A trimmed, AI-upgraded Cronometer: three energy rings,
a handful of nutrients, and two ways to avoid typing macros by hand — describe a dish in plain
language, or photograph a nutrition-facts label and let Claude read it. Barcodes are scanned
in-browser and looked up against Open Food Facts.

Written by Emily. Originally one page of Home Dock, a kitchen-iPad dashboard that is not public;
extracted to its own container and hostname in September 2026.

## Why it left Home Dock

Home Dock targets a 2012 iPad 4 running Mobile Safari 9, and enforces that: no `fetch()`, no CSS
grid, no `var()`, ES5 only, checked by a static verifier. `karb.html` uses 90 `const`, 33 arrow
functions, template literals, `display:grid` and no `XMLHttpRequest` anywhere. It broke every rule
of the server it was running on, and no dock page linked to it.

So the split removes a contradiction rather than drawing a new boundary — and because Safari 9
cannot negotiate modern TLS, it is also the only page on that server that *could* move behind a
Cloudflare hostname without the iPad losing it.

## Layout

    karb_server.py      HTTP routing, static files, and identity. ~230 lines.
    track_backend.py    Everything else — schema, diary, items, AI, Open Food Facts. ~1100 lines.
    karb.html           The whole client, one file.
    vendor/zxing.min.js Barcode decoding, bundled rather than CDN'd.
    karb.service        systemd unit.
    karb.env.example    Configuration, and the difference between the two deploy phases.

Python standard library only — no pip, no virtualenv, nothing to install. That was a constraint of
the 2 GB container it grew up on and has turned out to be worth keeping.

## Data

SQLite at `$KARB_DATA/track.db`, images beside it.

`items` is a **shared** household library — either person can log anything the other added — while
`profile` and `diary` are per-user. Three properties of the schema are load-bearing:

- **Totals are stored for the whole item, not per serving**, so logging a portion is arithmetic in
  either unit. `total_grams` is null when no gram data exists, which falls back to serving-only.
- **Nutrients are denormalized onto each diary row at log time**, so editing an item later does not
  rewrite history.
- **Every id is `secrets.token_hex(8)` and diary rows are `INSERT OR IGNORE`'d**, so writes are
  idempotent and ids never collide between two instances. An offline queue could be layered on
  without a schema change.

**Copy the database with `sqlite3 .backup`, never `cp`.** It runs in WAL mode and the WAL is
routinely larger than the main file, so a plain copy silently loses the most recent writes.

## Identity

Karb's original "auth" was a name picker: the client sent `?user=emily|bill` and the server
believed it. Correct for two people on a LAN, unshippable on a public hostname.

Behind Cloudflare Access the server ignores the client's claim entirely and reads
`Cf-Access-Authenticated-User-Email`, mapping it to a user id via `users.json`. Unknown email,
or no header at all, is a 403 — so a request that reaches the origin without passing through the
tunnel is refused rather than served.

`KARB_AUTH=access` is the **default**, so an unconfigured deploy fails closed. The name picker is
still available as `KARB_AUTH=picker` for LAN work, and logs a warning on every start.

This matters more than it looks: `/api/track/items/ai_text` and `ai_photo` spend an Anthropic key
per call, so "unauthenticated" and "internet-reachable" would also mean "billable".

## Deploying

    One LXC on the house network · Debian 13 · runs as `emily`

    /home/emily/karb            this repo
    /home/emily/karb-data       track.db + images   (the only writable path)
    /home/emily/.karb-secrets   anthropic.json, users.json   (700, files 600)
    /etc/karb.env               configuration

Secrets sit outside the project directory on purpose: the unit sets `ProtectHome=read-only` and
grants `ReadWritePaths` only to `karb-data`, so the server can read its keys and can never write
its own code.
