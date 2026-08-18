import asyncio
import re
import time

from curl_cffi.requests import AsyncSession

from clients.base import InventoryClient
from config import (
    ZEPTO,
    apply_zepto_tokens,
    resolve_store_for_pincode,
    zepto_has_refresh,
    zepto_profile,
    zepto_refresh_profile,
    zepto_session_status,
)

# Field names Zepto has used for the same concept across response versions.
# The parser below is deliberately shape-agnostic: it walks the whole payload
# looking for dicts that carry a name AND a price, rather than assuming a path,
# so a layout change reorders things instead of breaking the client.
_NAME_KEYS = ("name", "productName", "displayName", "title")
_BRAND_KEYS = ("brand", "brandName", "productBrand")
_ID_KEYS = ("productVariantId", "variantId", "skuId", "id", "productId")
_MRP_KEYS = ("mrp", "listingMrp", "maxRetailPrice", "strikeThroughPrice")
_PRICE_KEYS = ("sellingPrice", "discountedSellingPrice", "listingPrice", "price", "offerPrice")
_STOCK_KEYS = ("inStock", "available", "isAvailable", "inventory", "availableQuantity")
_QTY_KEYS = ("quantityDescription", "packsize", "packSize", "formattedPacksize", "unitOfMeasure")


# Zepto throttles on request rate, not just concurrency: even 3 in flight gets
# 429s without spacing, and the exact ceiling is not documented anywhere. So
# the interval self-tunes -- widen hard on a 429, drift back down while calls
# succeed -- rather than hard-coding a guess. Shared across client instances.
_MIN_INTERVAL_S = 0.6
_MAX_INTERVAL_S = 3.0
_interval = _MIN_INTERVAL_S
_rate_lock = asyncio.Lock()
_last_call = 0.0


def reset_pace() -> None:
    """Drop the rate penalty back to the floor. Called at the start of each
    search so one throttled run doesn't slow the next, and so the limiter can't
    ratchet itself into a multi-minute crawl."""
    global _interval
    _interval = _MIN_INTERVAL_S


# once we have been quiet this long, whatever tripped the limiter has aged out
_IDLE_RESET_S = 25.0


async def _pace() -> None:
    global _last_call, _interval
    async with _rate_lock:
        idle = time.monotonic() - _last_call
        if idle > _IDLE_RESET_S:
            # a penalty earned during a big city fan-out must not still be
            # slowing down a single-store search minutes later
            _interval = _MIN_INTERVAL_S
        wait = _interval - idle
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()
        # ease back off the brakes while things are going through
        _interval = max(_MIN_INTERVAL_S, _interval * 0.9)


def widen_interval() -> None:
    global _interval
    # gentler bump + a lower ceiling than before, and _pace() decays faster,
    # so a handful of 429s slow us down briefly instead of for the whole run
    _interval = min(_MAX_INTERVAL_S, _interval * 1.3)


# --- session renewal ------------------------------------------------------
# One renewal at a time across the whole process: a city-wide fan-out fires
# hundreds of searches, and they must not each kick off their own refresh.
_refresh_lock = asyncio.Lock()


