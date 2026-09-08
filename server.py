import asyncio
import json
import os
import re
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


# Zepto/Instamart match literally, so common phone-brand shorthand finds nothing
# ("iq z11x" != "iQOO Z11x"). Expand a few unambiguous whole-word aliases so the
# name people actually type resolves to the catalogue name.
_QUERY_ALIASES = {"iq": "iqoo"}
_ALIAS_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _QUERY_ALIASES) + r")\b", re.I)


def _apply_aliases(q: str) -> str:
    return _ALIAS_RE.sub(lambda m: _QUERY_ALIASES[m.group(0).lower()], q)


def _split_queries(raw: str) -> list[str]:
    """One search box, several products: 'amul milk, brown bread' runs both."""
    seen, queries = set(), []
    for part in raw.split(","):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            queries.append(_apply_aliases(part))
    if not queries:
        raise HTTPException(status_code=400, detail="No search query provided")
    return queries


# a multi-product search across a big city fans out to queries x stores calls,
# which needs a cap. Zepto's edge throttles far harder than Instamart's -- a
# 24-wide fan-out there gets almost every request 429'd -- so it gets its own,
# much smaller budget.
_CONCURRENCY = {"instamart": 24, "zepto": 1}
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
# 0 = search every store in the city (no sampling). Override with the env vars
# to cap the fan-out if a host has a timeout.
_STORES_PER_CITY = {
    "instamart": int(os.environ.get("STORES_PER_CITY_INSTAMART", "0")),   # 0 = all stores
    # Zepto's guest session is WAF-limited to ~25 requests before it's blocked,
    # so a whole big city (93+ stores) can't be swept -- sample a spread instead.
    "zepto": int(os.environ.get("STORES_PER_CITY_ZEPTO", "25")),  # WAF rolling-limit sweet spot
}


