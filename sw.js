/* RoamDial GitHub Pages 版 Service Worker
 * 纯静态托管（GitHub Pages）下代理原站的全部动态接口：
 *  - /api/globe  -> 本地数据快照
 *  - /api/stations/<id> -> Radio Browser 实时聚合
 *  - /api/lyrics -> lrclib 歌词
 *  - /api/now / /api/songclock -> 电台流 ICY 元数据
 *  - /api/identify -> 静态托管无法提供音频指纹识别，返回 503
 */
'use strict';

const VERSION = 'rd-static-v1';
const STATIC = ['index.html', 'about.html', 'privacy.html', 'manifest.webmanifest', 'favicon.svg', 'apple-touch-icon.png', 'icon-192.png', 'icon-512.png', 'og.png', 'land-dots.json', 'api_core_cn.json', 'api_rest.json'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

/* ---------- 工具 ---------- */
const RB_HOSTS = ['de1.api.radio-browser.info', 'de2.api.radio-browser.info', 'fi1.api.radio-browser.info', 'at1.api.radio-browser.info'];

async function rbFetch(p, timeoutMs = 9000) {
  let lastErr = null;
  for (const h of RB_HOSTS) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), timeoutMs);
      const r = await fetch(`https://${h}/json/${p}`, { signal: ctl.signal, headers: { 'User-Agent': 'RoamDialStatic/1.0' } });
      clearTimeout(t);
      if (r.ok) return await r.json();
      lastErr = new Error('rb ' + r.status);
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error('radio browser unreachable');
}

function normUrl(u) {
  if (!u) return '';
  let s = (u || '').trim();
  if (/^http:\/\//i.test(s)) s = 'https://' + s.slice(7);
  return s.replace(/\/+$/, '');
}

async function probeStream(url, timeoutMs = 6000) {
  const t0 = Date.now();
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    const r = await fetch(url, { method: 'GET', headers: { Range: 'bytes=0-1023', 'User-Agent': 'Mozilla/5.0' }, redirect: 'follow', signal: ctl.signal });
    clearTimeout(t);
    const ms = Date.now() - t0;
    if (r.ok || r.status === 206) { await (r.body && r.body.cancel ? r.body.cancel() : Promise.resolve()).catch(() => {}); return { ok: true, ms }; }
    await (r.body && r.body.cancel ? r.body.cancel() : Promise.resolve()).catch(() => {});
    return { ok: false, ms };
  } catch (e) { return { ok: false, ms: Date.now() - t0 }; }
}

/* 电台缓存（5 分钟） */
const stationCache = new Map();
async function getStation(shortId) {
  const hit = stationCache.get(shortId);
  if (hit && Date.now() - hit.t < 5 * 60 * 1000) return hit.v;
  const rest = await (await fetch('api_rest.json')).json();
  const i = rest.id.indexOf(shortId);
  if (i < 0) return null;
  const name = rest.name[i];
  const state = rest.state[i];
  const tz = rest.tz[i];

  let list = null;
  try { list = await rbFetch(`stations/byname/${encodeURIComponent(name)}`, 12000); }
  catch (e) {
    try { list = await rbFetch(`stations/search?name=${encodeURIComponent(name)}&hidebroken=true`, 12000); }
    catch (e2) { return null; }
  }
  if (!Array.isArray(list) || !list.length) return null;

  const byVotes = list.slice().sort((a, b) => (b.votes || 0) - (a.votes || 0));
  const main = byVotes[0];
  const seen = new Set();
  const cands = [];
  for (const s of byVotes) {
    for (const u of [normUrl(s.url_resolved || s.url), normUrl(s.url)]) {
      if (u && !seen.has(u)) { seen.add(u); cands.push({ u, s }); }
    }
  }
  const snapUrl = normUrl(rest.url[i]);
  if (snapUrl && !seen.has(snapUrl)) { seen.add(snapUrl); cands.unshift({ u: snapUrl, s: main }); }

  const probes = await Promise.all(cands.slice(0, 6).map((c) => probeStream(c.u).then((p) => ({ c, p }))));
  const streams = probes.map(({ c, p }) => ({
    url: c.u,
    codec: (c.s.codec || 'UNKNOWN').toUpperCase(),
    br: c.s.bitrate || 0,
    hls: !!(c.s.hls) || /\.m3u8($|\?)/i.test(c.u),
    cors: true,
    https: /^https:\/\//i.test(c.u),
    ok: p.ok,
    ms: p.ms,
  }));

  const v = {
    id: shortId,
    uuid: main.stationuuid || '',
    name: main.name || name,
    homepage: main.homepage || '',
    favicon: main.favicon || '',
    country: main.country || '',
    state: main.state || state || '',
    tags: main.tags || '',
    language: main.language || '',
    tz,
    streams,
  };
  stationCache.set(shortId, { t: Date.now(), v });
  return v;
}

