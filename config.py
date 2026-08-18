import asyncio
import base64
import csv
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from curl_cffi.requests import AsyncSession

_ROOT = Path(__file__).parent


@dataclass
class PlatformConfig:
    base_url: str
    headers: dict = field(default_factory=dict)
    label: str = ""
    # each platform has its own darkstore network, so its own store list
    stores_file: str = ""


INSTAMART = PlatformConfig(
    base_url="https://instamart.in", label="Instamart", stores_file="darkstores.csv"
)
# Zepto's base_url comes from the captured request itself -- see zepto_profile()
ZEPTO = PlatformConfig(base_url="", label="Zepto", stores_file="zepto_darkstores.csv")

PLATFORMS = {"instamart": INSTAMART, "zepto": ZEPTO}

_INSTAMART_PROFILES_PATH = _ROOT / "instamart_profiles.json"
_ZEPTO_PROFILE_PATH = _ROOT / "zepto_profile.json"
_ZEPTO_REFRESH_PATH = _ROOT / "zepto_refresh.json"
_METRO_STATIONS_PATH = _ROOT / "metro_stations.csv"
_GEOCODE_CACHE_PATH = _ROOT / "pincode_geocode_cache.json"


def _load_instamart_profiles() -> dict:
    # on a host, the session comes in as a secret env var rather than a file
    env = os.environ.get("INSTAMART_PROFILES_JSON")
    if env:
        return json.loads(env)
    if not _INSTAMART_PROFILES_PATH.exists():
        return {}
    return json.loads(_INSTAMART_PROFILES_PATH.read_text())


def instamart_headers() -> dict:
    """Any one captured session works for any store -- storeId, not the
    cookie, determines which store's catalog gets searched (verified:
    swapping storeId while keeping the same cookie returns that store's
    own data, confirmed via the response's podId)."""
    profiles = _load_instamart_profiles()
    if not profiles:
        raise KeyError(
            "No captured Instamart session found. Capture a search request "
            "from instamart.in and add it to instamart_profiles.json."
        )
    profile = next(iter(profiles.values()))
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "cookie": profile["cookie"],
        "matcher": profile.get("matcher", ""),
        "origin": "https://instamart.in",
        "referer": "https://instamart.in/",
        "user-agent": profile.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        ),
        "x-build-version": profile.get("build_version", "2.362.0"),
        "x-device-id": profile["device_id"],
    }


def zepto_profile() -> dict:
    """Zepto's search request, captured verbatim from a logged-in browser.

    Unlike Instamart -- whose endpoint is stable and whose only secret is a
    cookie -- Zepto signs requests with short-lived tokens and moves its API
    around, so nothing here is hardcoded. tools/import_zepto_curl.py turns a
    'Copy as cURL' from DevTools into this file: url, method, headers and body
    template, with {query}, {lat}, {lng} and {store_id} left as placeholders.
    """
    if not _ZEPTO_PROFILE_PATH.exists():
        raise KeyError(
            "No captured Zepto request found. In a logged-in zeptonow.com tab, "
            "search for a product, copy that request from DevTools as cURL, and run: "
            "python tools/import_zepto_curl.py --query '<what you searched>' < capture.txt"
        )
    profile = json.loads(_ZEPTO_PROFILE_PATH.read_text())
    missing = [k for k in ("url", "method", "headers") if k not in profile]
    if missing:
        raise KeyError(f"zepto_profile.json is missing {', '.join(missing)}; re-run the import tool.")
    return profile


def zepto_session_status() -> dict:
    """Zepto's accessToken is a JWT good for one hour. Reading its expiry from
    the cookie lets a stale capture fail instantly with the real reason, rather
    than every store burning its full retry budget against a 429 wall."""
    if not _ZEPTO_PROFILE_PATH.exists():
        return {"ready": False, "reason": "No captured Zepto session yet.", "expires_at": None}
    try:
        cookie = json.loads(_ZEPTO_PROFILE_PATH.read_text())["headers"]["cookie"]
        token = next(c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("accessToken="))
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    except Exception:
        # no readable token: let the request itself decide
        return {"ready": True, "reason": "", "expires_at": None}

    left = exp - time.time()
    if left <= 0:
        capture = (
            f"Zepto session expired {int(-left // 60)} min ago. Re-capture the search "
            "request from a zepto.com tab and re-run tools/import_zepto_curl.py."
        )
        # a refresh profile means we can renew silently, so soften the message
        if zepto_has_refresh():
            capture = "Zepto access token expired; renewing from the captured refresh request…"
        return {"ready": False, "expires_at": exp, "reason": capture}
    return {"ready": True, "reason": "", "expires_at": exp}


