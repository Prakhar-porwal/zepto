// Browser-driven Zepto phone search. Zepto's full-search endpoint signs each
// request over the query, so it can't be replayed by curl for an arbitrary
// term -- but the real (logged-in) browser signs it natively. So we drive the
// browser to search, intercept the results response, and print it as JSON.
//
//   node tools/zepto_phone.mjs "oppo k14x"
//
// Prints the raw SHOW_ALL_RESULTS response body to stdout (JSON) for the store
// the logged-in profile resolves to. Exit 3 = no results captured (log in).

import { spawn } from 'node:child_process';

const query = process.argv[2];
if (!query) { console.error('usage: node tools/zepto_phone.mjs "<phone query>"'); process.exit(2); }

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PROFILE = '/Users/prakharporwal/Downloads/traker/.zepto-chrome';
const PORT = 9441;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const chrome = spawn(CHROME, [
  `--user-data-dir=${PROFILE}`, `--remote-debugging-port=${PORT}`,
  '--no-first-run', '--no-default-browser-check',
  '--window-position=-3200,-3200', '--window-size=500,400',
  `https://www.zepto.com/search?query=${encodeURIComponent(query)}`,
], { stdio: 'ignore' });

let target;
for (let i = 0; i < 60; i++) {
  try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
        target = l.find(t => t.type === 'page'); if (target) break; } catch {}
  await sleep(300);
}
if (!target) { console.error('could not attach to Chrome'); chrome.kill(); process.exit(2); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0; const pend = new Map(); const hits = [];
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); }
  if (m.method === 'Network.responseReceived' && /v3\/search$/.test(m.params.response.url)) hits.push(m.params.requestId);
});
const send = (method, params = {}) => new Promise(res => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
await send('Network.enable');

// wait for the full-results response to arrive (retry a little for slow loads)
let body = '';
for (let t = 0; t < 8 && !body; t++) {
  await sleep(1500);
  for (const rid of hits) {
    try {
      const r = (await send('Network.getResponseBody', { requestId: rid })).body || '';
      if (/SHOW_ALL_RESULTS|productResponse|totalProductCount/.test(r) && r.length > body.length) body = r;
    } catch {}
  }
}
ws.close(); chrome.kill();
if (!body) { console.error('no results captured — is the profile logged in?'); process.exit(3); }
process.stdout.write(body);
