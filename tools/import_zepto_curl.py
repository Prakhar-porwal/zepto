"""Turn a 'Copy as cURL' of a Zepto search into zepto_profile.json.

    1. open zeptonow.com, set your delivery location, search for something
    2. DevTools > Network > the search XHR > right-click > Copy > Copy as cURL
    3. python tools/import_zepto_curl.py --query "amul milk" < capture.txt

The searched term, the capture's lat/lng and its store id are replaced with
{query}, {lat}, {lng} and {store_id} placeholders wherever they appear, so the
same request can then be replayed for any product at any location.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "zepto_profile.json"

# headers that pin the request to one capture and break the replay
_DROP_HEADERS = {"content-length", "host", "accept-encoding"}

_LAT_HEADERS = {"latitude", "lat", "x-latitude", "store_lat"}
_LNG_HEADERS = {"longitude", "lng", "long", "x-longitude", "store_lng"}
_STORE_HEADERS = {"store_id", "storeid", "store_ids", "storeids", "x-store-id"}


def parse_curl(text: str) -> dict:
    # DevTools wraps long commands; join them before lexing
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


def templatize(profile: dict, query: str, lat: str | None, lng: str | None, store_id: str | None) -> dict:
    """Replace the concrete values from the capture with placeholders."""
    subs = [(query, "{query}"), (lat, "{lat}"), (lng, "{lng}"), (store_id, "{store_id}")]
    subs = [(needle, token) for needle, token in subs if needle]

    def swap(text: str) -> str:
        for needle, token in subs:
            # whole-value or word-boundary match only, so a stray "12" inside an
            # id never gets clobbered by a latitude of 12
            text = re.sub(rf"(?<![\w.-]){re.escape(needle)}(?![\w.-])", token, text)
            text = text.replace(needle.replace(" ", "%20"), token)
            text = text.replace(needle.replace(" ", "+"), token)
        return text

    def swap_value(node):
        """Walk the parsed body: strings get a textual swap, numbers become the
        placeholder string (a bare {lat} in raw JSON would not parse)."""
        if isinstance(node, str):
            return swap(node)
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            for needle, token in subs:
                if str(node) == needle.strip() or _num_eq(node, needle):
                    return token
            return node
        if isinstance(node, dict):
            return {k: swap_value(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap_value(v) for v in node]
        return node

    split = urlsplit(profile["url"])
    params = dict(parse_qsl(split.query, keep_blank_values=True))
    profile["url"] = swap(urlunsplit((split.scheme, split.netloc, split.path, "", "")))
    profile["params"] = {k: swap(v) for k, v in params.items()} or None
    profile["headers"] = {k: swap(v) for k, v in profile["headers"].items()}

    body = profile.pop("body", None)
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise SystemExit("Request body is not JSON; this importer only handles JSON bodies.")
        profile["body"] = swap_value(parsed)
    else:
        profile["body"] = None
    return profile


def _num_eq(value, needle: str) -> bool:
    try:
        return float(value) == float(needle)
    except (TypeError, ValueError):
        return False


def sniff(headers: dict, names: set) -> str | None:
    for key, value in headers.items():
        if key.lower() in names and value.strip():
            return value.strip()
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", required=True, help="the product term you typed when capturing")
    ap.add_argument("--lat", help="capture latitude (default: sniffed from headers)")
    ap.add_argument("--lng", help="capture longitude (default: sniffed from headers)")
    ap.add_argument("--store-id", help="capture store id (default: sniffed from headers)")
    ap.add_argument("--price-scale", type=int, default=100, help="divide prices by this (paise=100)")
    ap.add_argument("--file", help="read the cURL from this file instead of stdin")
    args = ap.parse_args()

    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    profile = parse_curl(text)

    lat = args.lat or sniff(profile["headers"], _LAT_HEADERS)
    lng = args.lng or sniff(profile["headers"], _LNG_HEADERS)
    store_id = args.store_id or sniff(profile["headers"], _STORE_HEADERS)

    profile = templatize(profile, args.query, lat, lng, store_id)
    profile["price_scale"] = args.price_scale
    if store_id:
        # kept only for reference -- searches use each store's own id from
        # zepto_darkstores.csv, not the one this capture happened to use
        profile["captured_store_id"] = store_id

    _OUT.write_text(json.dumps(profile, indent=2) + "\n")

    found = [name for name, ok in
             (("{query}", True), ("{lat}", lat), ("{lng}", lng), ("{store_id}", store_id)) if ok]
    blob = json.dumps(profile)
    print(f"wrote {_OUT.relative_to(_ROOT)}")
    print(f"  {profile['method']} {profile['url']}")
    print(f"  headers: {len(profile['headers'])}, body: {'yes' if profile['body'] else 'no'}")
    for token in found:
        print(f"  {token}: {'placed' if token in blob else 'NOT FOUND in the request -- check the value'}")
    if "{lat}" not in blob:
        print("  note: no lat/lng in the request means every location would return the same "
              "store; pass --lat/--lng with the values you used, or capture again after "
              "setting the delivery address.")


if __name__ == "__main__":
    main()
