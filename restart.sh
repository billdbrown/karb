#!/bin/sh
# Check, restart, confirm. Use this instead of `systemctl restart karb`.
#
# The unit is Restart=always and the hostname is on the public internet, so a
# saved syntax error is not a broken page - it is a crash loop behind
# karb.bill.computer. Both files are stdlib-only Python, which makes a syntax
# error the realistic failure and py_compile a complete check for it.
#
# Exits non-zero and prints the log if the service does not come back, so a
# bad deploy tells you rather than leaving you to notice.
set -e
cd "$(dirname "$0")"

# Emily runs this as herself and needs sudo; Bill reaches the box as root over
# SSH and does not. Calling sudo anyway breaks that second case, because sudo
# ties its cached credential to a terminal and a non-interactive ssh has none -
# so it prompts, finds no tty, and fails after the checks have already passed.
if [ "$(id -u)" = 0 ]; then SUDO=; else SUDO=sudo; fi

# The service's own configuration is the authority on where things live. Without
# this, running as root resolves ~ to /root and the checks below look for
# users.json somewhere it was never going to be - the same "as root, ~ is /root"
# trap that already cost this household a Google Health token once.
# set -a so the values reach the python below, not just this shell.
if [ -r /etc/karb.env ]; then set -a; . /etc/karb.env; set +a; fi

python3 -m py_compile karb_server.py track_backend.py merge_db.py
echo "syntax ok"

# users.json is read per request and a broken one fails every request closed,
# which looks like an auth problem rather than a typo.
python3 - <<'PY'
import json, os, sys
p = os.environ.get("KARB_USERS") or os.path.expanduser("~/.karb-secrets/users.json")
try:
    n = len(json.load(open(p)))
except FileNotFoundError:
    sys.exit("users.json missing at %s - every request would be refused" % p)
except ValueError as e:
    sys.exit("users.json is not valid JSON: %s" % e)
print("users.json ok, %d mapped" % n)
PY

$SUDO systemctl restart karb
sleep 2
if ! $SUDO systemctl is-active --quiet karb; then
  echo "karb did NOT come back:"
  $SUDO journalctl -u karb -n 20 --no-pager
  exit 1
fi
curl -fsS "http://${KARB_BIND:-127.0.0.1:8080}/healthz" && echo "  <- serving"