def zepto_is_configured() -> bool:
    return zepto_session_status()["ready"]


# --- automatic session renewal -------------------------------------------
# Zepto's accessToken lasts an hour; the refreshToken lasts far longer. If the
# user captures the browser's own token-refresh request once, we can replay it
# to mint a new accessToken whenever the current one lapses -- turning "re-capture
# every hour" into "re-capture when the refresh token itself finally dies".


def zepto_refresh_profile() -> dict | None:
    if not _ZEPTO_REFRESH_PATH.exists():
        return None
    return json.loads(_ZEPTO_REFRESH_PATH.read_text())


def zepto_has_refresh() -> bool:
    return _ZEPTO_REFRESH_PATH.exists()


def zepto_usable() -> bool:
    """True if we can serve Zepto now or renew our way into it."""
    return zepto_session_status()["ready"] or zepto_has_refresh()


def zepto_browser_ready() -> bool:
    """Zepto search now runs through the logged-in browser profile, so what
    matters is that the profile exists (a login happened at least once), not the
    stored curl token."""
    return (_ROOT / ".zepto-chrome").exists()


def _cookie_to_dict(cookie: str) -> dict:
    jar = {}
    for part in cookie.split("; "):
        if "=" in part:
            key, value = part.split("=", 1)
            jar[key] = value
    return jar