def _find_token(node, keys: set):
    """Depth-first hunt for a token by any of several key names."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str) and value:
                return value
            found = _find_token(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_token(value, keys)
            if found:
                return found
    return None


def _extract_tokens(resp) -> tuple[str | None, str | None]:
    """New tokens can arrive as Set-Cookie, in the JSON body, or both."""
    access = refresh = None
    try:
        access = resp.cookies.get("accessToken")
        refresh = resp.cookies.get("refreshToken")
    except Exception:
        pass
    try:
        body = resp.json()
        access = access or _find_token(body, {"accessToken", "access_token", "authToken", "token"})
        refresh = refresh or _find_token(body, {"refreshToken", "refresh_token"})
    except Exception:
        pass
    return access, refresh


async def _do_refresh() -> None:
    profile = zepto_refresh_profile()
    if not profile:
        raise RuntimeError("no refresh request captured")
    async with AsyncSession(impersonate="chrome124") as session:
        resp = await session.request(
            profile.get("method", "POST"),
            profile["url"],
            headers=profile["headers"],
            params=profile.get("params") or None,
            json=profile.get("body") or None,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"refresh request returned HTTP {resp.status_code}")
    access, refresh = _extract_tokens(resp)
    if not access:
        raise RuntimeError("refresh response carried no access token")
    apply_zepto_tokens(access, refresh)


async def _ensure_session() -> None:
    """Make sure a live accessToken exists before searching, renewing from the
    captured refresh request when it has lapsed."""
    if zepto_session_status()["ready"]:
        return
    if not zepto_has_refresh():
        raise RuntimeError(zepto_session_status()["reason"])
    async with _refresh_lock:
        if zepto_session_status()["ready"]:   # another coroutine already renewed
            return
        try:
            await _do_refresh()
        except Exception as e:
            raise RuntimeError(
                f"Zepto auto-renewal failed ({str(e)[:80]}). Re-capture the refresh request "
                "with tools/import_zepto_refresh_curl.py, or the search request with "
                "tools/import_zepto_curl.py."
            ) from e
    if not zepto_session_status()["ready"]:
        raise RuntimeError("Zepto renewal did not yield a usable session; re-capture and retry.")


class ZeptoClient(InventoryClient):
    def __init__(self):
        # ZEPTO.base_url is empty on purpose: the captured URL is absolute and
        # gets passed to request() whole
        super().__init__(ZEPTO)
        # zepto_profile() raises with capture instructions if nothing exists;
        # an expired-but-renewable session is allowed through and handled per
        # search by _ensure_session()
        self.profile = zepto_profile()
        # Zepto quotes money in paise; the import tool records this so a change
        # of units is a config edit rather than a code change
        self.price_scale = self.profile.get("price_scale", 100)

    def on_throttle(self) -> None:
        widen_interval()

    def _fill(self, blob, **subs):
        """Substitute {query}/{lat}/{lng}/{store_id} through the captured
        request, at any depth, leaving everything else exactly as captured."""
        if isinstance(blob, str):
            # a value that is nothing but a placeholder keeps its native type,
            # so a JSON body captured with a numeric latitude stays numeric
            for key, value in subs.items():
                if blob == "{%s}" % key:
                    return value
            for key, value in subs.items():
                blob = blob.replace("{%s}" % key, str(value))
            return blob
        if isinstance(blob, dict):
            return {k: self._fill(v, **subs) for k, v in blob.items()}
        if isinstance(blob, list):
            return [self._fill(v, **subs) for v in blob]
        return blob

    async def search_at_store(self, query: str, store: dict, offset: int = 0) -> dict:
        """zepto_darkstores.csv carries Zepto's own store uuids, so each request
        targets a real store rather than relying on the captured one; the lat/lng
        go along for the ride because Zepto's headers expect them."""
        await _ensure_session()
        # reload after a possible renewal so we send the refreshed cookie
        profile = zepto_profile()
        subs = {
            "query": query,
            "lat": store["lat"],
            "lng": store["lng"],
            "store_id": store["id"],
            "offset": offset,
        }
        body = self._fill(profile.get("body") or None, **subs)
        # NB: Zepto's full-search mode ("SHOW_ALL_RESULTS", what returns actual
        # phones) validates a request-signature HMAC'd over the exact body bytes,
        # so it can't be replayed for an arbitrary query -- only the type-ahead
        # ("AUTOSUGGEST") mode, which ignores the signature, replays freely. We
        # stay on whatever mode the profile captured (AUTOSUGGEST).
        await _pace()
        try:
            return await self.request(
                self._fill(profile["url"], **subs),
                method=profile.get("method", "POST"),
                params=self._fill(profile.get("params") or None, **subs),
                json=body,
                # header values must be strings even where the body wants numbers
                headers={k: str(v) for k, v in self._fill(profile["headers"], **subs).items()},
                retries=5,
                retry_delay=2.0,
            )
        except Exception as e:
            # Zepto sits behind AWS WAF: once the captured token ages out the
            # edge answers with a challenge page or 429 instead of JSON, and the
            # raw decode error gives no hint about what to do
            raise RuntimeError(_explain(e)) from e

    async def search(self, query: str, pincode: str, offset: int = 0) -> dict:
        store = await resolve_store_for_pincode("zepto", pincode)
        raw = await self.search_at_store(query, store, offset)
        raw["_store"] = store
        return raw

    def parse_inventory(self, raw: dict) -> list[dict]:
        rows: list[dict] = []
        seen: set = set()
        self._walk(raw, rows, seen)
        for row in rows:
            row["mrp"] = self._money(row["mrp"])
            row["offer_price"] = self._money(row["offer_price"])
        return rows

    def _money(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            digits = re.sub(r"[^\d.]", "", value)
            return round(float(digits), 2) if digits else None
        return round(value / self.price_scale, 2)

    def _walk(self, node, rows: list[dict], seen: set) -> None:
        if isinstance(node, dict):
            row = self._as_product(node)
            if row is not None:
                key = row["sku_id"] or row["name"]
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            for value in node.values():
                self._walk(value, rows, seen)
        elif isinstance(node, list):
            for value in node:
                self._walk(value, rows, seen)

    @classmethod
    def _as_product(cls, node: dict) -> dict | None:
        # a product is anything naming itself and carrying a price; the price
        # may sit on the node or one level down in a product/variant sub-dict
        merged = dict(node)
        for nested in ("product", "productVariant", "variant", "productInfo", "meta"):
            child = node.get(nested)
            if isinstance(child, dict):
                merged = {**child, **merged}

        name = _first(merged, _NAME_KEYS, str)
        price = _first(merged, _PRICE_KEYS, (int, float, str))
        if not name or price is None:
            return None

        # Zepto sells several pack sizes under one product name; the table keys
        # rows by name, so without this a 200 ml and a 1 L tetra pack collapse
        # into one row showing whichever price came first
        qty = _first(merged, _QTY_KEYS, str) or ""
        if qty and qty not in name:
            name = f"{name} | {qty}"

        # outOfStock is the explicit signal; availableQuantity only backs it up,
        # since a stocked item can legitimately show a small count
        out_of_stock = merged.get("outOfStock")
        if isinstance(out_of_stock, bool):
            in_stock = not out_of_stock
        else:
            stock = _first(merged, _STOCK_KEYS, (bool, int))
            in_stock = bool(stock) if stock is not None else True
        return {
            "name": name,
            "brand": _first(merged, _BRAND_KEYS, str) or "",
            "sku_id": _first(merged, _ID_KEYS, str) or "",
            "in_stock": in_stock,
            "mrp": _first(merged, _MRP_KEYS, (int, float, str)),
            "offer_price": price,
            "quantity_description": qty,
            "category": _first(merged, ("category", "categoryName", "l1Category"), str) or "",
        }


_STALE_CAPTURE = (
    "Zepto rejected the replay ({detail}). Its edge is behind AWS WAF, so the "
    "captured token and cookies expire: re-capture the search request from a "
    "zeptonow.com tab and re-run tools/import_zepto_curl.py."
)


def _explain(error: Exception) -> str:
    detail = str(error)[:120] or type(error).__name__
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status in (202, 401, 403, 429) or "JSON" in type(error).__name__ or "Expecting value" in detail:
        return _STALE_CAPTURE.format(detail=f"HTTP {status}" if status else "non-JSON response")
    return f"Zepto request failed: {detail}"


def _first(node: dict, keys, types):
    for key in keys:
        if key in node and isinstance(node[key], types):
            value = node[key]
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None
