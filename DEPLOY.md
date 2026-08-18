# Deploying traker to web hosting

The app is a FastAPI server (`server:app`). A cloud deploy is **Instamart-only** —
Zepto search needs a real browser on your Mac and can't run on a host (it's hidden
in the UI already).

Files prepared for you:
- `Procfile` / `runtime.txt` — for buildpack hosts (Render, Railway)
- `Dockerfile` / `.dockerignore` — for Docker hosts (Fly.io, etc.)
- `.gitignore` — keeps the session cookie OUT of git
- Instamart session is read from the `INSTAMART_PROFILES_JSON` env var (a secret),
  so it never has to be committed.

## Recommended: Render (free, git-based)

1. Put this folder in a GitHub repo and push it. (The `.gitignore` already excludes
   `instamart_profiles.json`, so your cookie stays private.)

2. Get the session as a one-line value to paste as a secret:
       python3 -c "import json;print(json.dumps(json.load(open('instamart_profiles.json'))))"

3. On https://render.com → New → Web Service → connect the repo:
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
     (Render also auto-detects the `Procfile`.)
   - Add an **Environment Variable**:
     `INSTAMART_PROFILES_JSON` = the one-line value from step 2

4. Create the service. In ~2 min you get a `https://<name>.onrender.com` URL.

## Docker hosts (Fly.io, etc.)

    fly launch --no-deploy         # uses the Dockerfile
    fly secrets set INSTAMART_PROFILES_JSON="$(python3 -c "import json;print(json.dumps(json.load(open('instamart_profiles.json'))))")"
    fly deploy

## Honest caveats

- **Datacenter IP**: Instamart may throttle/deny requests from a cloud IP more than
  from your home IP. It may work fine; if it starts returning errors, the fix is a
  residential proxy (extra setup) or go back to the ngrok-from-Mac tunnel.
- **Cookie expiry**: when Instamart searches start failing, refresh the cookie —
  capture a new session and update the `INSTAMART_PROFILES_JSON` env var.
- **Free tiers sleep**: Render/Railway free instances spin down when idle; the first
  request after a nap takes ~30-60s.
- **ToS**: this reads Instamart's private API. Keep traffic low and personal.

## Vercel

Files prepared: `api/index.py` (ASGI entrypoint), `vercel.json`, `.vercelignore`.
Vercel is serverless, so mind the caveats below.

    vercel login                                   # your account (interactive)

    # add the Instamart session as a Production secret:
    vercel env add INSTAMART_PROFILES_JSON production
    #   paste the one-line value from:
    #   python3 -c "import json;print(json.dumps(json.load(open('instamart_profiles.json'))))"

    # keep city searches under the serverless timeout (smaller fan-out):
    vercel env add STORES_PER_CITY_INSTAMART production      # value: 12

    vercel --prod                                  # deploy → https://<name>.vercel.app

### Vercel-specific caveats
- **10s timeout on the Hobby (free) plan.** Pincode and small searches are fine;
  a big city search can be cut off. The `STORES_PER_CITY_INSTAMART=12` env var
  above trims the fan-out. Pro raises the limit (vercel.json already asks for 60s).
- **curl_cffi native lib** must load in Vercel's Lambda-based runtime. It usually
  does via the manylinux wheel, but if you see import/500 errors on deploy, that's
  the cause — fall back to Render/Fly (a normal long-running server, better fit for
  this app) or the ngrok-from-Mac tunnel.
- **No persistent disk**: the pincode geocode cache can't persist between calls, so
  pincode lookups re-hit Nominatim (rate-limited). City/store searches are unaffected.