def _dict_to_cookie(jar: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def apply_zepto_tokens(access: str | None, refresh: str | None) -> None:
    """Persist renewed tokens into the cookie of every stored Zepto profile.

    Refresh tokens often rotate -- each renewal hands back a new one and voids
    the old -- so the refresh profile's own cookie is updated too, otherwise the
    second renewal would replay a dead token.
    """
    for path in (_ZEPTO_PROFILE_PATH, _ZEPTO_REFRESH_PATH):
        if not path.exists():
            continue
        profile = json.loads(path.read_text())
        cookie = profile.get("headers", {}).get("cookie")
        if not cookie:
            continue
        jar = _cookie_to_dict(cookie)
        if access:
            jar["accessToken"] = access
        if refresh:
            jar["refreshToken"] = refresh
        profile["headers"]["cookie"] = _dict_to_cookie(jar)
        path.write_text(json.dumps(profile, indent=2) + "\n")


_metro_stations_cache: list[dict] | None = None


def _load_metro_stations() -> list[dict]:
    global _metro_stations_cache
    if _metro_stations_cache is None:
        with open(_METRO_STATIONS_PATH, newline="", encoding="utf-8") as f:
            _metro_stations_cache = [
                {"name": r["name"], "network": r["network"], "lat": float(r["lat"]), "lng": float(r["lng"])}
                for r in csv.DictReader(f)
            ]
    return _metro_stations_cache


_MAX_METRO_DISTANCE_KM = 15.0


def _nearest_metro(lat: float, lng: float) -> dict | None:
    stations = _load_metro_stations()
    best = min(stations, key=lambda m: _haversine_km(lat, lng, m["lat"], m["lng"]))
    distance_km = round(_haversine_km(lat, lng, best["lat"], best["lng"]), 2)
    if distance_km > _MAX_METRO_DISTANCE_KM:
        return None  # no metro system anywhere near this location
    return {"name": best["name"], "network": best["network"], "distance_km": distance_km}


_darkstores_cache: dict[str, list[dict]] = {}

# Zepto names its stores '<CITY CODE>-<locality>'; the code is redundant once
# the store is grouped under a city, and it makes the column headers noisy
_STORE_CODE_PREFIX = re.compile(r"^[A-Za-z]{2,5}[\s_-]+(?=\S)")


def _load_darkstores(platform: str = "instamart") -> list[dict]:
    """Both store files carry id/lat/lng; they differ in whether the locality
    column is called 'locality' or 'name', and Zepto's also names the city."""
    if platform not in _darkstores_cache:
        path = _ROOT / PLATFORMS[platform].stores_file
        if not path.exists():
            raise KeyError(f"No store list for {platform!r} -- expected {path.name}")
        with open(path, newline="", encoding="utf-8") as f:
            stores = []
            for r in csv.DictReader(f):
                raw_name = r.get("locality") or r.get("name") or ""
                city_hint = (r.get("city") or "").strip()
                if city_hint.lower() == "test city":
                    continue  # a placeholder row in Zepto's list, not a real store
                stores.append({
                    "id": r["id"],
                    "locality": _STORE_CODE_PREFIX.sub("", raw_name).strip() or raw_name,
                    "lat": float(r["lat"]),
                    "lng": float(r["lng"]),
                    "city_hint": city_hint,
                })
        for s in stores:
            s["nearest_metro"] = _nearest_metro(s["lat"], s["lng"])
        _darkstores_cache[platform] = stores
    return _darkstores_cache[platform]


CITY_PRESETS = {
    # metros
    "delhi": {"label": "Delhi", "lat": 28.6139, "lng": 77.2090, "radius_km": 25},
    "gurugram": {"label": "Gurugram", "lat": 28.4595, "lng": 77.0266, "radius_km": 25},
    "noida": {"label": "Noida", "lat": 28.5355, "lng": 77.3910, "radius_km": 25},
    "bangalore": {"label": "Bangalore", "lat": 12.9716, "lng": 77.5946, "radius_km": 25},
    "mumbai": {"label": "Mumbai", "lat": 19.0760, "lng": 72.8777, "radius_km": 30},
    "hyderabad": {"label": "Hyderabad", "lat": 17.3850, "lng": 78.4867, "radius_km": 30},
    "chennai": {"label": "Chennai", "lat": 13.0827, "lng": 80.2707, "radius_km": 45},
    "pune": {"label": "Pune", "lat": 18.5204, "lng": 73.8567, "radius_km": 25},
    "kolkata": {"label": "Kolkata", "lat": 22.5726, "lng": 88.3639, "radius_km": 25},
    # NCR and MMR satellites -- own cities in their own right, and without an
    # entry here their stores get quietly absorbed by Noida/Mumbai. Tight radii:
    # they only need to out-rank the big neighbour over their own ground.
    "faridabad": {"label": "Faridabad", "lat": 28.4089, "lng": 77.3178, "radius_km": 9},
    "ghaziabad": {"label": "Ghaziabad", "lat": 28.6692, "lng": 77.4538, "radius_km": 10},
    "greater-noida": {"label": "Greater Noida", "lat": 28.4744, "lng": 77.5040, "radius_km": 12},
    # kept tight on purpose: Mulund and Bhandup sit ~5 km from Thane station
    # but are Mumbai, so the ambiguous band is left to the parent metro
    "thane": {"label": "Thane", "lat": 19.2183, "lng": 72.9781, "radius_km": 4.5},
    "navi-mumbai": {"label": "Navi Mumbai", "lat": 19.0330, "lng": 73.0297, "radius_km": 12},
    # rest of India -- any city with no entry here still shows up, grouped by
    # _discover_clusters() below and labelled after its largest locality
    "ahmedabad": {"label": "Ahmedabad", "lat": 23.0225, "lng": 72.5714, "radius_km": 25},
    "surat": {"label": "Surat", "lat": 21.1702, "lng": 72.8311, "radius_km": 20},
    "vadodara": {"label": "Vadodara", "lat": 22.3072, "lng": 73.1812, "radius_km": 20},
    "rajkot": {"label": "Rajkot", "lat": 22.3039, "lng": 70.8022, "radius_km": 20},
    "jaipur": {"label": "Jaipur", "lat": 26.9124, "lng": 75.7873, "radius_km": 25},
    "jodhpur": {"label": "Jodhpur", "lat": 26.2389, "lng": 73.0243, "radius_km": 20},
    "udaipur": {"label": "Udaipur", "lat": 24.5854, "lng": 73.7125, "radius_km": 20},
    "kota": {"label": "Kota", "lat": 25.2138, "lng": 75.8648, "radius_km": 20},
    "ajmer": {"label": "Ajmer", "lat": 26.4499, "lng": 74.6399, "radius_km": 20},
    "lucknow": {"label": "Lucknow", "lat": 26.8467, "lng": 80.9462, "radius_km": 25},
    "kanpur": {"label": "Kanpur", "lat": 26.4499, "lng": 80.3319, "radius_km": 20},
    "varanasi": {"label": "Varanasi", "lat": 25.3176, "lng": 82.9739, "radius_km": 20},
    "prayagraj": {"label": "Prayagraj", "lat": 25.4358, "lng": 81.8463, "radius_km": 20},
    "agra": {"label": "Agra", "lat": 27.1767, "lng": 78.0081, "radius_km": 20},
    "meerut": {"label": "Meerut", "lat": 28.9845, "lng": 77.7064, "radius_km": 20},
    "bareilly": {"label": "Bareilly", "lat": 28.3670, "lng": 79.4304, "radius_km": 20},
    "gorakhpur": {"label": "Gorakhpur", "lat": 26.7606, "lng": 83.3732, "radius_km": 20},
    "dehradun": {"label": "Dehradun", "lat": 30.3165, "lng": 78.0322, "radius_km": 20},
    "chandigarh": {"label": "Chandigarh", "lat": 30.7333, "lng": 76.7794, "radius_km": 25},
    # tricity split -- nearest-centre-in-range does the work, so Mohali can
    # reach out to Kharar without taking Chandigarh's own inner sectors
    "mohali": {"label": "Mohali", "lat": 30.7046, "lng": 76.7179, "radius_km": 10},
    "panchkula": {"label": "Panchkula", "lat": 30.6942, "lng": 76.8606, "radius_km": 6},
    "zirakpur": {"label": "Zirakpur", "lat": 30.6425, "lng": 76.8173, "radius_km": 5},
    "ludhiana": {"label": "Ludhiana", "lat": 30.9010, "lng": 75.8573, "radius_km": 20},
    "amritsar": {"label": "Amritsar", "lat": 31.6340, "lng": 74.8723, "radius_km": 20},
    "jalandhar": {"label": "Jalandhar", "lat": 31.3260, "lng": 75.5762, "radius_km": 20},
    "shimla": {"label": "Shimla", "lat": 31.1048, "lng": 77.1734, "radius_km": 20},
    "jammu": {"label": "Jammu", "lat": 32.7266, "lng": 74.8570, "radius_km": 20},
    "srinagar": {"label": "Srinagar", "lat": 34.0837, "lng": 74.7973, "radius_km": 20},
    "indore": {"label": "Indore", "lat": 22.7196, "lng": 75.8577, "radius_km": 20},
    "bhopal": {"label": "Bhopal", "lat": 23.2599, "lng": 77.4126, "radius_km": 20},
    "jabalpur": {"label": "Jabalpur", "lat": 23.1815, "lng": 79.9864, "radius_km": 20},
    "gwalior": {"label": "Gwalior", "lat": 26.2183, "lng": 78.1828, "radius_km": 20},
    "nagpur": {"label": "Nagpur", "lat": 21.1458, "lng": 79.0882, "radius_km": 20},
    "nashik": {"label": "Nashik", "lat": 19.9975, "lng": 73.7898, "radius_km": 20},
    "aurangabad": {"label": "Chhatrapati Sambhajinagar", "lat": 19.8762, "lng": 75.3433, "radius_km": 20},
    "kalyan": {"label": "Kalyan-Dombivli", "lat": 19.2403, "lng": 73.1305, "radius_km": 20},
    "vasai-virar": {"label": "Vasai-Virar", "lat": 19.3919, "lng": 72.8397, "radius_km": 15},
    "kolhapur": {"label": "Kolhapur", "lat": 16.7050, "lng": 74.2433, "radius_km": 20},
    "solapur": {"label": "Solapur", "lat": 17.6599, "lng": 75.9064, "radius_km": 20},
    "patna": {"label": "Patna", "lat": 25.5941, "lng": 85.1376, "radius_km": 20},
    "ranchi": {"label": "Ranchi", "lat": 23.3441, "lng": 85.3096, "radius_km": 20},
    "jamshedpur": {"label": "Jamshedpur", "lat": 22.8046, "lng": 86.2029, "radius_km": 20},
    "dhanbad": {"label": "Dhanbad", "lat": 23.7957, "lng": 86.4304, "radius_km": 20},
    "asansol": {"label": "Asansol", "lat": 23.6739, "lng": 86.9524, "radius_km": 20},
    "durgapur": {"label": "Durgapur", "lat": 23.5204, "lng": 87.3119, "radius_km": 20},
    "siliguri": {"label": "Siliguri", "lat": 26.7271, "lng": 88.3953, "radius_km": 20},
    "guwahati": {"label": "Guwahati", "lat": 26.1445, "lng": 91.7362, "radius_km": 20},
    "bhubaneswar": {"label": "Bhubaneswar", "lat": 20.2961, "lng": 85.8245, "radius_km": 25},
    "raipur": {"label": "Raipur", "lat": 21.2514, "lng": 81.6296, "radius_km": 20},
    "visakhapatnam": {"label": "Visakhapatnam", "lat": 17.6868, "lng": 83.2185, "radius_km": 20},
    "vijayawada": {"label": "Vijayawada", "lat": 16.5062, "lng": 80.6480, "radius_km": 20},
    "guntur": {"label": "Guntur", "lat": 16.3067, "lng": 80.4365, "radius_km": 20},
    "tirupati": {"label": "Tirupati", "lat": 13.6288, "lng": 79.4192, "radius_km": 20},
    "warangal": {"label": "Warangal", "lat": 17.9689, "lng": 79.5941, "radius_km": 20},
    "kochi": {"label": "Kochi", "lat": 9.9312, "lng": 76.2673, "radius_km": 25},
    "thrissur": {"label": "Thrissur", "lat": 10.5276, "lng": 76.2144, "radius_km": 20},
    "kozhikode": {"label": "Kozhikode", "lat": 11.2588, "lng": 75.7804, "radius_km": 20},
    "kottayam": {"label": "Kottayam", "lat": 9.5916, "lng": 76.5222, "radius_km": 25},
    "thiruvananthapuram": {"label": "Thiruvananthapuram", "lat": 8.5241, "lng": 76.9366, "radius_km": 20},
    "coimbatore": {"label": "Coimbatore", "lat": 11.0168, "lng": 76.9558, "radius_km": 20},
    "madurai": {"label": "Madurai", "lat": 9.9252, "lng": 78.1198, "radius_km": 20},
    "tiruchirappalli": {"label": "Tiruchirappalli", "lat": 10.7905, "lng": 78.7047, "radius_km": 20},
    "salem": {"label": "Salem", "lat": 11.6643, "lng": 78.1460, "radius_km": 20},
    "vellore": {"label": "Vellore", "lat": 12.9165, "lng": 79.1325, "radius_km": 20},
    "puducherry": {"label": "Puducherry", "lat": 11.9416, "lng": 79.8083, "radius_km": 20},
    "mysuru": {"label": "Mysuru", "lat": 12.2958, "lng": 76.6394, "radius_km": 20},
    "mangaluru": {"label": "Mangaluru", "lat": 12.9141, "lng": 74.8560, "radius_km": 20},
    "hubballi": {"label": "Hubballi-Dharwad", "lat": 15.3647, "lng": 75.1240, "radius_km": 25},
    "goa": {"label": "Goa", "lat": 15.4909, "lng": 73.8278, "radius_km": 35},
    "aligarh": {"label": "Aligarh", "lat": 27.8974, "lng": 78.0880, "radius_km": 20},
    "moradabad": {"label": "Moradabad", "lat": 28.8386, "lng": 78.7733, "radius_km": 20},
    "mathura": {"label": "Mathura", "lat": 27.4924, "lng": 77.6737, "radius_km": 20},
    "jhansi": {"label": "Jhansi", "lat": 25.4484, "lng": 78.5685, "radius_km": 20},
    "saharanpur": {"label": "Saharanpur", "lat": 29.9680, "lng": 77.5460, "radius_km": 20},
    "roorkee": {"label": "Roorkee", "lat": 29.8543, "lng": 77.8880, "radius_km": 20},
    "rudrapur": {"label": "Rudrapur", "lat": 28.9750, "lng": 79.4000, "radius_km": 20},
    "panipat": {"label": "Panipat", "lat": 29.3909, "lng": 76.9635, "radius_km": 20},
    "karnal": {"label": "Karnal", "lat": 29.6857, "lng": 76.9905, "radius_km": 20},
    "kurukshetra": {"label": "Kurukshetra", "lat": 29.9695, "lng": 76.8783, "radius_km": 20},
    "ambala": {"label": "Ambala", "lat": 30.3782, "lng": 76.7767, "radius_km": 20},
    "rohtak": {"label": "Rohtak", "lat": 28.8955, "lng": 76.6066, "radius_km": 20},
    "patiala": {"label": "Patiala", "lat": 30.3398, "lng": 76.3869, "radius_km": 20},
    "bathinda": {"label": "Bathinda", "lat": 30.2110, "lng": 74.9455, "radius_km": 20},
    "sri-ganganagar": {"label": "Sri Ganganagar", "lat": 29.9094, "lng": 73.8800, "radius_km": 20},
    "bikaner": {"label": "Bikaner", "lat": 28.0229, "lng": 73.3119, "radius_km": 20},
    "muzaffarpur": {"label": "Muzaffarpur", "lat": 26.1209, "lng": 85.3647, "radius_km": 20},
    "kalyani": {"label": "Kalyani", "lat": 22.9750, "lng": 88.4344, "radius_km": 20},
    "silchar": {"label": "Silchar", "lat": 24.8333, "lng": 92.7789, "radius_km": 20},
    "dibrugarh": {"label": "Dibrugarh", "lat": 27.4728, "lng": 94.9120, "radius_km": 20},
    "berhampur": {"label": "Berhampur", "lat": 19.3150, "lng": 84.7941, "radius_km": 20},
    "bilaspur": {"label": "Bilaspur", "lat": 22.0797, "lng": 82.1409, "radius_km": 20},
    "bhilai": {"label": "Bhilai-Durg", "lat": 21.2000, "lng": 81.3500, "radius_km": 20},
    "ujjain": {"label": "Ujjain", "lat": 23.1765, "lng": 75.7885, "radius_km": 20},
    "amravati": {"label": "Amravati", "lat": 20.9320, "lng": 77.7523, "radius_km": 20},
    "nanded": {"label": "Nanded", "lat": 19.1383, "lng": 77.3210, "radius_km": 20},
    "latur": {"label": "Latur", "lat": 18.4088, "lng": 76.5604, "radius_km": 20},
    "anand": {"label": "Anand", "lat": 22.5645, "lng": 72.9289, "radius_km": 20},
    "vapi": {"label": "Vapi-Daman", "lat": 20.3893, "lng": 72.9106, "radius_km": 20},
    "nizamabad": {"label": "Nizamabad", "lat": 18.6725, "lng": 78.0941, "radius_km": 20},
    "karimnagar": {"label": "Karimnagar", "lat": 18.4386, "lng": 79.1288, "radius_km": 20},
    "khammam": {"label": "Khammam", "lat": 17.2473, "lng": 80.1514, "radius_km": 20},
    "rajahmundry": {"label": "Rajahmundry", "lat": 17.0005, "lng": 81.8040, "radius_km": 20},
    "kakinada": {"label": "Kakinada", "lat": 16.9891, "lng": 82.2475, "radius_km": 20},
    "tenali": {"label": "Tenali", "lat": 16.2430, "lng": 80.6400, "radius_km": 15},
    "ongole": {"label": "Ongole", "lat": 15.5057, "lng": 80.0499, "radius_km": 20},
    "nellore": {"label": "Nellore", "lat": 14.4426, "lng": 79.9865, "radius_km": 20},
    "kurnool": {"label": "Kurnool", "lat": 15.8281, "lng": 78.0373, "radius_km": 20},
    "anantapur": {"label": "Anantapur", "lat": 14.6819, "lng": 77.6006, "radius_km": 20},
    "belagavi": {"label": "Belagavi", "lat": 15.8497, "lng": 74.4977, "radius_km": 20},
    "udupi": {"label": "Udupi-Manipal", "lat": 13.3409, "lng": 74.7421, "radius_km": 20},
    "tumakuru": {"label": "Tumakuru", "lat": 13.3379, "lng": 77.1173, "radius_km": 20},
    "davanagere": {"label": "Davanagere", "lat": 14.4644, "lng": 75.9218, "radius_km": 20},
    "shivamogga": {"label": "Shivamogga", "lat": 13.9299, "lng": 75.5681, "radius_km": 20},
    "kannur": {"label": "Kannur", "lat": 11.8745, "lng": 75.3704, "radius_km": 20},
    "kollam": {"label": "Kollam", "lat": 8.8932, "lng": 76.6141, "radius_km": 20},
    "perumbavoor": {"label": "Perumbavoor", "lat": 10.1074, "lng": 76.4750, "radius_km": 20},
    "palakkad": {"label": "Palakkad", "lat": 10.7867, "lng": 76.6548, "radius_km": 20},
    "nagercoil": {"label": "Nagercoil", "lat": 8.1833, "lng": 77.4119, "radius_km": 20},
    "tirunelveli": {"label": "Tirunelveli", "lat": 8.7139, "lng": 77.7567, "radius_km": 20},
    "thoothukudi": {"label": "Thoothukudi", "lat": 8.7642, "lng": 78.1348, "radius_km": 20},
    "thanjavur": {"label": "Thanjavur", "lat": 10.7870, "lng": 79.1378, "radius_km": 20},
    "karur": {"label": "Karur", "lat": 10.9601, "lng": 78.0766, "radius_km": 20},
    "tiruppur": {"label": "Tiruppur", "lat": 11.1085, "lng": 77.3411, "radius_km": 20},
    "erode": {"label": "Erode", "lat": 11.3410, "lng": 77.7172, "radius_km": 20},
    "kanchipuram": {"label": "Kanchipuram", "lat": 12.8342, "lng": 79.7036, "radius_km": 20},
}

_METRO_KEYS = frozenset(
    {
        "delhi", "gurugram", "noida", "faridabad", "ghaziabad", "greater-noida",
        "bangalore", "mumbai", "thane", "navi-mumbai", "hyderabad", "chennai",
        "pune", "kolkata",
    }
)

# leftovers -- stores outside every preset above -- are grouped into their own
# ad-hoc "cities" so that no darkstore is invisible in the UI
_CLUSTER_RADIUS_KM = 20.0

_city_index_cache: dict[str, dict] = {}


def _titlecase_locality(name: str) -> str:
    """Locality names in the CSV are a mix of ALL CAPS, Title Case and noise
    like ' - MP'; normalise them enough to use as a city label."""
    cleaned = name.split(" - ")[0].strip()
    words = [w if (w.isupper() and len(w) <= 3) else w.capitalize() for w in cleaned.split()]
    return " ".join(words) or cleaned


# the store files spell some cities differently than CITY_PRESETS does; without
# this a 'Bengaluru' hint reads as a second city next to 'Bangalore'
_CITY_ALIASES = {
    "bengaluru": "Bangalore",
    "vadodra": "Vadodara",
    "sas nagar": "Mohali",
    "chatrapati sambhaji nagar": "Chhatrapati Sambhajinagar",
    "gurgaon": "Gurugram",
    "bombay": "Mumbai",
    "calcutta": "Kolkata",
    "madras": "Chennai",
    "trichy": "Tiruchirappalli",
    "mysore": "Mysuru",
    "belgavi": "Belagavi",
    "tumkuru": "Tumakuru",
}


def _discover_clusters(leftovers: list[dict]) -> list[dict]:
    """Greedy clustering: repeatedly take the store with the most neighbours
    within _CLUSTER_RADIUS_KM, make that group a city, remove it, repeat."""
    clusters = []
    pool = list(leftovers)
    while pool:
        seed = max(
            pool,
            key=lambda c: sum(
                1 for s in pool if _haversine_km(c["lat"], c["lng"], s["lat"], s["lng"]) <= _CLUSTER_RADIUS_KM
            ),
        )
        members = [s for s in pool if _haversine_km(seed["lat"], seed["lng"], s["lat"], s["lng"]) <= _CLUSTER_RADIUS_KM]
        hint = _CITY_ALIASES.get((seed.get("city_hint") or "").lower(), seed.get("city_hint") or "")
        clusters.append(
            {
                # prefer the store file's own city name over a locality guess
                "label": (_titlecase_locality(hint) if hint.isupper() else hint)
                         or _titlecase_locality(seed["locality"]),
                # used when the hint collides with a city that already exists:
                # Zepto files distant towns like Palghar under 'Mumbai'
                "alt_label": _titlecase_locality(seed["locality"]),
                "lat": sum(s["lat"] for s in members) / len(members),
                "lng": sum(s["lng"] for s in members) / len(members),
                "radius_km": _CLUSTER_RADIUS_KM,
                "members": members,
            }
        )
        pool = [s for s in pool if s not in members]
    return clusters


def _slugify(label: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-") or "area"


def _build_city_index(platform: str) -> dict:
    """key -> {label, lat, lng, curated, stores}. Every darkstore lands in
    exactly one city; presets win, leftovers form auto-discovered clusters."""
    cities = {
        key: {"label": p["label"], "lat": p["lat"], "lng": p["lng"], "curated": True, "stores": []}
        for key, p in CITY_PRESETS.items()
    }
    leftovers = []
    for s in _load_darkstores(platform):
        # of the presets whose radius actually covers this store, take the
        # nearest -- so neighbouring cities (Delhi/Gurugram/Noida) never
        # double-count, and a store isn't dropped just because the closest
        # centre happens to have a tighter radius than a slightly farther one
        in_range = {
            key: d
            for key, p in CITY_PRESETS.items()
            if (d := _haversine_km(p["lat"], p["lng"], s["lat"], s["lng"])) <= p["radius_km"]
        }
        # a suburb inside a metro stays with the metro even when some smaller
        # satellite centre is marginally closer (Dahisar is Mumbai, not Vasai)
        metro_hits = {k: d for k, d in in_range.items() if k in _METRO_KEYS}
        in_range = metro_hits or in_range
        if in_range:
            nearest_key = min(in_range, key=in_range.get)
            cities[nearest_key]["stores"].append({**s, "distance_km": round(in_range[nearest_key], 2)})
        else:
            leftovers.append(s)

    taken_labels = {c["label"] for c in cities.values()}
    for cluster in _discover_clusters(leftovers):
        # a hint that duplicates a real city means the store is an outlying town
        # filed under its nearest metro, so name it after itself instead
        label = cluster["label"]
        if label in taken_labels:
            label = cluster["alt_label"]
        taken_labels.add(label)
        key = _slugify(label)
        while key in cities:
            key += "-1"
        cities[key] = {
            "label": label,
            "lat": cluster["lat"],
            "lng": cluster["lng"],
            "curated": False,
            "stores": [
                {**s, "distance_km": round(_haversine_km(cluster["lat"], cluster["lng"], s["lat"], s["lng"]), 2)}
                for s in cluster["members"]
            ],
        }

    for city in cities.values():
        city["stores"].sort(key=lambda s: s["distance_km"])
        for s in city["stores"]:
            s["city_label"] = city["label"]
    return {k: c for k, c in cities.items() if c["stores"]}


def city_index(platform: str = "instamart") -> dict:
    if platform not in _city_index_cache:
        _city_index_cache[platform] = _build_city_index(platform)
    return _city_index_cache[platform]


def city_label(platform: str, city_key: str) -> str:
    city = city_index(platform).get(city_key)
    if not city:
        raise KeyError(f"Unknown city {city_key!r}")
    return city["label"]


def stores_in_city(platform: str, city_key: str) -> list[dict]:
    city = city_index(platform).get(city_key)
    if not city:
        raise KeyError(f"Unknown city {city_key!r}")
    return city["stores"]


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_geocode_cache: dict[str, list[float]] | None = None
_last_nominatim_call = 0.0
_nominatim_lock = asyncio.Lock()


def _load_geocode_cache() -> dict:
    global _geocode_cache
    if _geocode_cache is None:
        _geocode_cache = json.loads(_GEOCODE_CACHE_PATH.read_text()) if _GEOCODE_CACHE_PATH.exists() else {}
    return _geocode_cache


def _save_geocode_cache() -> None:
    try:
        _GEOCODE_CACHE_PATH.write_text(json.dumps(_geocode_cache, indent=2))
    except OSError:
        pass  # read-only host FS -- the in-memory cache still works this run


async def _geocode_pincode(pincode: str) -> tuple[float, float]:
    cache = _load_geocode_cache()
    if pincode in cache:
        return cache[pincode][0], cache[pincode][1]

    global _last_nominatim_call
    async with _nominatim_lock:
        wait = 1.0 - (time.monotonic() - _last_nominatim_call)
        if wait > 0:
            await asyncio.sleep(wait)
        async with AsyncSession() as session:
            resp = await session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"postalcode": pincode, "country": "India", "format": "json", "limit": 1},
                headers={"User-Agent": "traker-personal-project/1.0"},
            )
        _last_nominatim_call = time.monotonic()
    resp.raise_for_status()
    results = resp.json()

    if not results:
        raise KeyError(f"Could not geocode pincode {pincode!r} (Nominatim returned no match).")
    lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
    cache[pincode] = [lat, lng]
    _save_geocode_cache()
    return lat, lng


