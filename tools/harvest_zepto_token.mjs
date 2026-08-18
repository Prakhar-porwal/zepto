// Harvest a fresh Zepto accessToken from a real, logged-in Chrome.
//
// Zepto's edge (AWS WAF) blocks headless/bot clients and its accessToken lasts
// only an hour, so instead of forging anything we let an actual browser hold the
// session -- it passes the WAF and renews its own token -- and just read the
// current token out of its cookie jar. First run: a Chrome window opens, you log
// in once. Later runs reuse that persistent profile and harvest silently.
//
// Prints one JSON line: {"accessToken": "...", "refreshToken": "..."} on success.
// Exit 3 means "no valid token yet -- log in in the window that opened".

import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..');
const PROFILE = path.join(ROOT, '.zepto-chrome');          // persistent login profile
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9422;
const DEADLINE_MS = Number(process.env.ZEPTO_LOGIN_TIMEOUT || 180000);

const log = (...a) => console.error('[harvest]', ...a);   // stderr: stdout is reserved for the JSON
const sleep = ms => new Promise(r => setTimeout(r, ms));

function tokenExpiry(jwt) {
  try {
    const payload = jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(Buffer.from(payload, 'base64').toString()).exp || 0;
  } catch { return 0; }
}

mkdirSync(PROFILE, { recursive: true });
// On a scheduled top-up the login already exists, so shove the window far
// off-screen and small -- it opens, harvests, and closes within seconds without
// stealing the foreground. The first, interactive login pass overrides this so
// you can actually see the page to sign in.
const OFFSCREEN = process.env.ZEPTO_VISIBLE !== '1';
const chrome = spawn(CHROME, [
  `--user-data-dir=${PROFILE}`,
  `--remote-debugging-port=${PORT}`,
  '--no-first-run', '--no-default-browser-check',
  OFFSCREEN ? '--window-position=-3200,-3200' : '--window-position=120,80',
  OFFSCREEN ? '--window-size=500,400' : '--window-size=1200,900',
  'https://www.zepto.com/',
], { stdio: 'ignore' });

let target;
for (let i = 0; i < 60; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    target = list.find(t => t.type === 'page'); if (target) break;
  } catch {}
  await sleep(300);
}
if (!target) { log('could not attach to Chrome'); chrome.kill(); process.exit(2); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0; const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
await send('Network.enable');

log('watching for a valid Zepto login (a Chrome window is open — log in if prompted)…');
const started = Date.now();
while (Date.now() - started < DEADLINE_MS) {
  const { cookies = [] } = await send('Network.getCookies', { urls: ['https://www.zepto.com', 'https://bff-gateway.zepto.com'] });
  const jar = Object.fromEntries(cookies.map(c => [c.name, c.value]));
  const access = jar.accessToken;
  if (access && tokenExpiry(access) * 1000 > Date.now() + 60000) {
    process.stdout.write(JSON.stringify({ accessToken: access, refreshToken: jar.refreshToken || null }) + '\n');
    ws.close(); chrome.kill();
    process.exit(0);
  }
  await sleep(2500);
}
log('timed out with no valid token. Log in at zepto.com in the open window, then re-run.');
ws.close(); chrome.kill();
process.exit(3);
