import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from clients.base import close_session
from clients.instamart import InstamartClient
from clients.zepto import ZeptoClient
from config import (
    PLATFORMS,
    city_index,
    city_label,
    search_stores,
    stores_by_ids,
    stores_in_city,
    zepto_session_status,
    zepto_browser_ready,
    zepto_usable,
)

app = FastAPI(title="traker")
_STATIC = str(Path(__file__).resolve().parent / "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.on_event("shutdown")
async def shutdown():
    await close_session()


@app.get("/")
async def index():
    return FileResponse(f"{_STATIC}/index.html")


_CLIENTS = {"instamart": InstamartClient, "zepto": ZeptoClient}


def _client(platform: str):
    if platform not in _CLIENTS:
        raise HTTPException(status_code=400, detail=f"Unknown platform {platform!r}")
    try:
        return _CLIENTS[platform]()
    except KeyError as e:  # platform present but not yet configured
        raise HTTPException(status_code=400, detail=e.args[0])


@app.get("/api/platforms")
async def get_platforms():
    return {
        "platforms": [
            {
                "key": key,
                "label": cfg.label,
                # the UI greys out a platform until its session capture exists
                # -- and again once that capture goes stale
                # Zepto search is browser-driven now, so it's ready as long as the
                # browser profile has been logged in at least once
                "ready": True if key == "instamart" else zepto_browser_ready(),
                "reason": "" if (key == "instamart" or zepto_browser_ready())
                          else "Log into zepto.com once via tools/refresh_zepto_token.py.",
            }
            for key, cfg in PLATFORMS.items()
        ]
    }


@app.get("/api/cities")
async def get_cities(platform: str = "instamart"):
    cities = [
        {
            "key": key,
            "label": city["label"],
            "store_count": len(city["stores"]),
            "curated": city["curated"],
        }
        for key, city in city_index(platform).items()
    ]
    cities.sort(key=lambda c: (not c["curated"], -c["store_count"], c["label"]))
    return {"cities": cities}


def _store_summary(s: dict) -> dict:
    return {
        "id": s["id"],
        "locality": s["locality"],
        "city_label": s["city_label"],
        "distance_km": s["distance_km"],
        "nearest_metro": s["nearest_metro"],
        "lat": s.get("lat"),
        "lng": s.get("lng"),
    }


@app.get("/api/stores")
async def get_stores(q: str = Query(..., min_length=1), platform: str = "instamart"):
    return {"stores": [_store_summary(s) for s in search_stores(platform, q)]}


def _split_queries(raw: str) -> list[str]:
    """One search box, several products: 'amul milk, brown bread' runs both."""
    seen, queries = set(), []
    for part in raw.split(","):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            queries.append(part)
    if not queries:
        raise HTTPException(status_code=400, detail="No search query provided")
    return queries


# a multi-product search across a big city fans out to queries x stores calls,
# which needs a cap. Zepto's edge throttles far harder than Instamart's -- a
# 24-wide fan-out there gets almost every request 429'd -- so it gets its own,
# much smaller budget.
_CONCURRENCY = {"instamart": 24, "zepto": 3}
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _budget(client) -> asyncio.Semaphore:
    key = "zepto" if type(client).__name__ == "ZeptoClient" else "instamart"
    if key not in _SEMAPHORES:
        _SEMAPHORES[key] = asyncio.Semaphore(_CONCURRENCY[key])
    return _SEMAPHORES[key]


async def _fetch_one(client, query: str, pincode: str) -> dict:
    async with _budget(client):
        try:
            raw = await client.search(query, pincode=pincode)
        except (KeyError, RuntimeError) as e:
            return {"pincode": pincode, "items": [], "store": None, "error": str(e)}
    return {
        "pincode": pincode,
        "items": client.parse_inventory(raw),
        "store": raw.get("_store"),
        "error": None,
    }


async def _fetch_one_store(client, query: str, store: dict) -> dict:
    async with _budget(client):
        try:
            # Instamart keys off the store id, Zepto off the store's lat/lng --
            # each client takes what it needs from the same store record
            raw = await client.search_at_store(query, store)
        except RuntimeError as e:
            return {
                "store_id": store["id"],
                "locality": store["locality"],
                "city_label": store["city_label"],
                "distance_km": store["distance_km"],
                "nearest_metro": store.get("nearest_metro"),
                "lat": store.get("lat"),
                "lng": store.get("lng"),
                "items": [],
                "error": str(e),
            }
    return {
        "store_id": store["id"],
        "locality": store["locality"],
        "city_label": store["city_label"],
        "distance_km": store["distance_km"],
        "nearest_metro": store.get("nearest_metro"),
        "lat": store.get("lat"),
        "lng": store.get("lng"),
        "items": client.parse_inventory(raw),
        "error": None,
    }


async def _run_queries(client, queries: list[str], fetch, targets: list) -> list[dict]:
    """Every query against every target, all in flight at once (the semaphore
    above is what actually paces it), grouped back per query on the way out."""
    from clients import zepto as _z
    if type(client).__name__ == "ZeptoClient":
        _z.reset_pace()   # start each search at full speed, not last run's penalty
    jobs = [fetch(client, q, t) for q in queries for t in targets]
    flat = await asyncio.gather(*jobs)
    per_query = [flat[i * len(targets):(i + 1) * len(targets)] for i in range(len(queries))]
    return [{"query": q, "results": r} for q, r in zip(queries, per_query)]


# how many darkstores to actually hit per city. Instamart is fast, so it can
# afford a wide sweep; Zepto's rate limit means fewer, or a city takes minutes.
# tunable per host: serverless timeouts (Vercel Hobby = 10s) need a smaller fan-out.
# Set STORES_PER_CITY_INSTAMART=12 (etc.) in the host env to cap it.
_STORES_PER_CITY = {
    "instamart": int(os.environ.get("STORES_PER_CITY_INSTAMART", "40")),
    "zepto": int(os.environ.get("STORES_PER_CITY_ZEPTO", "15")),
}


def _sample_stores(stores: list, cap: int) -> list:
    """Evenly pick up to `cap` stores across the distance-sorted list, so the
    sample spreads over the whole city rather than clustering at the centre."""
    if len(stores) <= cap:
        return stores
    step = len(stores) / cap
    return [stores[int(i * step)] for i in range(cap)]


@app.get("/api/search")
async def search(query: str = Query(...), pincodes: str = Query(...), platform: str = "instamart"):
    pincode_list = [p.strip() for p in pincodes.split(",") if p.strip()]
    if not pincode_list:
        raise HTTPException(status_code=400, detail="No pincodes provided")
    return {"queries": await _run_queries(_client(platform), _split_queries(query), _fetch_one, pincode_list)}


@app.get("/api/search-city")
async def search_city(query: str = Query(...), cities: str = Query(...), platform: str = "instamart"):
    keys, seen = [], set()
    for key in (c.strip() for c in cities.split(",")):
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        raise HTTPException(status_code=400, detail="No cities provided")

    try:
        labels = [{"key": key, "label": city_label(platform, key)} for key in keys]
        # A big metro has ~100 darkstores; querying all of them against Zepto's
        # ~1 req/s limit takes minutes and makes an unreadable 100-column table.
        # Sample a spread across each city instead -- enough to see availability
        # and price spread without the wait. `total` lets the UI say what it did.
        stores, total = [], 0
        for key in keys:
            city_stores = stores_in_city(platform, key)
            total += len(city_stores)
            stores.extend(_sample_stores(city_stores, _STORES_PER_CITY[platform]))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    return {
        "cities": labels,
        "store_count": len(stores),
        "total_store_count": total,
        "queries": await _run_queries(_client(platform), _split_queries(query), _fetch_one_store, stores),
    }


@app.get("/api/search-stores")
async def search_at_stores(query: str = Query(...), store_ids: str = Query(...), platform: str = "instamart"):
    ids = [s.strip() for s in store_ids.split(",") if s.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="No stores selected")
    try:
        stores = stores_by_ids(platform, ids)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    return {
        "stores": [_store_summary(s) for s in stores],
        "queries": await _run_queries(_client(platform), _split_queries(query), _fetch_one_store, stores),
    }


# --- Zepto real search, via the logged-in browser -------------------------
# Zepto's full-search signs each request over the query, so it can't be curl'd
# for an arbitrary term. The browser signs it natively, so for Zepto we drive
# the real browser (tools/zepto_phone.mjs) and parse what it returns. This gives
# the true, correctly-ranked results (phones, groceries, anything) for the one
# store the login resolves to -- at the cost of ~15s per query and one location.
_ZEPTO_MJS = str(Path(__file__).resolve().parent / "tools" / "zepto_phone.mjs")
_zepto_browser_lock = asyncio.Lock()   # one browser/profile/debug-port at a time


async def _zepto_browser_search(query: str) -> tuple[list[dict], str | None]:
    async with _zepto_browser_lock:
        proc = await asyncio.create_subprocess_exec(
            "node", _ZEPTO_MJS, query,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return [], "Zepto browser search timed out."
    if proc.returncode != 0:
        return [], "Zepto search failed — is the .zepto-chrome profile logged in? Run tools/refresh_zepto_token.py."
    try:
        raw = json.loads(out.decode())
    except json.JSONDecodeError:
        return [], "Could not read Zepto's response."
    return ZeptoClient().parse_inventory(raw), None


@app.get("/api/zepto-search")
async def zepto_search(query: str = Query(...)):
    results_per_query = []
    for q in _split_queries(query):
        items, error = await _zepto_browser_search(q)
        results_per_query.append({
            "query": q,
            "results": [{
                "store_id": "zepto-browser",
                "locality": "Zepto",
                "city_label": "your delivery location",
                "distance_km": None,
                "nearest_metro": None,
                "items": items,
                "error": error,
            }],
        })
    return {
        "cities": [{"key": "zepto", "label": "Zepto"}],
        "store_count": 1,
        "total_store_count": 1,
        "queries": results_per_query,
    }
