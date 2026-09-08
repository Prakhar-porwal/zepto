"""Search phones on Zepto for real, via the logged-in browser.

Zepto's full-search signs each request over the query, so curl can't replay it
for an arbitrary term -- but the browser can. This drives the browser to do the
search and parses the phones out of the response.

    python tools/zepto_phone.py "oppo k14x"

Returns the phones (name, price, in-stock) at the store your Zepto login
resolves to. Slower than a normal search (a browser has to run), and it covers
your one delivery location rather than a whole city.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clients.zepto import ZeptoClient  # noqa: E402

PHONE = re.compile(r"(\d+\s?GB\b)|(\d{3,5}\s?mAh)|(\bsmartphone\b)|(\biPhone\s?\d)", re.I)
ACCESSORY = re.compile(
    r"\b(case|cover|tempered|glass|charger|cable|adapter|protector|holder|stand|mount|"
    r"pouch|skin|earphone|headphone|earbud|power ?bank|otg|selfie|"
    r"memory card|pen ?drive|sd ?card|micro ?sd|ssd|hard ?disk|flash ?drive)\b", re.I)


def is_phone(name: str) -> bool:
    return bool(PHONE.search(name)) and not ACCESSORY.search(name)


def main() -> int:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print('usage: python tools/zepto_phone.py "<phone query>"')
        return 2

    print(f'Searching Zepto for "{query}" via the browser…', file=sys.stderr)
    try:
        proc = subprocess.run(
            ["node", str(ROOT / "tools" / "zepto_phone.mjs"), query],
            capture_output=True, text=True, timeout=90,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"browser search failed: {e}")
        return 1
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("could not read Zepto response (log in to zepto.com in the profile browser).")
        return 1

    items = ZeptoClient().parse_inventory(raw)
    phones = [i for i in items if is_phone(i["name"])]
    total = raw.get("totalProductCount")

    print(f'\n"{query}" on Zepto — {len(phones)} phone models '
          f'(of {len(items)} results shown, {total} total in catalogue):\n')
    for p in phones:
        stock = "in stock" if p["in_stock"] else "OUT OF STOCK"
        price = f"₹{p['offer_price']:,.0f}" if p["offer_price"] is not None else "?"
        print(f"  {p['name'][:60]:<60} {price:>10}  {stock}")
    if not phones:
        print("  (no phone models — Zepto may not stock this at your location)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