_flat_stores_cache: dict[str, list[dict]] = {}


def all_stores(platform: str = "instamart") -> list[dict]:
    """Every darkstore, flattened, each tagged with the city it was placed in."""
    if platform not in _flat_stores_cache:
        _flat_stores_cache[platform] = [
            {**s, "city_key": key}
            for key, city in city_index(platform).items()
            for s in city["stores"]
        ]
    return _flat_stores_cache[platform]


def search_stores(platform: str, q: str, limit: int = 40) -> list[dict]:
    """Free-text match on store locality, falling back to its city name, so
    'saket' finds the Saket darkstore and 'kochi' lists every Kochi one."""
    needle = q.strip().lower()
    if not needle:
        return []
    scored = []
    for s in all_stores(platform):
        locality, city = s["locality"].lower(), s["city_label"].lower()
        if locality.startswith(needle):
            rank = 0
        elif needle in locality:
            rank = 1
        elif city.startswith(needle):
            rank = 2
        elif needle in city:
            rank = 3
        else:
            continue
        scored.append((rank, s["distance_km"], s))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in scored[:limit]]


def stores_by_ids(platform: str, ids: list[str]) -> list[dict]:
    by_id = {s["id"]: s for s in all_stores(platform)}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise KeyError(f"Unknown store id(s): {', '.join(missing)}")
    return [by_id[i] for i in ids]


def _city_of_store(platform: str, store_id: str) -> dict | None:
    for s in all_stores(platform):
        if s["id"] == store_id:
            return {"city_label": s["city_label"], "centre_distance_km": s["distance_km"]}
    return None


async def resolve_store_for_pincode(platform: str, pincode: str) -> dict:
    lat, lng = await _geocode_pincode(pincode.strip())
    stores = _load_darkstores(platform)
    best = min(stores, key=lambda s: _haversine_km(lat, lng, s["lat"], s["lng"]))
    distance_km = round(_haversine_km(lat, lng, best["lat"], best["lng"]), 2)
    return {
        # `id`/lat/lng so this record is interchangeable with a city-mode store:
        # Instamart searches by id, Zepto by coordinates
        "id": best["id"],
        "store_id": best["id"],
        "lat": best["lat"],
        "lng": best["lng"],
        "locality": best["locality"],
        "distance_km": distance_km,
        "nearest_metro": best["nearest_metro"],
        **(_city_of_store(platform, best["id"]) or {"city_label": None, "centre_distance_km": None}),
    }