/* ---------- ICY 元数据 ---------- */
async function readIcy(url, timeoutMs = 10000) {
  if (/\.m3u8($|\?)/i.test(url)) return { icy: false, title: '' };
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    const r = await fetch(url, { headers: { 'Icy-MetaData': '1', 'User-Agent': 'Mozilla/5.0' }, redirect: 'follow', signal: ctl.signal });
    const metaint = parseInt(r.headers.get('icy-metaint') || '0', 10);
    if (!metaint || !r.body) { clearTimeout(t); return { icy: false, title: '' }; }
    const reader = r.body.getReader();
    const chunk = new Uint8Array(metaint + 16 * 255);
    let got = 0;
    while (got < chunk.length) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        const n = Math.min(chunk.length - got, value.length);
        chunk.set(value.subarray(0, n), got);
        got += n;
        if (n === 0) break;
      }
    }
    reader.cancel().catch(() => {});
    clearTimeout(t);
    if (got < metaint + 1) return { icy: false, title: '' };
    const len = chunk[metaint] * 16;
    if (!len) return { icy: false, title: '' };
    const meta = new TextDecoder('latin1').decode(chunk.subarray(metaint + 1, metaint + 1 + len));
    const m = meta.match(/StreamTitle='([^']*)'/);
    return { icy: true, title: m ? m[1] : '', icyName: r.headers.get('icy-name') };
  } catch (e) { return { icy: false, title: '' }; }
}

/* ---------- lrclib ---------- */
function parseLrc(text) {
  const lines = [];
  const re = /\[(\d+):(\d+(?:\.\d+)?)\](.*)/g;
  let m;
  while ((m = re.exec(text))) {
    const sec = parseInt(m[1], 10) * 60 + parseFloat(m[2]);
    const txt = m[3].trim();
    if (txt) lines.push([sec, txt]);
  }
  return lines;
}
async function lrclibSearch(trackName, artistName, timeoutMs = 8000) {
  const q = new URLSearchParams({ track_name: trackName || '', artist_name: artistName || '' });
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    const r = await fetch(`https://lrclib.net/api/search?${q}`, { signal: ctl.signal, headers: { 'Lrclib-Client': 'RoamDialStatic/1.0' } });
    clearTimeout(t);
    if (!r.ok) return null;
    const arr = await r.json();
    if (!Array.isArray(arr) || !arr.length) return null;
    arr.sort((a, b) => (b.syncedLyrics ? 1 : 0) - (a.syncedLyrics ? 1 : 0));
    const best = arr[0];
    const synced = !!(best.syncedLyrics && best.syncedLyrics.length);
    const lines = synced ? parseLrc(best.syncedLyrics) : [];
    return { title: best.trackName || trackName, artist: best.artistName || artistName, synced, lines, plain: synced ? '' : (best.plainLyrics || '').split('\n').map((t2) => t2.trim()).filter(Boolean) };
  } catch (e) { return null; }
}

/* ---------- 响应工具 ---------- */
function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...extra } });
}
function text(body, status = 200, type = 'text/plain; charset=utf-8') {
  return new Response(body, { status, headers: { 'Content-Type': type, 'Cache-Control': 'no-store' } });
}

