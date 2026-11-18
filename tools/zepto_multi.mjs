// Multi-location Zepto phone search via a LOGGED-OUT (guest) browser.
//
//   node tools/zepto_multi.mjs "<query>" <locations.json>
//
// Why guest: a logged-in Zepto session pins to the account's saved address and
// ignores injected coordinates. A logged-out session resolves its serving store
// purely from the latitude/longitude it's given -- so we seed those (cookies +
// localStorage user-position), cold-load the app so it re-resolves, then run the
// real natively-signed SHOW_ALL_RESULTS search and capture it. Verified to
// switch stores by coordinate (Delhi vs Mumbai -> different stores/inventory).
//
// A running instance caches the store in a shared service worker, so each
// location needs a cold process. Sequential (the profile backs one Chrome).
// Emits one NDJSON line per location: {idx,id,lat,lng,resolved,ok,body|error}.
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { once } from 'node:events';

const query = process.argv[2];
const locs = JSON.parse(readFileSync(process.argv[3], 'utf8'));
if (!query || !Array.isArray(locs)) { console.error('usage: zepto_multi.mjs "<query>" <locations.json>'); process.exit(2); }

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
// persistent guest profile: keeps the aws-waf-token warm so only the first
// location pays the WAF-challenge cost. Never logged in.
// PROFILE and PORT are overridable so several sweepers can run in parallel,
// each with its own guest profile + debug port (a profile backs one Chrome).
const PROFILE = process.env.ZEPTO_PROFILE || new URL('../.zepto-guest', import.meta.url).pathname;
const PORT = Number(process.env.ZEPTO_PORT || 9449);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function searchOne({ id: sid, lat, lng }, n) {
  const chrome = spawn(CHROME, [
    `--user-data-dir=${PROFILE}`, `--remote-debugging-port=${PORT}`,
    // headless: the sweep relaunches per store (a process can only resolve one
    // location), and headless keeps every one of those launches invisible -- no
    // window pops up. Works as long as the profile's aws-waf-token is warm.
    '--headless=new', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check',
    '--window-size=1280,800', 'about:blank',
  ], { stdio: 'ignore' });
  // hard-kill and WAIT for the process to fully exit before returning, so the
  // profile's SingletonLock is released before the next store's Chrome launches
  // -- a not-yet-dead instance was causing lock races and hung CDP calls.
  const done = async out => {
    try { chrome.kill('SIGKILL'); } catch {}
    try { await Promise.race([once(chrome, 'exit'), sleep(3000)]); } catch {}
    return out;
  };
  try {
    let target;
    for (let i = 0; i < 50; i++) {
      try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
            target = l.find(t => t.type === 'page'); if (target) break; } catch {}
      await sleep(250);
    }
    if (!target) return await done({ idx: n, id: sid, lat, lng, ok: false, error: 'attach failed' });
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', () => j(new Error('ws open failed'))); });
    let id = 0; const pend = new Map(); const hits = []; let storeId = null; let dead = null;
    const killPending = why => { dead = dead || why; for (const [, p] of pend) p.rej(new Error(why)); pend.clear(); };
    ws.addEventListener('message', e => {
      const m = JSON.parse(e.data);
      if (m.id && pend.has(m.id)) { pend.get(m.id).res(m.result); pend.delete(m.id); }
      if (m.method === 'Network.requestWillBeSent' && /v3\/search$/.test(m.params.request.url)) {
        const h = m.params.request.headers; storeId = h['store_id'] || h['storeId'] || storeId;
      }
      if (m.method === 'Network.responseReceived' && /v3\/search$/.test(m.params.response.url)) hits.push(m.params.requestId);
    });
    ws.addEventListener('close', () => killPending('ws closed'));
    ws.addEventListener('error', () => killPending('ws error'));
    // every CDP call is time-bounded and rejects if the browser dies, so one hung
    // or crashed store becomes a caught error -- never a stalled top-level await
    // that aborts the whole sweep.
    const send = (method, params = {}) => new Promise((res, rej) => {
      if (dead) return rej(new Error(dead));
      const i = ++id;
      const to = setTimeout(() => { pend.delete(i); rej(new Error('cdp timeout: ' + method)); }, 15000);
      pend.set(i, { res: v => { clearTimeout(to); res(v); }, rej: e => { clearTimeout(to); rej(e); } });
      try { ws.send(JSON.stringify({ id: i, method, params })); } catch (e) { clearTimeout(to); pend.delete(i); rej(e); }
    });
    const setC = (name, value) => send('Network.setCookie', { name, value, domain: 'www.zepto.com', path: '/', url: 'https://www.zepto.com/' });
    await send('Network.enable'); await send('Page.enable');
    await send('Network.setUserAgentOverride', { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36' });
    // Drop any sticky store the profile cached for a previous location, so a slow
    // or failed resolution can't silently fall back to the wrong city's store
    // (that was turning Noida coords into Delhi data).
    const delC = (name) => send('Network.deleteCookies', { name, domain: 'www.zepto.com', path: '/', url: 'https://www.zepto.com/' });
    await delC('prev_store_id'); await delC('serviceability');
    await setC('latitude', String(lat));
    await setC('longitude', String(lng));
    await setC('user_position', encodeURIComponent(JSON.stringify({ latitude: +lat, longitude: +lng })));
    // Seed the authoritative location BEFORE any page script runs -- on every
    // document -- so the app always boots on the target coords, never a persisted
    // one. _hasHydrated makes the store treat it as a real, settled selection.
    await send('Page.addScriptToEvaluateOnNewDocument', { source:
      `try{localStorage.setItem('user-position',JSON.stringify({state:{userPosition:{latitude:${+lat},longitude:${+lng},userGpsCoords:null},_hasHydrated:true},version:0}));}catch(e){}` });
    await send('Page.navigate', { url: 'https://www.zepto.com/' });
    await sleep(3400);
    await send('Page.navigate', { url: `https://www.zepto.com/search?query=${encodeURIComponent(query)}` });
    let body = '';
    for (let attempt = 0; attempt < 2 && !body; attempt++) {
      if (attempt) await send('Page.navigate', { url: `https://www.zepto.com/search?query=${encodeURIComponent(query)}&_r=${attempt}` });
      for (let t = 0; t < 14 && !body; t++) {
        await sleep(950);
        for (const rid of hits) {
          try { const r = (await send('Network.getResponseBody', { requestId: rid })).body || '';
                if (/SHOW_ALL_RESULTS|totalProductCount|productResponse/.test(r) && r.length > body.length) body = r; } catch {}
        }
      }
    }
    try { ws.close(); } catch {}
    return await done(body ? { idx: n, id: sid, lat, lng, resolved: storeId, ok: true, body }
                           : { idx: n, id: sid, lat, lng, resolved: storeId, ok: false, error: 'no results captured' });
  } catch (e) {
    return await done({ idx: n, id: sid, lat, lng, ok: false, error: String(e).slice(0, 120) });
  }
}

for (let n = 0; n < locs.length; n++) {
  const out = await searchOne(locs[n], n);
  process.stdout.write(JSON.stringify(out) + '\n');
  await sleep(800);
}