def _sample_stores(stores: list, cap: int) -> list:
    """Evenly pick up to `cap` stores across the distance-sorted list, so the
    sample spreads over the whole city rather than clustering at the centre.
    cap <= 0 means take every store."""
    if cap <= 0 or len(stores) <= cap:
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
async def search_city(query: str = Query(...), cities: str = Query(...),
                      platform: str = "instamart", group: int = -1):
    keys, seen = [], set()
    for key in (c.strip() for c in cities.split(",")):
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        raise HTTPException(status_code=400, detail="No cities provided")

    cap = _STORES_PER_CITY[platform]
    try:
        labels = [{"key": key, "label": city_label(platform, key)} for key in keys]
        all_stores_ = [s for key in keys for s in stores_in_city(platform, key)]
        total = len(all_stores_)
        # When a city has more stores than one WAF-safe batch, split it into
        # numbered parts and let the caller search one part at a time. `group`
        # picks the part (0-indexed); each part is a contiguous slice of the
        # distance-sorted list, so Part A is the nearest ring, B the next, etc.
        if cap > 0 and total > cap:
            group_count = (total + cap - 1) // cap
            g = 0 if group < 0 else max(0, min(group, group_count - 1))
            stores = all_stores_[g * cap:(g + 1) * cap]
        else:
            group_count, g, stores = 1, 0, all_stores_
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    queries = _split_queries(query)
    result_queries = await _run_queries(_client(platform), queries, _fetch_one_store, stores)

    # Zepto's fast curl token silently expires; when that takes the whole city
    # down (every store errored), fall back to the self-refreshing browser sweep
    # so the search still returns data instead of a wall of "did not respond".
    if (platform == "zepto"
            and any(q["results"] for q in result_queries)
            and all(r.get("error") for q in result_queries for r in q["results"])):
        stores = stores[:_BROWSER_STORES_PER_PART]
        result_queries = [
            {"query": q["query"], "results": await _zepto_multi_sweep(q["query"], stores)}
            for q in result_queries
        ]

    return {
        "group": g,
        "group_count": group_count,
        "cities": labels,
        "store_count": len(stores),
        "total_store_count": total,
        "queries": result_queries,
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
_ZEPTO_MULTI_MJS = str(Path(__file__).resolve().parent / "tools" / "zepto_multi.mjs")
# the guest browser sweep costs ~14s/store, so a "part" is much smaller than the
# guest-API fan-out's 25. Phone stock barely varies within a city, so a spread of
# ~8 per part still shows the real price/availability picture, and Parts cover more.
_BROWSER_STORES_PER_PART = int(os.environ.get("ZEPTO_BROWSER_STORES_PER_PART", "15"))


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


# --- Zepto real search across MANY locations, via a logged-out browser --------
# A logged-in Zepto session pins to the account address and ignores injected
# coordinates; a logged-out (guest) one resolves its store purely from the
# lat/lng we seed -- so we drive a guest browser to each store's coordinates,
# run the natively-signed full search, and parse real phones per location. Slow
# (a cold browser per store), so it runs in small numbered parts like the guest
# fan-out, and holds the same one-browser lock.
# how many headless browsers run at once. A Chrome process resolves one store
# at a time, so the only way to go faster is more browsers -- each needs its own
# guest profile (a profile backs one Chrome). ~4x faster than a single lane.
_ZEPTO_PARALLEL = int(os.environ.get("ZEPTO_PARALLEL", "1"))  # 1 = reliable; >1 tried but WAF+CPU contention made it no faster
_GUEST_PROFILE = Path(__file__).resolve().parent / ".zepto-guest"


def _ensure_parallel_profiles(n: int) -> list[str]:
    """n guest-profile dirs: the warm base plus clones for the extra lanes. Clones
    inherit the base's warm aws-waf-token (headless is WAF-blocked cold) and then
    self-refresh. Big caches are skipped; cookies/localStorage carry the token."""
    import shutil
    profiles = [str(_GUEST_PROFILE)]
    for i in range(1, n):
        p = _GUEST_PROFILE.parent / f".zepto-guest-{i}"
        if not p.exists() and _GUEST_PROFILE.exists():
            try:
                shutil.copytree(_GUEST_PROFILE, p, ignore=shutil.ignore_patterns(
                    "Singleton*", "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
                    "DawnWebGPUCache", "Service Worker"))
            except Exception:
                p = _GUEST_PROFILE  # sharing serializes that lane, but still works
        profiles.append(str(p))
    return profiles


async def _sweep_lane(query: str, lane_stores: list[dict], profile: str, port: int) -> dict:
    """One browser lane: run the sweeper over its slice, keyed by store id."""
    if not lane_stores:
        return {}
    import tempfile
    locs = [{"id": s["id"], "lat": s.get("lat"), "lng": s.get("lng")} for s in lane_stores]
    fd, locs_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(locs, f)
    env = {**os.environ, "ZEPTO_PROFILE": profile, "ZEPTO_PORT": str(port)}
    out = b""
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", _ZEPTO_MULTI_MJS, query, locs_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=max(150, len(lane_stores) * 22 + 30))
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
    finally:
        try: os.unlink(locs_path)
        except OSError: pass
    by_id = {}
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            by_id[o["id"]] = o
        except (json.JSONDecodeError, KeyError):
            continue
    return by_id


async def _zepto_multi_sweep(query: str, stores: list[dict]) -> list[dict]:
    async with _zepto_browser_lock:   # one sweep at a time, but N browsers within it
        n = max(1, min(_ZEPTO_PARALLEL, len(stores)))
        profiles = await asyncio.to_thread(_ensure_parallel_profiles, n)
        # round-robin split so each lane spans the whole city, not one cluster
        lanes = [stores[i::n] for i in range(n)]
        lane_maps = await asyncio.gather(*[
            _sweep_lane(query, lanes[i], profiles[i], 9461 + i) for i in range(n)
        ])

    by_id: dict[str, dict] = {}
    for m in lane_maps:
        by_id.update(m)

    client = ZeptoClient()
    results = []
    for s in stores:
        base = {
            "store_id": s["id"], "locality": s["locality"], "city_label": s["city_label"],
            "distance_km": s["distance_km"], "nearest_metro": s.get("nearest_metro"),
            "lat": s.get("lat"), "lng": s.get("lng"),
        }
        o = by_id.get(s["id"])
        if not o or not o.get("ok"):
            results.append({**base, "items": [], "error": (o or {}).get("error", "no response")})
            continue
        try:
            items = client.parse_inventory(json.loads(o["body"]))
            results.append({**base, "items": items, "error": None})
        except Exception as e:  # a single malformed body must not sink the whole sweep
            results.append({**base, "items": [], "error": str(e)[:120]})
    return results


@app.get("/api/zepto-city-search")
async def zepto_city_search(query: str = Query(...), cities: str = Query(...), group: int = -1):
    keys, seen = [], set()
    for key in (c.strip() for c in cities.split(",")):
        if key and key not in seen:
            seen.add(key); keys.append(key)
    if not keys:
        raise HTTPException(status_code=400, detail="No cities provided")
    try:
        labels = [{"key": k, "label": city_label("zepto", k)} for k in keys]
        all_stores_ = [s for k in keys for s in stores_in_city("zepto", k)]
    except KeyError as e:
        raise HTTPException(status_code=400, detail=e.args[0])

    total = len(all_stores_)
    cap = _BROWSER_STORES_PER_PART
    if total > cap:
        group_count = (total + cap - 1) // cap
        g = 0 if group < 0 else max(0, min(group, group_count - 1))
        stores = all_stores_[g * cap:(g + 1) * cap]
    else:
        group_count, g, stores = 1, 0, all_stores_

    per_query = []
    for q in _split_queries(query):
        per_query.append({"query": q, "results": await _zepto_multi_sweep(q, stores)})
    return {
        "group": g, "group_count": group_count, "cities": labels,
        "store_count": len(stores), "total_store_count": total, "queries": per_query,
    }