/* ---------- 请求分发 ---------- */
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method === 'POST') {
    const u = new URL(req.url);
    if (u.pathname.startsWith('/api/log') || u.pathname.startsWith('/api/report')) { e.respondWith(text('', 204)); return; }
    if (u.pathname.startsWith('/api/identify')) { e.respondWith(json({ error: 'identify disabled', found: false }, 503)); return; }
  }
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const p = url.pathname;

  /* API: globe 快照 */
  if (p === '/api/globe') {
    const part = url.searchParams.get('part');
    const file = part === 'rest' ? 'api_rest.json' : 'api_core_cn.json';
    e.respondWith(fetch(file).then((r) => r.ok ? jsonP(r) : json({ error: 'snapshot missing' }, 500)));
    return;
  }
  /* 静态路径 rewrite（JS 里原站绝对路径） */
  if (p === '/land-dots.json') { e.respondWith(fetch('land-dots.json').then((r) => r.ok ? r : json({ error: 'missing' }, 500))); return; }
  if (p === '/about.html') { e.respondWith(fetch('about.html')); return; }
  if (p === '/privacy.html') { e.respondWith(fetch('privacy.html')); return; }
  if (p === '/favicon.svg') { e.respondWith(fetch('favicon.svg')); return; }
  if (p === '/apple-touch-icon.png') { e.respondWith(fetch('apple-touch-icon.png')); return; }
  if (p === '/icon-512.png') { e.respondWith(fetch('icon-512.png')); return; }
  if (p === '/icon-192.png') { e.respondWith(fetch('icon-192.png')); return; }
  if (p === '/og.png') { e.respondWith(fetch('og.png')); return; }
  if (p === '/manifest.webmanifest') { e.respondWith(fetch('manifest.webmanifest')); return; }
  if (/^\/flags\/[a-z]{2}\.svg$/.test(p)) { e.respondWith(fetch('.' + p)); return; }

  /* API: stations */
  let m = p.match(/^\/api\/stations\/([^/]+)$/);
  if (m) {
    e.respondWith(getStation(decodeURIComponent(m[1])).then((st) => st ? json(st) : json({ error: 'no such station' }, 404)));
    return;
  }
  /* API: now */
  m = p.match(/^\/api\/now\/([^/]+)$/);
  if (m) {
    const u = url.searchParams.get('u') || '';
    if (!u) { e.respondWith(json({ error: 'missing u' }, 400)); return; }
    e.respondWith(readIcy(u).then((icy) => json(icy)));
    return;
  }
  /* API: songclock */
  m = p.match(/^\/api\/songclock\/([^/]+)$/);
  if (m) {
    const u = url.searchParams.get('u') || '';
    if (!u) { e.respondWith(json({ error: 'missing u' }, 400)); return; }
    e.respondWith(
      (async () => {
        const icy = await readIcy(u);
        if (!icy.icy || !icy.title) return json({ error: '没有这条线路' }, 404);
        const parts = icy.title.split(' - ');
        const artist = parts.length > 1 ? parts.slice(0, -1).join(' - ') : '';
        const title = parts.length > 1 ? parts[parts.length - 1] : icy.title;
        const lrc = await lrclibSearch(title, artist);
        if (!lrc || (!lrc.synced && !lrc.lines.length)) return json({ error: '没有歌词', partial: true }, 404);
        const now = Date.now();
        return json({ lines: lrc.lines, synced: lrc.synced, startedAt: now, now, title: icy.title, source: 'icy' });
      })()
    );
    return;
  }
  /* API: lyrics */
  if (p === '/api/lyrics') {
    const q = url.searchParams.get('q') || '';
    if (!q) { e.respondWith(json({ error: 'missing q' }, 400)); return; }
    const parts = q.split(' - ');
    const artist = parts.length > 1 ? parts.slice(0, -1).join(' - ') : '';
    const title = parts.length > 1 ? parts[parts.length - 1] : q;
    e.respondWith(lrclibSearch(title, artist).then((lrc) => lrc ? json(lrc) : json({ error: 'not found', partial: true }, 404)));
    return;
  }

  /* 其余静态资源：走缓存优先 */
  if (req.mode === 'navigate' || p === '/' || p.endsWith('.html') || p === '' ) {
    e.respondWith(fetch(req).then((r) => r.ok ? r : fetch('index.html')));
    return;
  }
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((r) => {
      if (r.ok) {
        const cp = r.clone();
        caches.open(VERSION).then((c) => c.put(req, cp));
      }
      return r;
    }).catch(() => hit))
  );
});

/* globe 快照直接透传 JSON（保持原样） */
async function jsonP(r) {
  const b = await r.arrayBuffer();
  return new Response(b, { status: 200, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' } });
}
