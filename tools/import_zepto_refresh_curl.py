"""Turn a 'Copy as cURL' of Zepto's token-refresh call into zepto_refresh.json.

Once this exists, traker renews Zepto's hourly accessToken on its own -- you
only re-capture when the refresh token itself finally expires (days, not hours).

    1. open zepto.com logged in, then HARD-refresh the tab (Cmd/Ctrl-Shift-R)
    2. DevTools > Network > filter "auth" or "refresh" > find the auth-token /
       refresh-auth request > right-click > Copy > Copy as cURL
    3. python tools/import_zepto_refresh_curl.py --file refresh.txt

Nothing is templated: the request is replayed verbatim, and the rotating
refresh token in its cookie is rewritten in place after each successful renewal.
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "zepto_refresh.json"

_DROP_HEADERS = {"content-length", "host", "accept-encoding"}


def parse_curl(text: str) -> dict:
    text = text.replace("\\\n", " ").replace("^\n", " ").strip()
    if not text.startswith("curl"):
        raise SystemExit("That does not look like a cURL command.")

    tokens = shlex.split(text)
    url, method, headers, body = None, None, {}, None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in ("-X", "--request"):
            i += 1
            method = tokens[i]
        elif token in ("-H", "--header"):
            i += 1
            name, _, value = tokens[i].partition(":")
            if name.strip().lower() not in _DROP_HEADERS:
                headers[name.strip()] = value.strip()
        elif token in ("-d", "--data", "--data-raw", "--data-binary"):
            i += 1
            body = tokens[i]
        elif token in ("-b", "--cookie"):
            i += 1
            headers["cookie"] = tokens[i]
        elif not token.startswith("-") and url is None:
            url = token
        i += 1

    if url is None:
        raise SystemExit("No URL found in the cURL command.")
    return {"url": url, "method": method or ("POST" if body else "GET"), "headers": headers, "body": body}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="read the cURL from this file instead of stdin")
    args = ap.parse_args()

    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    profile = parse_curl(text)

    body = profile.pop("body", None)
    if body:
        try:
            profile["body"] = json.loads(body)
        except json.JSONDecodeError:
            raise SystemExit("Request body is not JSON; this importer only handles JSON bodies.")
    else:
        profile["body"] = None

    cookie = profile["headers"].get("cookie", "")
    has_refresh = "refreshToken=" in cookie
    _OUT.write_text(json.dumps(profile, indent=2) + "\n")

    print(f"wrote {_OUT.relative_to(_ROOT)}")
    print(f"  {profile['method']} {profile['url']}")
    print(f"  headers: {len(profile['headers'])}, body: {'yes' if profile['body'] else 'no'}")
    print(f"  refreshToken in cookie: {'yes' if has_refresh else 'NO -- this may be the wrong request'}")
    if "auth" not in profile["url"].lower() and "refresh" not in profile["url"].lower():
        print("  note: the URL mentions neither 'auth' nor 'refresh' -- double-check you copied "
              "the token-refresh call, not the search call.")


if __name__ == "__main__":
    main()
