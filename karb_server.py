#!/usr/bin/env python3
"""Karb - HTTP front end for the nutrition tracker.

This is the small piece that used to be part of Home Dock's `homedock_server.py`
(LXC 106). Karb was never an iPad app - it uses const, arrow functions, template
literals, CSS grid and var(), and never touches XMLHttpRequest - so it broke
every Safari 9 rule the dock is built around while being served by the dock's
server. Splitting it out removes that contradiction rather than creating a
boundary; see wiki/decisions/2026-09-12-karb-migration.md in the vault.

All the actual logic still lives in `track_backend.py`, unchanged apart from
making its data and secrets paths env-overridable. This file only routes.

Two things it adds that Home Dock's server did not:

IDENTITY. On the LAN, Karb's "auth" was a name picker - the client sent
`?user=emily|bill` and the server believed it. That is fine for two people on a
private network and is exactly what must not survive contact with a public
hostname. In `access` mode this server ignores whatever the client claims and
takes identity from `Cf-Access-Authenticated-User-Email`, which Cloudflare
Access injects and which cannot be set by the caller, mapping it to a Karb user
id through users.json. `require_user()` in track_backend already rejects ids
that aren't in the users table, so an unrecognised email fails closed.

FAIL-CLOSED BY DEFAULT. `access` is the default mode, so a misconfigured or
half-configured deploy refuses every request rather than serving the food diary
to the internet. The old name-picker behaviour still exists for LAN testing but
has to be asked for explicitly with KARB_AUTH=picker, and says so loudly in the
log when it starts.

The reason that matters here specifically: `/api/track/items/ai_text` and
`ai_photo` spend the Anthropic key on every call. Unauthenticated, internet
-reachable, and billable is a bad combination to arrive at by accident.

Environment (see karb.env.example):
    KARB_AUTH      access (default) | picker
    KARB_USERS     path to the email -> user id map, JSON
    KARB_BIND      host:port to listen on, default 127.0.0.1:8080
    KARB_DATA      track-data directory        (read by track_backend)
    KARB_SECRETS   secrets directory           (read by track_backend)
"""

import json
import os
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import track_backend

ROOT = os.path.dirname(os.path.abspath(__file__))

AUTH_MODE = (os.environ.get("KARB_AUTH") or "access").strip().lower()
USERS_FILE = os.environ.get("KARB_USERS") or os.path.join(
    track_backend.SECRETS, "users.json")
BIND = os.environ.get("KARB_BIND") or "127.0.0.1:8080"

ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# POST is an allowlist, not a prefix match: every route here is a write or an
# outbound spend, so a typo'd path must 404 rather than fall through to
# track_backend and raise there.
TRACK_POSTS = ("/api/track/diary", "/api/track/diary/delete",
               "/api/track/diary/move", "/api/track/diary/household",
               "/api/track/items", "/api/track/items/delete",
               "/api/track/items/ai_text", "/api/track/items/ai_photo",
               "/api/track/items/import_url", "/api/track/targets")

IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif"}


def load_user_map():
    """email (lowercased) -> Karb user id. Reloaded per request on purpose.

    It is two lines of JSON and changes roughly never, but re-reading it means
    adding a person is editing a file, not editing a file and remembering to
    restart the service.
    """
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        sys.stderr.write("karb: cannot read %s: %s\n" % (USERS_FILE, e))
        return {}
    return {str(k).strip().lower(): str(v) for k, v in (raw or {}).items()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        # journald timestamps already; only log what isn't a 2xx.
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ---------------------------------------------------------
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Static files only. The API paths use _json (no-store) and images set
        # their own long cache, so this leaves both alone. `no-cache` means
        # "revalidate", not "don't store" - unchanged files still 304.
        if getattr(self, "_static", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _identity(self):
        """The Karb user id for this request, or None to refuse it.

        In access mode the client cannot influence this at all: the only input
        is a header Cloudflare sets after it has authenticated the person. A
        request that reaches the origin without it did not come through the
        tunnel, which is the case worth refusing loudest - it is what a direct
        hit on the origin's own LAN address looks like.
        """
        if AUTH_MODE == "picker":
            return None            # caller falls back to the client's claim
        email = (self.headers.get(ACCESS_EMAIL_HEADER) or "").strip().lower()
        if not email:
            return False           # distinct from None: refuse, don't fall back
        return load_user_map().get(email) or False

    def _apply_identity(self, q=None, body=None):
        """Force the request's user to the authenticated one.

        Returns True if the request may proceed. Overwrites rather than checks,
        so a client that lies about `user` is corrected rather than rejected -
        there is no version of this where the browser's claim is consulted.
        """
        uid = self._identity()
        if uid is None:
            return True            # picker mode: leave the client's value be
        if uid is False:
            self._json({"error": "not authenticated"}, 403)
            return False
        if q is not None:
            q["user"] = [uid]
        if body is not None:
            body["user"] = uid
        return True

    # ---- GET -------------------------------------------------------------
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)

        if p.path == "/healthz":
            # For the tunnel and for `systemctl status` at a glance. No identity
            # check: it reveals nothing and must answer before Access is wired.
            return self._json({"ok": True, "auth": AUTH_MODE})

        if p.path.startswith("/api/track/"):
            if not self._apply_identity(q=q):
                return
            if p.path == "/api/track/image":
                return self._track_image((q.get("id") or [""])[0])
            try:
                return self._json(track_backend.handle_get(p.path, q))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p.path.startswith("/api/"):
            return self.send_error(404)

        # Karb owns the root here - on Home Dock it was one app of four.
        if p.path in ("/", "/karb", "/karb/"):
            self.path = "/karb.html"
        self._static = True
        return super().do_GET()

    def _track_image(self, rid):
        path = track_backend.get_image_path(rid)
        if not path:
            return self.send_error(404)
        with open(path, "rb") as f:
            data = f.read()
        ext = path.rsplit(".", 1)[-1].lower()
        self.send_response(200)
        self.send_header("Content-Type",
                         IMAGE_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # ---- POST ------------------------------------------------------------
    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path not in TRACK_POSTS:
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(req, dict):
            return self._json({"error": "bad json"}, 400)
        if not self._apply_identity(body=req):
            return
        try:
            return self._json(track_backend.handle_post(p.path, req))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def main():
    track_backend.init_db()

    if AUTH_MODE not in ("access", "picker"):
        sys.exit("karb: KARB_AUTH must be 'access' or 'picker', got %r" % AUTH_MODE)

    if AUTH_MODE == "picker":
        sys.stderr.write(
            "karb: WARNING - KARB_AUTH=picker. Identity is whatever the browser "
            "claims. Safe on the LAN, never behind a public hostname.\n")
    else:
        n = len(load_user_map())
        if not n:
            # Not fatal: refusing every request is the correct failure, and it
            # is far easier to diagnose from a running service than from a unit
            # that won't start.
            sys.stderr.write(
                "karb: WARNING - no users in %s, so every request will be "
                "refused.\n" % USERS_FILE)
        else:
            sys.stderr.write("karb: access mode, %d mapped %s\n"
                             % (n, "email" if n == 1 else "emails"))

    host, _, port = BIND.rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    srv.daemon_threads = True
    sys.stderr.write("karb: serving %s on %s\n" % (ROOT, BIND))
    srv.serve_forever()


if __name__ == "__main__":
    main()
