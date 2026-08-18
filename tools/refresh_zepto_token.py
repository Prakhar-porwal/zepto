"""Keep Zepto's session fresh without hourly copy-paste.

Zepto's accessToken lasts an hour and its edge blocks bots, so rather than forge
anything we let a real, logged-in Chrome hold the session and just copy its
current token into the app. Run this once to log in; after that a plain re-run
(or a cron entry) tops the token up in seconds.

    python tools/refresh_zepto_token.py

It reuses the search-request template already in zepto_profile.json (import a
search cURL once with import_zepto_curl.py first) and only swaps the token in.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

HARVESTER = ROOT / "tools" / "harvest_zepto_token.mjs"
LOCK = ROOT / ".zepto-refresh.lock"


def _find_node() -> str | None:
    # cron/launchd run with a bare PATH that omits Homebrew, so look there too
    found = shutil.which("node")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if Path(candidate).exists():
            return candidate
    return None


def _minutes_left() -> float | None:
    status = config.zepto_session_status()
    if status.get("expires_at") is None:
        return None
    return (status["expires_at"] - time.time()) / 60


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh Zepto's token from a logged-in Chrome.")
    ap.add_argument("--if-expiring", type=float, metavar="MIN",
                    help="only refresh when fewer than MIN minutes remain (for scheduled runs)")
    args = ap.parse_args()

    if not (ROOT / "zepto_profile.json").exists():
        print("No zepto_profile.json yet. Capture a search request once with "
              "tools/import_zepto_curl.py, then re-run this.")
        return 1

    # scheduled runs skip while the token is still healthy, so Chrome only opens
    # when it is actually needed
    if args.if_expiring is not None:
        left = _minutes_left()
        if left is not None and left > args.if_expiring:
            print(f"Token still good for ~{int(left)} min (> {args.if_expiring:g}); nothing to do.")
            return 0

    # never let two refreshes race for the same Chrome profile / debug port
    try:
        lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 300:
            print("Another refresh is already running; skipping.")
            return 0
        LOCK.unlink()  # stale lock from a crashed run
        lock_fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    node = _find_node()
    if node is None:
        os.close(lock_fd); LOCK.unlink()
        print("node is required to drive Chrome but was not found (looked on PATH and in Homebrew).")
        return 1

    print("Opening Chrome to read your live Zepto token (log in if the window asks)…")
    timed_out = False
    try:
        proc = subprocess.run([node, str(HARVESTER)], capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.close(lock_fd)
            LOCK.unlink()
        except (OSError, FileNotFoundError):
            pass
    if timed_out:
        print("Timed out waiting for a login. Re-run and log in in the Chrome window.")
        return 1

    if proc.returncode != 0:
        # harvester logs guidance to stderr
        sys.stderr.write(proc.stderr)
        return proc.returncode

    line = next((ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")), "")
    try:
        tokens = json.loads(line)
    except json.JSONDecodeError:
        print("Harvester returned no usable token. Log in at zepto.com and retry.")
        sys.stderr.write(proc.stderr)
        return 1

    config.apply_zepto_tokens(tokens.get("accessToken"), tokens.get("refreshToken"))
    status = config.zepto_session_status()
    if status["ready"]:
        mins = int((status["expires_at"] - time.time()) / 60)
        print(f"Zepto token refreshed — good for ~{mins} min. Searches work now without re-capturing.")
        return 0
    print("Applied a token but the session still reads as not-ready; try again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
