/* ==========================================================================
   Live console — bot control, token price chart, trades.

   The page only ever READS the bot's state file and POSTs control actions; it
   never drives the trading loop itself. The bot runs as its own process so a
   hung websocket cannot take the dashboard down with it.

   Charts are hand-rolled SVG so the mark specs stay exact: 2px lines, >=8px
   markers with a 2px surface ring, hairline solid gridlines, <=24px bars with a
   4px rounded data-end.

   Colour: UP and DOWN are categorical slots 1 and 2 from the validated palette
   (adjacent CVD dE 24.7 light / 26.8 dark). The hue is bound to the OUTCOME,
   never to which line happens to be higher.

   Cadence: the chart polls at 200ms. The bot samples at 200ms and writes the
   state file at 200ms, and the server trims the response to the ONE market
   being drawn (`?chart=`), because every market's full 5-minute history five
   times a second is megabytes for nothing. Tables rebuild only when their own
   data changes, so a text selection is not wiped five times a second.
   ========================================================================== */

'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* A market's length comes from the market. The venue lists 5m and 15m windows
   and the bot can run either or both, so a constant here silently broke the
   console at 15m: the picker filtered every market out (753s left is not
   <= 2 * 300), nothing was selected, and with nothing selected the server
   sends no price history at all -- no chart, no spot price. The backend
   publishes `window_s` per market; the slug is the fallback for a payload
   written before it did. */
const DEFAULT_WINDOW_S = 300;

function windowForSlug(slug) {
  const m = /-updown-(\d+)m-/.exec(String(slug || ''));
  return m ? Number(m[1]) * 60 : DEFAULT_WINDOW_S;
}

function windowOf(market) {
  if (market && Number(market.window_s) > 0) return Number(market.window_s);
  return windowForSlug(market && market.slug);
}

/* The window of the market the chart is drawing. */
function selectedWindowS(slug = state.selected) {
  const m = (state.live.live_markets || []).find((x) => x.slug === slug);
  return m ? windowOf(m) : windowForSlug(slug);
}

const state = {
  live: null,
  bot: null,
  page: 'market',        // which tab is open; gates the per-tick work
  selected: null,        // slug shown in the price chart
  timer: null,
  //: earnings card: which calendar period, and whose money. ?period= and
  //  ?scope= make a particular view linkable, the same way ?chart= does.
  earn: (() => {
    const q = new URLSearchParams(location.search);
    const period = q.get('period'), scope = q.get('scope');
    return {
      period: ['day', 'week', 'month'].includes(period) ? period : 'day',
      scope: ['all', 'paper', 'live'].includes(scope) ? scope : 'all',
      data: null,
    };
  })(),
  //: draw the strike distance on the token chart (as implied P(up)) instead
  //  of in its own panel. ?overlay=1|0 in the URL wins; else remembered.
  overlay: (() => {
    const q = new URLSearchParams(location.search).get('overlay');
    if (q === '1' || q === '0') return q === '1';
    try { return localStorage.getItem('tpb-overlay') === '1'; } catch { return false; }
  })(),
};

function applyOverlayLayout() {
  const on = state.overlay;
  $('#overlay-toggle').checked = on;
  $('#strike-panel').hidden = on;
  $('#price-panel').classList.toggle('price-col-wide', on);
  $('#price-panel-title').textContent = on ? 'Token price with strike distance' : 'Token price';
}

const fmt = {
  /* "15:45:00 – 16:00:00": a window's open and close on the UTC clock, from
     the bot's own timestamps. Falls back to the clock + seconds-left for a
     payload written before the bot published them. */
  window: (m) => {
    const hms = (ms) => new Date(ms).toISOString().slice(11, 19);
    if (m && m.open_ts && m.close_ts) return `${hms(m.open_ts)} – ${hms(m.close_ts)}`;
    if (m && m.secs_left != null) {
      const close = Date.now() + m.secs_left * 1000, win = (Number(m.window_s) || 300) * 1000;
      return `${hms(close - win)} – ${hms(close)}`;
    }
    return '—';
  },
  money: (v) => (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  signed: (v) => (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  pct: (v) => (v * 100).toFixed(1) + '%',
  num: (v, d = 0) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }),
  ms: (v) => Math.round(v) + ' ms',
  cents: (v) => v.toFixed(3),
  clock: (ms) => new Date(ms).toLocaleTimeString(),
  stamp: (ms) => new Date(ms).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
  // holds here run from under a second to a whole window, so seconds stay
  // seconds rather than becoming an unreadable 0:06
  dur: (s) => (s < 60 ? `${s < 10 ? s.toFixed(1) : Math.round(s)}s`
    : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s`),
  mkt: (slug) => (slug || '').replace('-updown-5m-', ' '),
  mmss: (s) => {
    const t = Math.max(0, Math.round(s));
    return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
  },
};

/* ═════════════════════════════════ theme ═════════════════════════════════ */

function initTheme() {
  // ?theme=dark|light wins, the same way ?overlay= does. It makes both themes
  // reachable without touching stored state, which is what a screenshot or a
  // second window comparing the two needs.
  const q = new URLSearchParams(location.search).get('theme');
  const stored = (() => { try { return localStorage.getItem('tpb-theme'); } catch { return null; } })();
  const pick = (q === 'dark' || q === 'light') ? q : stored;
  if (pick === 'dark' || pick === 'light') document.documentElement.dataset.theme = pick;
  themeLabel();
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tpb-theme', next); } catch { /* private mode */ }
    themeLabel();
    render(true);
  });
}
function themeLabel() {
  const dark = (document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  $('[data-theme-label]').textContent = dark ? 'Light' : 'Dark';
}

/* ══════════════════════════════ bot control ══════════════════════════════ */

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

function notice(msg, isError = true) {
  const box = $('#notice');
  if (!msg) { box.hidden = true; return; }
  box.textContent = msg;
  box.hidden = false;
  box.dataset.kind = isError ? 'error' : 'info';
}

function botArgs() {
  return {
    balance: Number($('#bot-balance').value) || 100,
    assets: ($('#bot-assets').value || 'BTC,ETH').trim(),
  };
}

async function control(action) {
  const buttons = ['#btn-start', '#btn-stop', '#btn-restart'];
  buttons.forEach((b) => { $(b).disabled = true; });
  notice('');
  try {
    const res = await post(`/api/bot/${action}`, botArgs());
    if (!res.ok && res.reason) notice(res.reason);
  } catch (e) {
    notice('Could not reach the server: ' + e);
  } finally {
    buttons.forEach((b) => { $(b).disabled = false; });
    poll();
  }
}

function renderBotStatus(b) {
  const badge = $('#bot-badge');
  const running = b && b.running;
  badge.dataset.state = running ? (b.managed ? 'running' : 'external') : 'stopped';
  $('#bot-state').textContent = !running ? 'stopped'
    : b.managed ? `running · ${Math.round((b.uptime_s || 0) / 60)}m`
    : 'running (external)';
  $('#btn-start').disabled = !!running;
  $('#btn-stop').disabled = !running;
  $('#btn-restart').disabled = !!(running && !b.managed);
  if (b && b.error) notice(b.error);
}

/* ═══════════════════════════════ polling ════════════════════════════════ */

/* Two cadences. The chart wants 200ms data; process status does not change
   five times a second and its endpoint is the one that can block on a
   subprocess handle, so it gets its own slower timer. */

let inFlight = false;

/* The server sends only the points we do not have yet, so the drawn history
   is accumulated here rather than re-downloaded every tick. Without this a
   100 ms poll would pull ~375 KB each time; with it a steady tick is ~1 KB. */
let histCache = { slug: null, pts: [] };
const HISTORY_CAP = 2000;               // matches LiveBot.HISTORY_POINTS

/* Apply one live payload, whichever transport carried it. The websocket and
   the polling fallback send byte-identical messages, so this is the only place
   that understands the shape. */
function applyLive(live, slug) {
  const usable = live && live.running !== false && live.equity != null;
  if (usable) {
    if (!slug) {
      histCache = { slug: null, pts: [] };
    } else {
      const incoming = (live.price_history || {})[slug] || [];
      // append only when this is a delta for the market we already hold;
      // a switched market or a full resend replaces what we had
      if (histCache.slug === slug && live.history_partial) {
        histCache.pts = histCache.pts.concat(incoming);
        if (histCache.pts.length > HISTORY_CAP) {
          histCache.pts = histCache.pts.slice(-HISTORY_CAP);
        }
      } else {
        histCache = { slug, pts: incoming };
      }
      live.price_history = { [slug]: histCache.pts };
    }
  }
  state.live = usable ? live : null;
  $('#idle').hidden = !!usable;
  $('#live').hidden = !usable;
  if (usable) render();
}

async function pollLive() {
  if (wsLive && wsLive.readyState === WebSocket.OPEN) return;   // push is driving
  if (inFlight) return;                 // never stack requests on a slow link
  inFlight = true;
  try {
    // only the market being drawn comes back with its history, and only the
    // part of it we are missing; everything else is a count in history_meta
    const slug = state.selected;
    let q = '';
    if (slug) {
      q = `?chart=${encodeURIComponent(slug)}`;
      if (histCache.slug === slug && histCache.pts.length) {
        q += `&since=${histCache.pts[histCache.pts.length - 1].t}`;
      }
    }
    const live = await fetch('/api/live' + q).then((r) => r.json()).catch(() => null);
    applyLive(live, slug);
  } catch { /* transient; next tick retries */ } finally {
    inFlight = false;
  }
}

/* ─────────────────────────── the push transport ───────────────────────────
   The bot writes state at 10 Hz and the server pushes each write down this
   socket, so the chart redraws when the data changes rather than when a timer
   happens to fire. Polling stays wired as the fallback: if the socket will not
   open or drops, `pollLive` takes over on the next tick without the page
   noticing, and the payloads are identical either way. */
let wsLive = null;
let wsRetry = 0;

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const slug = state.selected;
  return `${proto}//${location.host}/ws` +
         (slug ? `?chart=${encodeURIComponent(slug)}` : '');
}

function connectLive() {
  let sock;
  try {
    sock = new WebSocket(wsUrl());
  } catch {
    return;                             // polling is already running
  }
  wsLive = sock;
  sock.onopen = () => { wsRetry = 0; };
  sock.onmessage = (ev) => {
    try {
      applyLive(JSON.parse(ev.data), state.selected);
    } catch { /* one bad frame must not stop the stream */ }
  };
  sock.onclose = () => {
    if (wsLive === sock) wsLive = null;
    // back off to a few seconds so a server restart does not get hammered;
    // pollLive is serving the page in the meantime
    wsRetry = Math.min(wsRetry + 1, 5);
    setTimeout(connectLive, 500 * wsRetry);
  };
  sock.onerror = () => { try { sock.close(); } catch { /* already gone */ } };
}

/* Tell the socket which market to stream. Cheap enough to call on every
   selection change; no reconnect, and the server answers with a full history
   for the new market rather than a delta against the old one's timestamps. */
function setSelected(slug) {
  if (state.selected === slug) return;
  state.selected = slug;
  histCache = { slug: null, pts: [] };   // the new market shares no points
  sendChartSelection();
}

function sendChartSelection() {
  if (wsLive && wsLive.readyState === WebSocket.OPEN) {
    try {
      wsLive.send(JSON.stringify({ chart: state.selected || null }));
    } catch { /* the close handler will reconnect */ }
  }
}

/* ───────────────────── real Polymarket account (read only) ─────────────────
   A separate panel on purpose. It reports the venue's own numbers for one
   wallet, fetched from the public Data API; it shares nothing with the paper
   bot above and can place no orders. Refreshed on a slow timer because the
   endpoint is public and free -- the server caches it too. */
const ACCOUNT_POLL_MS = 15000;
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function renderAccount(a) {
  const card = $('#account-card');
  if (!a || a.configured === false) { card.hidden = true; return; }
  card.hidden = false;

  const stale = a.age_s != null && a.age_s > 60;
  $('#account-age').textContent = a.error ? `stale · ${esc(a.error)}`
    : (a.age_s == null ? '' : `updated ${a.age_s.toFixed(0)}s ago`);
  $('#account-age').className = 'badge';

  const pos = a.positions || [];
  const kpis = [
    { l: 'Portfolio value', v: a.value == null ? '—' : fmt.money(a.value),
      n: a.value_error ? esc(a.value_error) : `wallet ${esc((a.wallet || '').slice(0, 10))}…` },
    { l: 'Open positions', v: fmt.num(pos.length),
      n: a.positions_error ? esc(a.positions_error) : `${fmt.money(a.positions_value || 0)} at market` },
    { l: 'Unrealised', v: fmt.signed(a.unrealized_pnl || 0), n: 'on open positions' },
    { l: 'Trades', v: fmt.num((a.trades || []).length),
      n: a.trades_error ? esc(a.trades_error) : 'most recent first' },
  ];
  $('#account-kpis').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  const pcols = ['Market', 'Outcome', 'Shares', 'Avg', 'Now', 'Value', 'Unrealised'];
  $('#account-positions').innerHTML = pos.length
    ? '<thead><tr>' + pcols.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + pos.map((p) => `<tr>
          <td>${esc(p.title)}</td><td>${esc(p.outcome)}</td>
          <td>${fmt.num(p.size, 2)}</td>
          <td>${p.avg_price == null ? '—' : p.avg_price.toFixed(3)}</td>
          <td>${p.current_price == null ? '—' : p.current_price.toFixed(3)}</td>
          <td>${fmt.money(p.value || 0)}</td>
          <td class="${(p.unrealized_pnl || 0) >= 0 ? 'pos' : 'neg'}">${fmt.signed(p.unrealized_pnl || 0)}</td>
        </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No open positions.</td></tr></tbody>';

  const tcols = ['Time', 'Side', 'Market', 'Outcome', 'Shares', 'Price', 'Notional'];
  const trades = (a.trades || []).slice(0, 50);
  $('#account-trades').innerHTML = trades.length
    ? '<thead><tr>' + tcols.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + trades.map((t) => `<tr>
          <td>${fmt.clock((t.ts || 0) * 1000)}</td>
          <td class="${t.side === 'BUY' ? 'pos' : 'neg'}">${esc(t.side)}</td>
          <td>${esc(t.title)}</td><td>${esc(t.outcome)}</td>
          <td>${fmt.num(t.size, 2)}</td><td>${(t.price || 0).toFixed(3)}</td>
          <td>${fmt.money(t.usdc || 0)}</td>
        </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No trades on this wallet yet.</td></tr></tbody>';
}

async function pollAccount() {
  if (state.page !== 'ledger') return;
  try {
    const a = await fetch('/api/account').then((r) => r.json()).catch(() => null);
    renderAccount(a);
  } catch { /* the panel keeps its last render */ }
}

async function pollBot() {
  try {
    const bot = await fetch('/api/bot').then((r) => r.json()).catch(() => null);
    state.bot = bot;
    renderBotStatus(bot);
  } catch { /* transient */ }
}

async function poll() {
  await Promise.all([pollBot(), pollLive()]);
}

/* ══════════════════════════════ svg helpers ═════════════════════════════ */

const NS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
function mount(host, height) {
  host.innerHTML = '';
  const w = Math.max(host.clientWidth || 600, 260);
  const svg = el('svg', { width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'img' });
  host.appendChild(svg);
  return { svg, w, h: height };
}
function barPath(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w, h / 2));
  return `M${x},${y} H${x + w - rr} A${rr},${rr} 0 0 1 ${x + w},${y + rr}`
       + ` V${y + h - rr} A${rr},${rr} 0 0 1 ${x + w - rr},${y + h} H${x} Z`;
}

const tip = $('#tooltip');
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => { tip.hidden = true; };
const tipRows = (title, rows) =>
  `<div class="tt-title">${title}</div>` + rows.map(([k, v, c]) => `
    <div class="tt-row"><span class="tt-key">${
      c ? `<span class="tt-swatch" style="background:${c}"></span>` : ''}${k}</span>
    <span class="tt-val">${v}</span></div>`).join('');

function legend(host, items) {
  host.innerHTML = '';
  items.forEach((it) => {
    const s = document.createElement('span');
    s.className = 'legend-item';
    s.innerHTML = `<span class="legend-key" style="background:${it.color}"></span>${it.label}`;
    host.appendChild(s);
  });
}

/* ═════════════════════ the fixed axis, one window wide ══════════════════ */

/* x is elapsed time into the window, not sample index. Mapping x to the
   sample index made every new point recompress the whole line leftward; a
   fixed 0..window domain lets the line grow rightward and stay put. Points
   before the open are not drawn; post-close samples (left clamps to 0) sit at
   the right edge.

   `win` is the window length in seconds -- 300 or 900. It defaults to the
   selected market's so a caller that draws the current chart need not pass it,
   but the offscreen export of a CLOSED market must, because that market is no
   longer the selected one. */

const elapsedOf = (p, win = selectedWindowS()) =>
  win - Math.max(0, Math.min(win, p.left));
const inWindow = (p, win = selectedWindowS()) => p.left <= win;

/* Tick every minute at 5m, every three at 15m: six labels either way. */
function axisStepS(win) {
  return win <= 360 ? 60 : Math.round(win / 5 / 60) * 60;
}

function fixedAxis(svg, m, pw, H, win = selectedWindowS()) {
  const X = (p) => m.l + (elapsedOf(p, win) / win) * pw;
  const Xe = (elapsed) => m.l + (elapsed / win) * pw;
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: m.t + (H - m.t - m.b), y2: m.t + (H - m.t - m.b) }));
  const step = axisStepS(win);
  for (let k = 0; k * step <= win + 1e-6; k++) {
    const at = k * step;
    const last = at + step > win + 1e-6;
    const lab = el('text', { class: 'axis-text', x: Xe(at), y: H - 12,
      'text-anchor': k === 0 ? 'start' : last ? 'end' : 'middle' });
    lab.textContent = fmt.mmss(win - at);
    svg.appendChild(lab);
  }
  return { X, Xe };
}

/* nearest drawn point to a mouse x, by elapsed time */
function nearestByX(pts, ev, svg, m, pw, win = selectedWindowS()) {
  const bx = svg.getBoundingClientRect();
  const elapsed = Math.max(0, Math.min(win, ((ev.clientX - bx.left - m.l) / (pw || 1)) * win));
  let best = null, bd = Infinity;
  for (const p of pts) {
    const d = Math.abs(elapsedOf(p, win) - elapsed);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}

/* ════════════════════════════ price chart ═══════════════════════════════ */

/* Only the CURRENT and NEXT window per asset. From the bot's own
   exchange-aligned clock, against THAT market's own length: a market with
   more than one window left is the one after; a closed window has 0 and drops
   off the picker rather than lingering until it is reaped.

   Measuring every market against a single 300 excluded 15m markets entirely
   (753s left is not <= 600), which emptied the picker and left the page with
   nothing selected and therefore no chart at all. */
const isNextWindow = (m) => m.secs_left > windowOf(m);

function pickerMarkets() {
  return (state.live.live_markets || [])
    .filter((m) => m.secs_left > 0 && m.secs_left <= 2 * windowOf(m))
    .sort((a, b) => (isNextWindow(a) - isNextWindow(b))
                    || a.slug.localeCompare(b.slug));
}

let pickerSig = '';

function renderPicker() {
  const host = $('#market-picker');
  const markets = pickerMarkets();
  if (!markets.length) { host.innerHTML = ''; pickerSig = ''; return; }
  const meta = state.live.history_meta || {};
  const held = new Set(markets.filter((m) => m.up_shares > 0 || m.down_shares > 0).map((m) => m.slug));
  const slugs = new Set(markets.map((m) => m.slug));

  if (state.selected && !slugs.has(state.selected)) setSelected(null);

  // Default to something worth looking at: a market we hold, else the struck
  // current window closest to resolving, else a current window, else next.
  if (!state.selected) {
    const rank = (m) => {
      if (held.has(m.slug)) return [0, m.secs_left];
      if (m.strike > 0 && !isNextWindow(m)) return [1, m.secs_left];
      if (!isNextWindow(m)) return [2, m.secs_left];
      return [3, m.secs_left];
    };
    setSelected(markets.slice().sort((a, b) => {
      const ra = rank(a), rb = rank(b);
      return ra[0] - rb[0] || ra[1] - rb[1];
    })[0].slug);
  }

  // rebuilding eight buttons five times a second is pointless and eats clicks
  const sig = JSON.stringify(markets.map((m) => [m.slug, isNextWindow(m), held.has(m.slug), (meta[m.slug] || 0) >= 2]))
    + '|' + state.selected;
  if (sig === pickerSig) return;
  pickerSig = sig;

  host.innerHTML = '';
  markets.forEach((m) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip';
    b.dataset.on = m.slug === state.selected ? '1' : '0';
    b.innerHTML = `${fmt.mkt(m.slug)}`
      + (isNextWindow(m) ? '<span class="pending">next</span>' : '')
      + (held.has(m.slug) ? '<span class="held">held</span>' : '');
    b.addEventListener('click', () => { setSelected(m.slug); pollLive(); });
    host.appendChild(b);
  });
}

/* Format a crypto price at a sensible precision for its own magnitude --
   0.01 on BTC is noise, 0.01 on XRP is real. */
function fmtSpot(v) {
  if (v == null) return '—';
  const d = v >= 1000 ? 0 : v >= 100 ? 1 : v >= 1 ? 2 : 4;
  return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

function emptyChart(host, legendHost, forExport, hist, win = selectedWindowS()) {
  if (forExport) return null;
  const opensIn = hist && hist.length ? Math.round(hist[hist.length - 1].left - win) : null;
  host.innerHTML = `<p class="live-empty">${
    opensIn != null && opensIn > 0 ? `Window opens in ${opensIn}s.` : 'Collecting price samples…'}</p>`;
  legend(legendHost, []);
  return null;
}

/* Crypto spot price against the strike -- the left half of the split. Same
   fixed time axis as the token chart (both sampled into the same
   price_history point), but its own price scale since spot lives in dollars,
   not [0,1]. */
function renderSpotChart(slug = state.selected, host = $('#spot-chart'),
                         forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
    || (state.live.live_markets || []).find((x) => x.slug === slug);
  // this market's own window, not the selected one: the export draws a CLOSED
  // market offscreen while a different market is selected
  const win = selectedWindowS(slug);
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p, win));
  if (!pts.length) return emptyChart(host, $('#spot-legend'), forExport, hist, win);

  const cSpot = css('--text-primary'), strike = rec && rec.strike ? rec.strike : null;
  const H = forExport ? 300 : 220, m = { t: 14, r: 64, b: 34, l: 64 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;

  const vals = pts.map((p) => p.spot);
  if (strike) vals.push(strike);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.12 || Math.abs(lo) * 0.001 || 1;
  lo -= pad; hi += pad;
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;

  const ticks = 4;
  for (let k = 0; k <= ticks; k++) {
    const v = lo + ((hi - lo) * k) / ticks;
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = fmtSpot(v);
    svg.appendChild(lab);
  }
  const { X } = fixedAxis(svg, m, pw, H, win);

  // the strike is a reference threshold, not data -- dashed on purpose, to
  // read as "target" rather than as a second series
  if (strike != null && strike > 0) {
    const sy = Y(strike);
    svg.appendChild(el('line', {
      x1: m.l, x2: m.l + pw, y1: sy, y2: sy,
      stroke: css('--axis'), 'stroke-width': 1.5, 'stroke-dasharray': '5 4',
    }));
    const lab = el('text', { class: 'value-label', x: m.l + pw + 8, y: sy + 4 });
    lab.textContent = `strike ${fmtSpot(strike)}`;
    svg.appendChild(lab);
  }

  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p.spot)}`).join(' ');
  svg.appendChild(el('path', { class: 'series-line', d, stroke: cSpot }));
  const last = pts[pts.length - 1];
  const cx = X(last), cy = Y(last.spot);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: cSpot, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const endLab = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  endLab.textContent = fmtSpot(last.spot);
  svg.appendChild(endLab);

  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw, win);
    if (!p) return;
    cross.setAttribute('x1', X(p)); cross.setAttribute('x2', X(p)); cross.setAttribute('opacity', 1);
    const rows = [['Price', fmtSpot(p.spot), cSpot]];
    if (strike) rows.push(['vs strike', `${p.spot >= strike ? '+' : ''}${fmtSpot(p.spot - strike)}`]);
    showTip(tipRows(`${fmt.mmss(p.left)} left`, rows), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });

  if (!forExport) {
    const items = [{ label: 'Price', color: cSpot }];
    if (strike) items.push({ label: 'Strike', color: css('--axis') });
    legend($('#spot-legend'), items);
  }
  return svg;
}

/* Distance from the strike, in basis points, with the +-1 sigma envelope of
   the settling TWAP. This is the pricer's input laid bare: how far the price
   is from the line that decides the window, against how far it is expected
   to be able to move in the time left. Above the strike favours Up (series 1),
   below favours Down (series 2) -- the same hue-to-outcome binding as the
   token chart, so the two panels read as one story. */
/* Standardised Student-t(4) CDF, exactly as pricing/digital.py: the closed
   form for four degrees of freedom, evaluated at z * sqrt(nu / (nu - 2)). */
function t4cdf(z) {
  const t = z * Math.SQRT2;
  return 0.5 + (t * (t * t + 6)) / (2 * Math.pow(t * t + 4, 1.5));
}

function twapSd(left, sigmaBps) {
  const u = Math.max(0, left);
  const v = u >= 60 ? u - 40 : (u * u * u) / 10800;
  return sigmaBps * Math.sqrt(Math.max(v, 0));
}

function renderStrikeChart(slug = state.selected, host = $('#strike-chart'), forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
    || (state.live.live_markets || []).find((x) => x.slug === slug);
  const strike = rec && rec.strike ? rec.strike : null;
  const win = selectedWindowS(slug);
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p, win));
  if (!pts.length || !strike) {
    if (forExport) return null;
    host.innerHTML = `<p class="live-empty">${pts.length ? 'No strike yet for this window.' : 'Collecting price samples…'}</p>`;
    legend($('#strike-legend'), []);
    return null;
  }
  const asset = (slug || '').split('-')[0].toUpperCase();
  const sigma = (state.live.sigma_bps_per_sec || {})[asset] || 0;
  const cUp = css('--series-1'), cDown = css('--series-2'), cBand = css('--axis');
  const bpsOf = (p) => Math.log(p.spot / strike) * 1e4;

  const H = 300, m = { t: 14, r: 64, b: 34, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const bandAt = (p) => twapSd(p.left, sigma);
  let ext = 5;
  pts.forEach((p) => { ext = Math.max(ext, Math.abs(bpsOf(p)), bandAt(p)); });
  ext *= 1.12;
  const Y = (v) => m.t + ph / 2 - (v / ext) * (ph / 2);

  // symmetric gridlines at a round step
  const rawStep = ext / 2.2;
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find((s) => s >= rawStep) || 500;
  for (let v = -Math.floor(ext / step) * step; v <= ext; v += step) {
    if (v === 0) continue;
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = `${v > 0 ? '+' : ''}${v}`;
    svg.appendChild(lab);
  }
  const { X } = fixedAxis(svg, m, pw, H, win);

  // the +-1 sigma envelope: what the settling average can still do
  if (sigma > 0) {
    const top = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(bandAt(p))}`).join(' ');
    const bottom = pts.slice().reverse().map((p) => `L${X(p)},${Y(-bandAt(p))}`).join(' ');
    svg.appendChild(el('path', { class: 'band-fill', d: `${top} ${bottom} Z`, fill: cBand }));
    const last = pts[pts.length - 1];
    const bl = el('text', { class: 'value-label', x: X(last) + 10, y: Y(bandAt(last)) + 4 });
    bl.textContent = `±1σ ${bandAt(last).toFixed(1)}`;
    svg.appendChild(bl);
  }

  // zero is the strike
  svg.appendChild(el('line', { x1: m.l, x2: m.l + pw, y1: Y(0), y2: Y(0),
    stroke: css('--axis'), 'stroke-width': 1.5, 'stroke-dasharray': '5 4' }));
  const zl = el('text', { class: 'value-label', x: m.l + pw + 8, y: Y(0) + 4 });
  zl.textContent = 'strike';
  svg.appendChild(zl);

  // area between the line and the strike, coloured by which side it is on
  const uid = `clip-${Math.random().toString(36).slice(2, 8)}`;
  const defs = el('defs');
  const above = el('clipPath', { id: `${uid}-a` });
  above.appendChild(el('rect', { x: m.l, y: m.t, width: pw, height: Math.max(0, Y(0) - m.t) }));
  const below = el('clipPath', { id: `${uid}-b` });
  below.appendChild(el('rect', { x: m.l, y: Y(0), width: pw, height: Math.max(0, m.t + ph - Y(0)) }));
  defs.appendChild(above); defs.appendChild(below);
  svg.appendChild(defs);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(bpsOf(p))}`).join(' ');
  const area = `${line} L${X(pts[pts.length - 1])},${Y(0)} L${X(pts[0])},${Y(0)} Z`;
  svg.appendChild(el('path', { class: 'series-area', d: area, fill: cUp, 'clip-path': `url(#${uid}-a)` }));
  svg.appendChild(el('path', { class: 'series-area', d: area, fill: cDown, 'clip-path': `url(#${uid}-b)` }));
  svg.appendChild(el('path', { class: 'series-line', d: line, stroke: css('--text-primary') }));

  const last = pts[pts.length - 1], lv = bpsOf(last);
  const color = lv >= 0 ? cUp : cDown;
  const cx = X(last), cy = Y(lv);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const endLab = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  endLab.textContent = `${lv >= 0 ? '+' : ''}${lv.toFixed(1)} bps`;
  svg.appendChild(endLab);

  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw, win);
    if (!p) return;
    cross.setAttribute('x1', X(p)); cross.setAttribute('x2', X(p)); cross.setAttribute('opacity', 1);
    const v = bpsOf(p), sd = bandAt(p);
    const rows = [
      [v >= 0 ? 'Above strike' : 'Below strike', `${v >= 0 ? '+' : ''}${v.toFixed(1)} bps`, v >= 0 ? cUp : cDown],
      ['Price', fmtSpot(p.spot)],
      ['Strike', fmtSpot(strike)],
    ];
    if (sd > 0) rows.push(['±1σ to settle', `${sd.toFixed(1)} bps`, cBand], ['z', (v / sd).toFixed(2)]);
    if (p.up != null) rows.push(['Market Up', p.up.toFixed(3)]);
    showTip(tipRows(`${fmt.mmss(p.left)} left`, rows), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });

  if (!forExport) {
    legend($('#strike-legend'), [
      { label: 'Above strike', color: cUp }, { label: 'Below strike', color: cDown },
      { label: '±1σ expected move', color: cBand },
    ]);
  }
  return svg;
}

/* Parameterised so a CLOSED market can be drawn into an offscreen host for
   PNG export without disturbing what is on screen. */
function renderPriceChart(slug = state.selected, host = $('#price-chart'),
                          forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const win = selectedWindowS(slug);
  // NOT .filter(inWindow): Array#filter passes (p, index, array), so the index
  // would arrive as the window length
  const pts = (hist || []).filter((p) => inWindow(p, win));
  if (pts.length < 2) return emptyChart(host, $('#price-legend'), forExport, hist, win);

  const cUp = css('--series-1'), cDown = css('--series-2');
  const H = 300, m = { t: 14, r: 56, b: 34, l: 48 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const Y = (v) => m.t + ph - v * ph;                 // prices live in [0,1]

  [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = t.toFixed(2);
    svg.appendChild(lab);
  });
  const { X } = fixedAxis(svg, m, pw, H, win);

  [['up', cUp], ['down', cDown]].forEach(([key, color]) => {
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p[key])}`).join(' ');
    svg.appendChild(el('path', { class: 'series-line', d, stroke: color }));
    const last = pts[pts.length - 1];
    const cx = X(last), cy = Y(last[key]);
    svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
    const t = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
    t.textContent = last[key].toFixed(2);
    svg.appendChild(t);
  });

  // the strike distance, expressed on THIS axis as the Up probability it
  // implies (distance / expected move through the pricer's own CDF), so the
  // model's number sits directly against the market's without a second scale
  let implied = null;
  if (state.overlay && !forExport) {
    const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
      || (state.live.live_markets || []).find((x) => x.slug === slug);
    const strike = rec && rec.strike ? rec.strike : null;
    const asset = (slug || '').split('-')[0].toUpperCase();
    const sigma = (state.live.sigma_bps_per_sec || {})[asset] || 0;
    if (strike && sigma > 0) {
      implied = new Map();
      pts.forEach((p) => {
        if (p.spot == null) return;
        const bps = Math.log(p.spot / strike) * 1e4;
        const sd = twapSd(p.left, sigma);
        implied.set(p, { bps, sd, p: sd > 0 ? t4cdf(bps / sd) : (bps >= 0 ? 1 : 0) });
      });
      const ip = pts.filter((p) => implied.has(p));
      if (ip.length) {
        const d = ip.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(implied.get(p).p)}`).join(' ');
        svg.appendChild(el('path', { class: 'series-line', d, stroke: css('--text-primary'),
                                      'stroke-dasharray': '5 4' }));
        const last = ip[ip.length - 1], v = implied.get(last);
        const cx = X(last), cy = Y(v.p);
        svg.appendChild(el('circle', { cx, cy, r: 4.5, fill: css('--text-primary'),
                                        stroke: css('--surface-1'), 'stroke-width': 2 }));
        const t = el('text', { class: 'value-label', x: cx + 10, y: cy + 4 });
        t.textContent = `model ${v.p.toFixed(2)} (${v.bps >= 0 ? '+' : ''}${v.bps.toFixed(1)} bps)`;
        svg.appendChild(t);
      }
    }
  }

  // where we actually bought — the whole point of plotting this
  const fills = (state.live.ledger || []).filter(
    (r) => r.event === 'fill' && r.slug === slug);
  fills.forEach((f) => {
    let at = pts[0], best = Infinity;
    pts.forEach((p) => { const d = Math.abs(p.t - f.ts); if (d < best) { best = d; at = p; } });
    const color = f.side === 'UP' ? cUp : cDown;
    const cx = X(at), cy = Y(f.price);
    svg.appendChild(el('path', {
      d: `M${cx},${cy - 7} L${cx + 7},${cy} L${cx},${cy + 7} L${cx - 7},${cy} Z`,
      fill: color, stroke: css('--surface-1'), 'stroke-width': 2,
    }));
    const hit = el('circle', { class: 'hit', cx, cy, r: 13 });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows('Bought ' + f.side, [
      ['Price', f.price.toFixed(3), color],
      ['Shares', fmt.num(f.size, 1)],
      ['Cost', fmt.money(f.cost || f.price * f.size)],
      ['Our fair', (f.fair ?? 0).toFixed(3)],
      ['Time left', fmt.mmss(f.secs_left || 0)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });

  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw, win);
    if (!p) return;
    cross.setAttribute('x1', X(p)); cross.setAttribute('x2', X(p)); cross.setAttribute('opacity', 1);
    const rows = [
      ['Up', p.up.toFixed(3), cUp],
      ['Down', p.down.toFixed(3), cDown],
      ['Sum', (p.up + p.down).toFixed(3)],
    ];
    const v = implied && implied.get(p);
    if (v) {
      rows.push(['Model P(up)', v.p.toFixed(3), css('--text-primary')],
                ['Distance', `${v.bps >= 0 ? '+' : ''}${v.bps.toFixed(1)} bps`],
                ['±1σ to settle', `${v.sd.toFixed(1)} bps`],
                ['Model − Up', `${(v.p - p.up) >= 0 ? '+' : ''}${(v.p - p.up).toFixed(3)}`]);
    }
    showTip(tipRows(`${fmt.mmss(p.left)} left`, rows), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });

  if (!forExport) {
    const items = [{ label: 'Up', color: cUp }, { label: 'Down', color: cDown }];
    if (implied) items.push({ label: 'Model P(up) from strike distance', color: css('--text-primary') });
    legend($('#price-legend'), items);
  }

  if (forExport) return svg;
  const mk = (state.live.live_markets || []).find((x) => x.slug === slug);
  $('#chart-sub').textContent = mk
    ? `${fmt.mkt(slug)} · ${fmt.window(mk)} UTC · strike ${mk.strike ? mk.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'} · ${fmt.mmss(mk.secs_left)} left · ${mk.status || ''}`
    : 'Where the price sits against the strike, what the market charges for each side, and the raw price path.';
  return svg;
}

/* ═══════════════════════ closed-window PNG export ═══════════════════════ */

/* When a window closes its chart is exported once, then the view moves on to
   a live market. Rendering happens in the browser rather than in Python so the
   saved image IS the chart on screen — a second plotting implementation server
   side would drift away from this one. The bot separately archives the raw
   points to data/charts/<slug>.json, so a window is never lost when the page
   is shut. */

const exported = (() => {
  try { return new Set(JSON.parse(localStorage.getItem('tpb-charts') || '[]')); }
  catch { return new Set(); }
})();
function rememberExported(slug) {
  exported.add(slug);
  try {
    // keep the list bounded; it only exists to avoid re-POSTing on reload
    localStorage.setItem('tpb-charts', JSON.stringify([...exported].slice(-400)));
  } catch { /* private mode */ }
}

/* The chart is styled by an external stylesheet. Serialised on its own, an SVG
   loses all of that and renders as unstyled black — so every computed paint
   property is copied onto the clone as a presentation attribute first. */
const PAINT = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-linecap',
               'stroke-linejoin', 'opacity', 'font-size', 'font-family',
               'font-weight', 'text-anchor', 'shape-rendering'];

function inlineStyles(src) {
  const clone = src.cloneNode(true);
  const from = [src, ...src.querySelectorAll('*')];
  const to = [clone, ...clone.querySelectorAll('*')];
  from.forEach((node, i) => {
    const cs = getComputedStyle(node);
    PAINT.forEach((prop) => {
      const v = cs.getPropertyValue(prop);
      if (v) to[i].setAttribute(prop, v.trim());
    });
    to[i].removeAttribute('class');
  });
  return clone;
}

async function exportChartPng(slug) {
  // the main poll only carries the market being drawn; fetch this one's own
  // history explicitly, and hand it to the renderer
  const own = await fetch(`/api/live?chart=${encodeURIComponent(slug)}`)
    .then((r) => r.json()).catch(() => null);
  const pts = own && own.price_history && own.price_history[slug];
  if (!pts || pts.length < 2 || !state.live) return false;
  state.live.price_history = Object.assign({}, state.live.price_history, { [slug]: pts });

  // draw the closed market offscreen so the visible chart is untouched
  const stage = document.createElement('div');
  stage.style.cssText = 'position:fixed;left:-10000px;top:0;width:1100px';
  document.body.appendChild(stage);
  try {
    const svgEl = renderPriceChart(slug, stage, true);
    if (!svgEl) return false;

    const w = Number(svgEl.getAttribute('width'));
    const h = Number(svgEl.getAttribute('height'));
    const CAP = 46;                         // caption band under the plot
    const out = inlineStyles(svgEl);
    out.setAttribute('xmlns', NS);
    out.setAttribute('width', w);
    out.setAttribute('height', h + CAP);
    out.setAttribute('viewBox', `0 0 ${w} ${h + CAP}`);

    // SVG is transparent; a PNG needs a real background
    const bg = el('rect', { x: 0, y: 0, width: w, height: h + CAP,
                            fill: css('--surface-1') });
    out.insertBefore(bg, out.firstChild);

    const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
      || (state.live.live_markets || []).find((x) => x.slug === slug) || {};
    const caption = el('text', {
      x: 14, y: h + 20, 'font-size': 13, 'font-weight': 600,
      'font-family': css('--sans') || 'sans-serif', fill: css('--text-primary'),
    });
    caption.textContent = fmt.mkt(slug)
      + (rec.strike ? `  ·  strike ${rec.strike.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
                    : '  ·  never struck')
      + (rec.outcome ? `  ·  ${rec.outcome} won` : '')
      + (rec.traded ? `  ·  ${fmt.signed(rec.pnl || 0)}` : '  ·  not traded');
    out.appendChild(caption);

    const sub = el('text', {
      x: 14, y: h + 37, 'font-size': 11,
      'font-family': css('--sans') || 'sans-serif', fill: css('--text-muted'),
    });
    sub.textContent = new Date().toLocaleString() + '  ·  paper trading, simulated fills';
    out.appendChild(sub);

    const xml = new XMLSerializer().serializeToString(out);
    const url = URL.createObjectURL(new Blob([xml], { type: 'image/svg+xml;charset=utf-8' }));
    try {
      const img = new Image();
      await new Promise((res, rej) => {
        img.onload = res;
        img.onerror = () => rej(new Error('svg decode failed'));
        img.src = url;
      });
      const SCALE = 2;                      // readable on a high-dpi screen
      const canvas = document.createElement('canvas');
      canvas.width = w * SCALE;
      canvas.height = (h + CAP) * SCALE;
      const ctx = canvas.getContext('2d');
      ctx.scale(SCALE, SCALE);
      ctx.drawImage(img, 0, 0);
      const png = await new Promise((r) => canvas.toBlob(r, 'image/png'));
      if (!png) return false;
      const res = await fetch(`/api/chart/${encodeURIComponent(slug)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'image/png' },
        body: png,
      }).then((r) => r.json());
      return !!res.ok;
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    console.warn('chart export failed for', slug, e);
    return false;
  } finally {
    stage.remove();
  }
}

let exporting = false;

async function archiveClosedWindows() {
  if (exporting || !state.live) return;
  const meta = state.live.history_meta || {};

  // Wait for the bot to publish the RESOLVED window rather than firing the
  // moment the clock hits zero. Settlement lands ~40s after close, and a chart
  // captioned without its outcome is missing the point of keeping it.
  const resolved = state.live.closed_markets || [];
  const done = resolved
    .map((m) => m.slug)
    .filter((slug) => !exported.has(slug) && (meta[slug] || 0) >= 10);
  if (!done.length) return;

  exporting = true;
  try {
    for (const slug of done) {
      rememberExported(slug);               // mark first: never retry in a loop
      const ok = await exportChartPng(slug);
      if (ok) notice(`Saved chart for ${fmt.mkt(slug)} → data/charts/${slug}.png`, false);
    }
  } finally {
    exporting = false;
  }
  // a closed window has left the picker; renderPicker re-selects on the next tick
}

/* ═══════════════════════════════ tables ═════════════════════════════════ */

function modeLabel(d) {
  if (d.mode === 'LIVE-ARMED') return 'REAL MONEY · ';
  if (d.mode === 'live-dry-run') return 'live dry run · ';
  return '';
}

function liveLine(lv) {
  if (!lv) return '';
  const bits = [];
  if (lv.venue_balance != null) bits.push(`account ${fmt.money(lv.venue_balance)}`);
  if (lv.pending_redemption) bits.push(`pending redemption ${fmt.money(lv.pending_redemption)}`);
  if (lv.halted) bits.push(`HALTED: ${lv.halted}`);
  if (lv.kill_file_present) bits.push('KILL FILE PRESENT');
  return bits.length ? ' · ' + bits.join(' · ') : '';
}

function byAsset(m) {
  const e = Object.entries(m || {});
  return e.length ? ' · by asset ' + e.map(([a, v]) => `${a} ${fmt.signed(v)}`).join(', ') : '';
}

function feedsLine(f) {
  const e = Object.entries(f || {});
  if (!e.length) return '';
  const down = e.filter(([, v]) => !v.connected);
  return down.length
    ? ' · FEEDS DOWN: ' + down.map(([n, v]) => `${n} ${v.state_age_s != null ? Math.round(v.state_age_s) + 's' : ''}`).join(', ')
    : ` · feeds ok (${e.length})`;
}

/* Regime constants come from the engine as LABEL|LABEL; show only what is
   not the default, in words. */
function fmtRegime(label) {
  const parts = String(label || '').split('|')
    .filter((x) => x && !x.startsWith('NORMAL') && x !== 'SIDEWAYS')
    .map((x) => x.toLowerCase().replace(/_/g, ' '));
  return parts.length ? parts.join(' · ') : 'normal';
}

const STATUS_RANK = { closing: 0, live: 1, pending: 2, 'missed open': 3 };
function marketStatus(m) {
  return m.status || (m.strike ? 'live' : 'pending');
}

function flowCell(dc) {
  if (dc.ofi == null) return '—';
  const v = dc.ofi;
  const cls = v > 0.15 ? 'pos' : v < -0.15 ? 'neg' : '';
  const d = dc.ofi_drift_bps ? ` (${dc.ofi_drift_bps >= 0 ? '+' : ''}${dc.ofi_drift_bps.toFixed(1)}bp)` : '';
  return `<span class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(2)}</span>${d}`;
}

function bestSide(dc) {
  const s = (dc.sides || []).filter((x) => x.costs);
  if (!s.length) return null;
  return s.reduce((a, b) => (b.costs.net_edge > a.costs.net_edge ? b : a));
}

function renderHero() {
  const d = state.live;
  const h = $('#hero-equity');
  h.textContent = fmt.money(d.equity);
  h.className = 'hero-figure ' + (d.pnl >= 0 ? 'pos' : 'neg');
  $('#hero-sub').textContent = modeLabel(d)
    + `${fmt.signed(d.pnl)} from ${fmt.money(d.starting_balance)} · `
    + `realised ${fmt.signed(d.realised_pnl || 0)} · up ${Math.round((d.uptime_s || 0) / 60)}m`
    + byAsset(d.pnl_by_asset) + feedsLine(d.feeds) + liveLine(d.live);
  $('#hero-sub').classList.toggle('neg', d.mode === 'LIVE-ARMED');

  const s = d.stats || {}, o = d.orders || {}, e = d.evidence || {}, r = d.risk || {};
  const kpis = [
    { l: 'Settled', v: fmt.num(s.settled || 0), n: `${s.wins || 0}W / ${s.losses || 0}L` },
    { l: 'Orders', v: fmt.num(o.orders_submitted || 0), n: `${fmt.num(o.orders_filled || 0)} filled` },
    { l: 'Open markets', v: fmt.num((d.live_markets || []).length), n: `${s.windows_seen || 0} seen` },
    { l: 'Reaction lag', v: fmt.ms(d.reaction_lag_ms || 0), n: 'measured' },
    { l: 'Clock offset', v: fmt.ms(d.clock_offset_ms || 0), n: 'local vs exchange' },
    { l: 'Basis disagreements', v: fmt.num(s.basis_disagreements || 0), n: 'proxy vs venue' },
    { l: 'Evidence', v: (e.t == null ? '—' : e.t.toFixed(2)), n: `t over ${e.epochs || 0} epochs traded` },
    { l: 'Today', v: fmt.signed(r.realised_today || 0),
      n: `drawdown ${fmt.money(r.drawdown || 0)}${r.halted ? ' · HALTED' : ''}` },
  ];
  $('#kpi-row').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');
}

function renderMarkets() {
  const rows = state.live.live_markets || [];
  const struck = rows.filter((m) => m.strike > 0).length;
  const cov = state.live.twap_coverage_s || {};
  const warming = Object.entries(cov).filter(([, v]) => v < 59);
  $('#markets-sub').textContent = struck
    ? `${struck} struck and being priced, ${rows.length - struck} awaiting their window open.`
    : warming.length
      ? `None struck yet — the 60s TWAP window is still filling (${
          warming.map(([a, v]) => `${a} ${Math.round(v)}s`).join(', ')}).`
      : 'None struck yet.';
  const ordered = rows.slice().sort((a, b) =>
    (STATUS_RANK[marketStatus(a)] ?? 9) - (STATUS_RANK[marketStatus(b)] ?? 9)
    || a.secs_left - b.secs_left || a.slug.localeCompare(b.slug));
  $('#markets-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Market</th><th scope="col">Status</th><th scope="col">Strike</th>'
      + '<th scope="col">Opens – closes (UTC)</th><th scope="col">Left</th><th scope="col">Model</th><th scope="col">Market</th>'
      + '<th scope="col">Net edge</th><th scope="col">Flow</th><th scope="col">Regime</th>'
      + '<th scope="col">Decision</th><th scope="col">Position</th></tr></thead><tbody>'
      + ordered.map((m) => { const dc = m.decision || {}; const best = bestSide(dc); const st = marketStatus(m); return `<tr>
          <td>${fmt.mkt(m.slug)}</td>
          <td><span class="status" data-status="${st.replace(' ', '-')}">${st}</span></td>
          <td>${m.strike ? m.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'}</td>
          <td class="dim">${fmt.window(m)}</td>
          <td>${fmt.mmss(m.secs_left)}</td>
          <td>${dc.p_used == null ? '—' : dc.p_used.toFixed(3)}</td>
          <td>${dc.p_market == null ? '—' : dc.p_market.toFixed(3)}</td>
          <td>${best ? (best.costs.net_edge >= 0 ? '+' : '') + best.costs.net_edge.toFixed(3) : '—'}</td>
          <td>${flowCell(dc)}</td>
          <td class="dim wrap">${fmtRegime(dc.regime)}</td>
          <td>${dc.reason ? `<span class="pending">${GATE_LABELS[dc.reason] || dc.reason}</span>`
            : (dc.side ? `<b>BUY ${dc.side}</b>` : '—')}</td>
          <td>${m.up_shares ? `<span class="side-tag" data-side="UP">UP</span> ${fmt.num(m.up_shares, 1)}`
            : m.down_shares ? `<span class="side-tag" data-side="DOWN">DOWN</span> ${fmt.num(m.down_shares, 1)}` : '—'}</td></tr>`; }).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No struck markets yet — needs 60s of price history first.</td></tr></tbody>';
}

function renderAgreement() {
  const ag = state.live.model_vs_market || {};
  const rows = Object.entries(ag).filter(([, v]) => v && v.n);
  $('#agreement-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Asset</th><th scope="col">Bias</th>'
      + '<th scope="col">Mean |diff|</th><th scope="col">Sigma</th><th scope="col">Sources</th>'
      + '<th scope="col">Regime</th><th scope="col">n</th></tr></thead><tbody>'
      + rows.map(([a, v]) => `<tr><td>${a}</td>
          <td>${v.mean_bias >= 0 ? '+' : ''}${v.mean_bias.toFixed(3)}</td>
          <td>${v.mean_abs.toFixed(3)}</td>
          <td>${(state.live.sigma_bps_per_sec || {})[a] ?? '—'}</td>
          <td>${((state.live.spot_sources || {})[a] || {}).n ?? '—'}</td>
          <td class="dim wrap">${fmtRegime((state.live.regime || {})[a])}</td>
          <td>${fmt.num(v.n)}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">Warming up.</td></tr></tbody>';

  const worst = rows.reduce((mx, [, v]) => Math.abs(v.mean_bias) > Math.abs(mx) ? v.mean_bias : mx, 0);
  $('#agreement-note').textContent = rows.length
    ? (Math.abs(worst) > 0.05
      ? `Our fair differs from the market's mid by ${worst.toFixed(3)} on average. A large `
        + 'one-directional gap against a liquid book is a bug in our model, not free money.'
      : 'Model and market agree closely. When mean |diff| exceeds |bias| the residual is '
        + 'scatter rather than a systematic error.')
    : '';
}

const TRADE_COLS = ['Time', 'Market', 'Side', 'Price', 'Shares', 'Cost', 'Our fair', 'Left', 'Result'];

/* Settlement polls the venue up to SETTLE_MAX_ATTEMPTS x SETTLE_RETRY_MS
   (5 x 30 s) after a window closes, so a fill is legitimately unresolved for
   up to ~150 s past its close before anything is wrong. */
const SETTLE_GRACE_MS = 180000;

/* A fill's own window close, derived from the point it was taken at. The
   ledger tail carries no market metadata, but every fill records how much of
   its window was left when it was struck, which is the same thing. */
function closeTsOf(f) {
  return f.ts + (f.secs_left || 0) * 1000;
}

function resultOf(f, s, now) {
  // a settle row is authoritative whenever we have one
  if (s && s.unresolved) return { text: 'unresolved', won: null };
  // closed on the book before the oracle printed: a realised number, not a bet
  if (s && s.exited) return { text: `${s.pnl >= 0 ? 'banked' : 'cut'} ${fmt.signed(s.pnl)}`,
                              won: s.pnl >= 0 };
  if (s) return { text: s.pnl >= 0 ? `won ${fmt.signed(s.pnl)}` : `lost ${fmt.signed(s.pnl)}`,
                  won: s.pnl >= 0 };
  // no settle row: distinguish "still trading" from "closed, awaiting the
  // venue" from "closed long ago and never resolved" -- all three used to
  // render as "open", which is why resolved markets looked stuck
  const closed = closeTsOf(f);
  if (now < closed) return { text: 'open', won: null };
  if (now < closed + SETTLE_GRACE_MS) return { text: 'settling', won: null };
  return { text: 'unresolved', won: null };
}

function tradeRows() {
  const led = state.live.ledger || [];
  const settled = {};
  led.forEach((r) => { if (r.event === 'settle') settled[r.slug] = r; });
  const now = Date.now();
  return led.filter((r) => r.event === 'fill').slice().reverse().map((f) => {
    const s = settled[f.slug];
    const res = resultOf(f, s, now);
    return {
      time: fmt.clock(f.ts),
      market: fmt.mkt(f.slug),
      // an exit is a SELL of the same side we bought; say so rather than
      // showing a second "UP" row that looks like we doubled down
      side: f.action === 'SELL' ? `${f.side || '?'} exit` : (f.side || '—'),
      price: f.price.toFixed(3),
      shares: fmt.num(f.size, 1),
      cost: fmt.money(f.cost ?? f.price * f.size),
      fair: (f.p_used == null ? (f.fair ?? 0) : (f.side === 'UP' ? f.p_used : 1 - f.p_used)).toFixed(3),
      left: `${Math.round(f.secs_left || 0)}s`,
      result: res.text,
      won: res.won,
    };
  });
}

function renderTrades() {
  const rows = tradeRows();
  $('#trades-table').innerHTML = rows.length
    ? '<thead><tr>' + TRADE_COLS.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + rows.map((r) => `<tr>
          <td>${r.time}</td><td>${r.market}</td>
          <td><span class="side-tag" data-side="${r.side}">${r.side}</span></td>
          <td>${r.price}</td><td>${r.shares}</td><td>${r.cost}</td>
          <td>${r.fair}</td><td>${r.left}</td>
          <td class="${r.won === null ? '' : r.won ? 'pos' : 'neg'}">${r.result}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No trades yet.</td></tr></tbody>';

  const done = rows.filter((r) => r.won !== null);
  const st = state.live.stats || {};
  $('#trades-note').textContent = done.length
    ? `${done.length} settled fills · realised ${fmt.signed(st.realised_pnl || 0)}. `
      + 'Buying a favourite at price p only profits if the true win rate exceeds p.'
    : 'Nothing has settled yet.';
}

const GATE_LABELS = {
  OUTSIDE_TIME_WINDOW: 'Outside time window', CANNOT_LAND_IN_TIME: 'Cannot land in time',
  STALE_DATA: 'Stale data', DATA_INCONSISTENT: 'Sources disagree', NO_STRIKE: 'No strike yet',
  MODEL_NOT_READY: 'Model warming', MODEL_SANITY: 'Sanity halt', BAD_REGIME: 'Bad regime',
  NO_OFFER: 'No offer (one-sided book)', PRICE_BAND: 'Price outside band', LOW_LIQUIDITY: 'Low liquidity',
  HIGH_SLIPPAGE: 'High slippage', EDGE_TOO_SMALL: 'Edge too small', LOW_CONFIDENCE: 'Low confidence',
  ALREADY_POSITIONED: 'Already positioned', RISK_LIMIT: 'Risk limit', SIZE_ZERO: 'Size rounds to zero',
  ORDER_IN_FLIGHT: 'Order in flight', FOK_UNFILLABLE: 'FOK rejected at venue',
  MARKET_CLOSED: 'Landed after close', NO_LIQUIDITY: 'No liquidity at landing',
  REVALIDATE_HIGH_SLIPPAGE: 'Moved before send', REVALIDATE_EDGE_TOO_SMALL: 'Edge gone before send',
  outside_time_window: 'Outside time window',
  no_edge: 'No edge',
  price_outside_tradeable_band: 'Price outside band',
  fair_too_close_to_half: 'Fair too near 0.50',
  edge_inside_model_uncertainty: 'Edge inside model error',
  model_disagrees_with_market: 'Sanity halt',
  already_positioned: 'Already positioned',
  size_zero: 'Size rounds to zero',
  cannot_land_in_time: 'Cannot land in time',
  spot_too_stale: 'Spot too stale',
  book_too_stale: 'Book too stale',
  vol_not_ready: 'Vol not warmed',
  twap_not_ready: 'TWAP not warmed',
  no_spot: 'No spot',
};

function renderGates() {
  const host = $('#gate-chart');
  const rows = Object.entries(state.live.skips || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 7);
  if (!rows.length) {
    host.innerHTML = '<p class="live-empty">No markets are being evaluated yet, '
      + 'so no rule has had anything to block.</p>';
    return;
  }

  const rowH = 30, m = { t: 6, r: 78, b: 6, l: 176 };
  const H = Math.max(rows.length * rowH + m.t + m.b, 110);
  const { svg, w } = mount(host, H);
  const pw = Math.max(w - m.l - m.r, 40);
  const max = Math.max(...rows.map((r) => r[1]), 1);

  rows.forEach(([k, v], i) => {
    const th = Math.min(20, rowH - 10);
    const y = m.t + i * rowH + (rowH - th) / 2;
    const len = Math.max((v / max) * pw, 1.5);

    const lab = el('text', { class: 'axis-text', x: m.l - 10, y: y + th / 2 + 4, 'text-anchor': 'end' });
    lab.textContent = GATE_LABELS[k] || k;
    svg.appendChild(lab);
    svg.appendChild(el('path', { d: barPath(m.l, y, len, th, 4), fill: css('--series-1') }));

    const vl = el('text', { class: 'value-label', x: m.l + len + 8, y: y + th / 2 + 4 });
    vl.textContent = fmt.num(v);
    svg.appendChild(vl);

    const hit = el('rect', { class: 'hit', x: m.l, y: m.t + i * rowH, width: pw + m.r, height: rowH });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(
      tipRows(GATE_LABELS[k] || k, [['Blocked', fmt.num(v)]]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

function copyTrades() {
  const rows = tradeRows();
  const text = [TRADE_COLS.join(',')].concat(
    rows.map((r) => [r.time, r.market, r.side, r.price, r.shares, r.cost, r.fair, r.left, r.result]
      .map((c) => `"${String(c).replace(/"/g, '""')}"`).join(','))).join('\n');
  const btn = $('#copy-trades');
  navigator.clipboard?.writeText(text).then(
    () => { btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy as CSV'; }, 1400); },
    () => { btn.textContent = 'Copy failed'; setTimeout(() => { btn.textContent = 'Copy as CSV'; }, 1400); });
}

/* ═══════════════════ history across all runs (persisted) ═══════════════════ */

/* The live panels are per-process: a restart starts a fresh exchange at its
   --balance with an empty ledger, and stop() removes the state file. The
   trade log on disk is append-only and survives every restart, so this card
   is the continuous record -- shown whether or not a bot is running. */

function renderHistoryChart(curve) {
  const host = $('#history-chart');
  if (!curve.length) { host.innerHTML = ''; return; }
  const H = 150, m = { t: 12, r: 60, b: 24, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const vals = curve.map((c) => c.cum).concat([0]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1;
  lo -= pad; hi += pad;
  const X = (i) => m.l + (curve.length <= 1 ? pw / 2 : (i / (curve.length - 1)) * pw);
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
  const color = css('--series-1');

  // the zero line is the one that matters here: above it the record is up
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: Y(0), y2: Y(0) }));
  const z = el('text', { class: 'axis-text', x: m.l - 9, y: Y(0) + 4, 'text-anchor': 'end' });
  z.textContent = '$0';
  svg.appendChild(z);

  const d = curve.map((c, i) => `${i ? 'L' : 'M'}${X(i)},${Y(c.cum)}`).join(' ');
  svg.appendChild(el('path', { class: 'series-line', d, stroke: color }));
  const last = curve[curve.length - 1];
  const cx = X(curve.length - 1), cy = Y(last.cum);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const t = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  t.textContent = fmt.signed(last.cum);
  svg.appendChild(t);

  curve.forEach((c, i) => {
    const hit = el('circle', { class: 'hit', cx: X(i), cy: Y(c.cum), r: 12 });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(fmt.mkt(c.slug), [
      ['Settled', fmt.signed(c.pnl)],
      ['Cumulative', fmt.signed(c.cum)],
      ['When', fmt.clock(c.ts)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

//: paging state for the history table. Kept module-level (not in `state`)
//  because it is UI navigation, not data from the server -- pollHistory()
//  reads it to build the request and writes it back only when the server
//  had to clamp it (e.g. the operator was on a page that no longer exists).
let historyPage = 1;
const HISTORY_PAGE_SIZE = 25;

function renderHistoryPager(h) {
  const host = $('#history-pager');
  const totalPages = h.total_pages || 1;
  const page = h.page || 1;
  if ((h.total_rows || 0) <= HISTORY_PAGE_SIZE) { host.innerHTML = ''; return; }
  host.innerHTML = `
    <span class="pager-info">Page ${page} of ${totalPages} · ${fmt.num(h.total_rows || 0)} rows</span>
    <button class="btn btn-ghost btn-sm" id="history-prev" type="button" ${page <= 1 ? 'disabled' : ''}>Prev</button>
    <button class="btn btn-ghost btn-sm" id="history-next" type="button" ${page >= totalPages ? 'disabled' : ''}>Next</button>`;
  $('#history-prev').addEventListener('click', () => {
    historyPage = Math.max(1, page - 1);
    pollHistory();
  });
  $('#history-next').addEventListener('click', () => {
    historyPage = Math.min(totalPages, page + 1);
    pollHistory();
  });
}

/* One position is several events. This is the same data read as trades:
   when it was bought, when it was sold or settled, and what that cost and
   returned. The raw stream is one toggle away for anyone who wants it. */
function renderHistoryTrips(rows) {
  const t = $('#history-table');
  const head = '<thead><tr><th scope="col">Market</th><th scope="col">Side</th>'
    + '<th scope="col">Shares</th><th scope="col">Bought</th><th scope="col">Closed</th>'
    + '<th scope="col">Held</th><th scope="col">Result</th></tr></thead>';
  if (!rows.length) {
    t.innerHTML = head + '<tbody><tr><td colspan="7" class="dim">No trades yet.</td></tr></tbody>';
    return;
  }
  t.innerHTML = head + '<tbody>' + rows.map((r) => {
    const closedWord = r.status === 'open' ? 'still open'
      : r.status === 'sold' ? 'sold' : 'settled';
    const closedCell = r.closed_ts == null
      ? '<span class="dim">still open</span>'
      : `${fmt.clock(r.closed_ts)}<span class="sub">${closedWord}`
        + `${r.exit_price == null ? '' : ` @ ${fmt.cents(r.exit_price)}`}</span>`;
    const pnl = r.pnl;
    const result = pnl == null
      ? '<span class="dim">—</span>'
      : `<span class="${pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(pnl)}</span>`
        + `<span class="sub">fees ${fmt.money(r.fees || 0)}</span>`;
    return `<tr>
        <td>${fmt.mkt(r.slug)}${r.mode ? `<span class="sub">${r.mode}</span>` : ''}</td>
        <td><span class="side-tag" data-side="${r.side || ''}">${r.side || '—'}</span></td>
        <td>${fmt.num(r.shares, 2)}<span class="sub">${r.buys} fill${r.buys === 1 ? '' : 's'}</span></td>
        <td>${r.bought_ts == null ? '—' : fmt.clock(r.bought_ts)}
            <span class="sub">${r.entry_price == null ? '' : `@ ${fmt.cents(r.entry_price)}`}</span></td>
        <td>${closedCell}</td>
        <td>${r.hold_s == null ? '—' : fmt.dur(r.hold_s)}</td>
        <td>${result}</td>
      </tr>`;
  }).join('') + '</tbody>';
}

function renderHistoryTable(rows) {
  // rows arrive as one page, newest first, straight from the server --
  // no client-side reversing or slicing left to do.
  const t = $('#history-table');
  t.innerHTML = '<thead><tr><th scope="col">Time</th><th scope="col">Event</th>'
    + '<th scope="col">Market</th><th scope="col">Detail</th><th scope="col">Result</th></tr></thead><tbody>'
    + rows.map((r) => {
      if (r.event === 'fill') {
        // An entry and its exit are both "fill" rows for the same market and
        // side, so without naming the direction they read as the same trade
        // written twice. Older rows carry no action at all and are buys.
        const sold = String(r.action || '').toUpperCase() === 'SELL';
        return `<tr><td>${fmt.clock(r.ts)}</td>`
          + `<td><span class="act ${sold ? 'act-sell' : 'act-buy'}">${sold ? 'sell' : 'buy'}</span></td>`
          + `<td>${fmt.mkt(r.slug)}</td>`
          + `<td><span class="side-tag" data-side="${r.side || ''}">${r.side || '—'}</span> `
          + `${fmt.num(r.size, 1)} sh @ ${Number(r.price).toFixed(3)}</td>`
          + `<td>${sold ? 'received' : 'cost'} ${fmt.money(Math.abs(r.cost ?? r.price * r.size))}</td></tr>`;
      }
      const pnl = Number(r.pnl || 0);
      // The venue outcome is authoritative. When Gamma had not published one
      // in time the bot settled on its own Binance-proxy TWAP; say so rather
      // than render a blank, so a basis disagreement is visible in the record.
      const venue = r.venue_up != null;
      const up = venue ? r.venue_up : r.ours_up;
      const who = up == null ? '' : (venue ? '' : ' (our TWAP)');
      // A position closed early never waits for a direction, so naming a
      // winner there would be inventing one. Say what actually happened.
      const how = up != null ? `${up ? 'UP' : 'DOWN'} won${who}`
        : r.exited ? 'closed before resolution'
          : 'outcome not recorded';
      return `<tr><td>${fmt.clock(r.ts)}</td><td>settle</td><td>${fmt.mkt(r.slug)}</td>`
        + `<td>${how}</td>`
        + `<td class="${pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(pnl)} · bal ${fmt.money(r.balance || 0)}</td></tr>`;
    }).join('') + '</tbody>';
}

/* ?view=events&money=live links the raw stream or one bot's half of it, the
   same way ?period= and ?scope= link an earnings view. */
const HIST = (() => {
  const q = new URLSearchParams(location.search);
  const view = q.get('view'), money = q.get('money');
  return {
    view: view === 'events' ? 'events' : 'trips',
    mode: ['all', 'paper', 'live'].includes(money) ? money : 'all',
  };
})();

async function pollHistory() {
  if (state.page !== 'ledger') return;
  let h;
  try {
    const qs = `?page=${historyPage}&page_size=${HISTORY_PAGE_SIZE}`
      + `&view=${HIST.view}&mode=${HIST.mode}`;
    h = await fetch(`/api/history${qs}`).then((r) => r.json());
  } catch { return; }
  const card = $('#history-card');
  if (!h || (!h.settled && !h.fills)) { card.hidden = true; return; }
  card.hidden = false;
  // the server clamps an out-of-range page (e.g. after a log rotation);
  // follow it so Prev/Next keep working off the page that actually exists
  historyPage = h.page || 1;
  const pnl = h.realised_pnl || 0;
  const s = h.trips_summary || {};

  // the money filter only earns its place once two kinds of bot have written
  $('#history-mode-field').hidden = (h.modes || []).length < 2;

  $('#history-kpis').innerHTML = [
    { l: 'Realised PnL, all runs', v: fmt.signed(pnl), n: 'sum of every settlement on disk',
      cls: pnl >= 0 ? 'pos' : 'neg' },
    { l: 'Trades', v: fmt.num(s.trips || 0),
      n: `${fmt.num(h.events_total || 0)} events folded${s.open ? ` · ${s.open} open` : ''}` },
    { l: 'Won', v: s.win_rate == null ? '—' : fmt.pct(s.win_rate),
      n: `${fmt.num(s.wins || 0)}W / ${fmt.num(s.losses || 0)}L` },
    { l: 'Typical hold', v: s.median_hold_s == null ? '—' : fmt.dur(s.median_hold_s),
      n: `${fmt.num(s.sold_early || 0)} sold early · ${fmt.num(s.held_to_resolution || 0)} held` },
    { l: 'Fees paid', v: fmt.money(s.fees || 0), n: `${fmt.num(s.shares || 0, 2)} shares traded` },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value ${k.cls || ''}">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  renderHistoryChart(h.curve || []);
  if (h.view === 'events') renderHistoryTable(h.rows || []);
  else renderHistoryTrips(h.rows || []);
  renderHistoryPager(h);
}

function historySub(view) {
  return view === 'events'
    ? 'Every fill and settlement exactly as the bot wrote it, newest first. '
      + 'An entry and its exit are separate rows here.'
    : 'One row per position, with when it was bought and when it was sold or settled. '
      + 'A position is often entered in two fills and then exited, which is several '
      + 'events for one trade — those are folded together here.';
}

function wireHistory() {
  $$('#history-view .seg-btn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === HIST.view));
  $('#history-mode').value = HIST.mode;
  $('#history-sub').textContent = historySub(HIST.view);
  $('#history-view').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-view]');
    if (!btn) return;
    HIST.view = btn.dataset.view;
    historyPage = 1;
    $$('#history-view .seg-btn').forEach((b) => b.classList.toggle('is-on', b === btn));
    $('#history-sub').textContent = historySub(HIST.view);
    pollHistory();
  });
  $('#history-mode').addEventListener('change', (ev) => {
    HIST.mode = ev.target.value;
    historyPage = 1;
    pollHistory();
  });
}

wireHistory();
pollHistory();
setInterval(pollHistory, 5000);
pollAccount();
setInterval(pollAccount, ACCOUNT_POLL_MS);

/* ═══════════════════════════════ wire up ════════════════════════════════ */

/* The chart refreshes ten times a second; the tables mostly do not change
   that often, and re-rendering a table wipes any text selection the user is
   in the middle of. So each table is rebuilt only when its own data moved. */
const sigs = {};
function changed(key, sig) {
  if (sigs[key] === sig) return false;
  sigs[key] = sig;
  return true;
}

function render(force = false) {
  if (!state.live) return;
  // Every panel below lives on the market page. Redrawing it ten times a
  // second while the operator is reading another tab costs the same CPU and
  // shows nobody anything, so the work simply does not happen.
  if (state.page !== 'market') return;
  const d = state.live;

  if (force) pickerSig = '';
  renderHero();
  renderPicker();
  if (!state.overlay) renderStrikeChart();
  renderPriceChart();
  renderSpotChart();
  archiveClosedWindows();

  const mkSig = JSON.stringify(d.live_markets || []);
  if (force || changed('markets', mkSig)) renderMarkets();

  const agSig = JSON.stringify(d.model_vs_market || {});
  if (force || changed('agreement', agSig)) renderAgreement();

  const ledSig = (d.ledger || []).length + ':' + ((d.ledger || []).at(-1)?.ts ?? 0);
  if (force || changed('trades', ledSig)) renderTrades();

  const gateSig = JSON.stringify(d.skips || {});
  if (force || changed('gates', gateSig)) renderGates();
}

let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => render(true), 140);
});

$('#btn-start').addEventListener('click', () => control('start'));
$('#btn-stop').addEventListener('click', () => control('stop'));
$('#btn-restart').addEventListener('click', () => control('restart'));
$('#copy-trades').addEventListener('click', copyTrades);
$('#overlay-toggle').addEventListener('change', (ev) => {
  state.overlay = ev.target.checked;
  try { localStorage.setItem('tpb-overlay', state.overlay ? '1' : '0'); } catch { /* private mode */ }
  applyOverlayLayout();
  render(true);
});
applyOverlayLayout();

initTheme();
poll();
connectLive();                                 // push transport
// Fallback only: pollLive() returns immediately while the socket is open, so
// this costs one function call a tick until the socket is actually gone.
state.timer = setInterval(pollLive, 250);      // chart cadence (fallback)
state.botTimer = setInterval(pollBot, 3000);   // process status

/* ══════════════════════════════════ earnings ══════════════════════════════
   Realised PnL per calendar period, from the append-only earnings log.

   Form: the job is polarity — did this day/week/month make money or lose it —
   so it is a DIVERGING column chart on a zero baseline, not a line and not a
   sequential ramp. Profit takes categorical slot 1 and loss slot 2 (the same
   two hues UP and DOWN use elsewhere); the pair validates at CVD dE 24.7 in
   light and 26.8 in dark, where green/red fails at 4.1 and is unreadable for
   the most common colour blindness. The hue is bound to the SIGN, so a filter
   that changes which periods are shown never repaints anything.

   Cumulative PnL is deliberately NOT overlaid: it is the same unit but a much
   larger magnitude, and sharing one axis would flatten every bar to nothing.
   It rides in the tooltip and in the card below instead.
   ========================================================================= */

const EARN_SPAN_HINT = { day: 'last 30 days', week: 'last 26 weeks', month: 'last 12 months' };

function earnAxisMoney(v) {
  const a = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (a >= 1000) {
    return sign + '$' + (a / 1000).toFixed(a >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
  }
  const s = a >= 10 ? a.toFixed(0) : a.toFixed(2).replace(/\.?0+$/, '');
  return sign + '$' + (s || '0');
}

/* Axis ticks land on round money — 0, $2.50, $5 — not on fractions of the
   data range. It also puts a gridline exactly on zero whenever the range
   crosses it, which is the one line a profit-and-loss chart is read against. */
function niceTicks(lo, hi, target = 5) {
  const span = (hi - lo) || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(span / target)));
  const norm = span / target / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step * 1e-9; v += step) {
    ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return { ticks, lo: start, hi: end === start ? start + step : end };
}

/* a column with its far end rounded and its baseline end square */
function colPath(x, y, w, h, r, up) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0.4) return `M${x},${y} h${w}`;              // a flat period still shows a tick
  return up
    ? `M${x},${y + h} V${y + rr} A${rr},${rr} 0 0 1 ${x + rr},${y}`
      + ` H${x + w - rr} A${rr},${rr} 0 0 1 ${x + w},${y + rr} V${y + h} Z`
    : `M${x},${y} H${x + w} V${y + h - rr} A${rr},${rr} 0 0 1 ${x + w - rr},${y + h}`
      + ` H${x + rr} A${rr},${rr} 0 0 1 ${x},${y + h - rr} Z`;
}

async function loadEarnings() {
  const { period, scope } = state.earn;
  try {
    const r = await fetch(`/api/earnings?period=${period}&scope=${scope}`);
    state.earn.data = await r.json();
  } catch {
    return;                                    // keep the last good view
  }
  renderEarnings();
}

function renderEarnings() {
  const d = state.earn.data;
  if (!d) return;
  const t = d.totals;

  const periodWord = { day: 'day', week: 'week', month: 'month' }[d.period];
  const kpis = [
    { l: `Earned (${EARN_SPAN_HINT[d.period]})`, v: fmt.signed(t.pnl),
      n: `${fmt.num(t.trades)} settled · ${t.periods_traded} ${periodWord}${t.periods_traded === 1 ? '' : 's'} traded` },
    { l: `Best ${periodWord}`, v: d.best ? fmt.signed(d.best.pnl) : '—',
      n: d.best ? d.best.label : 'nothing settled yet' },
    { l: `Worst ${periodWord}`, v: d.worst ? fmt.signed(d.worst.pnl) : '—',
      n: d.worst ? d.worst.label : 'nothing settled yet' },
    { l: 'Win rate', v: t.win_rate == null ? '—' : fmt.pct(t.win_rate),
      n: `${fmt.num(t.wins)}W / ${fmt.num(t.losses)}L` },
    { l: 'Per settled window', v: t.per_trade == null ? '—' : fmt.signed(t.per_trade),
      n: 'realised, after fees' },
    { l: 'All time', v: fmt.signed(t.all_time_pnl),
      n: `${fmt.num(t.all_time_trades)} windows on record` },
  ];
  $('#earnings-kpis').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  const pos = css('--series-1'), neg = css('--series-2');
  legend($('#earnings-legend'), [{ label: 'Profit', color: pos }, { label: 'Loss', color: neg }]);

  renderEarningsChart(d, pos, neg);
  renderEarningsTable(d);

  const modes = (d.modes || []).join(', ');
  const scopeWord = { all: 'paper and real money together',
    paper: 'paper trading only', live: 'real money only' }[d.scope];
  $('#earnings-note').textContent = t.all_time_trades
    ? `Showing ${scopeWord}. Recorded from: ${modes || 'no runs yet'}. `
      + 'Every settled window is written to data/earnings.jsonl, which no restart clears.'
    : 'Nothing has settled yet. Each settled window appends one line to '
      + 'data/earnings.jsonl, and this card reads that file — so it keeps counting across restarts.';
}

function renderEarningsChart(d, pos, neg) {
  const host = $('#earnings-chart');
  const buckets = d.buckets || [];
  if (!buckets.length) { host.innerHTML = ''; return; }

  const H = 260, m = { t: 22, r: 16, b: 38, l: 60 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;

  const vals = buckets.map((b) => b.pnl);
  const rawHi = Math.max(0, ...vals), rawLo = Math.min(0, ...vals);
  // headroom for the direct labels that sit above and below the extreme bars
  const pad = ((rawHi - rawLo) || 2) * 0.12;
  const scale = niceTicks(rawLo - pad, rawHi + pad);
  const lo = scale.lo, hi = scale.hi;

  const band = pw / buckets.length;
  const barW = Math.min(24, Math.max(3, band - 2));     // the 2px surface gap
  const X = (i) => m.l + i * band + (band - barW) / 2;
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
  const zero = Y(0);

  // gridlines: hairline, solid, recessive — they carry the values not labelled
  scale.ticks.forEach((v) => {
    const y = Y(v);
    if (v !== 0) {
      svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    }
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: y + 4, 'text-anchor': 'end' });
    lab.textContent = earnAxisMoney(v);
    svg.appendChild(lab);
  });
  // the baseline is the one line that matters: above it the period earned
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: zero, y2: zero }));

  // x labels, thinned so they cannot collide, and the newest period always shown
  const every = Math.max(1, Math.ceil(buckets.length / 8));
  buckets.forEach((b, i) => {
    const last = i === buckets.length - 1;
    if (!last && (i % every || i > buckets.length - 1 - every / 2)) return;
    const lab = el('text', {
      class: 'axis-text', x: X(i) + barW / 2, y: H - 14,
      'text-anchor': last ? 'end' : 'middle',
    });
    lab.textContent = b.label;
    svg.appendChild(lab);
  });

  const labelled = new Set([
    d.best && d.best.key, d.worst && d.worst.key,
    buckets[buckets.length - 1].trades ? buckets[buckets.length - 1].key : null,
  ].filter(Boolean));

  buckets.forEach((b, i) => {
    const up = b.pnl >= 0;
    const h = Math.abs(Y(b.pnl) - zero);
    const y = up ? zero - h : zero;
    const colour = up ? pos : neg;
    if (b.trades) {
      svg.appendChild(el('path', { d: colPath(X(i), y, barW, h, 4, up), fill: colour }));
    }
    // direct labels, sparingly: the extremes and the current period only
    if (labelled.has(b.key) && b.trades) {
      const lab = el('text', {
        class: 'value-label-strong', x: X(i) + barW / 2,
        y: up ? y - 7 : y + h + 15, 'text-anchor': 'middle',
      });
      lab.textContent = fmt.signed(b.pnl);
      svg.appendChild(lab);
    }
    const hit = el('rect', { class: 'hit', x: m.l + i * band, y: m.t, width: band, height: ph });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(b.label, [
      ['Realised', fmt.signed(b.pnl), b.trades ? colour : null],
      ['Running total', fmt.signed(b.cum)],
      ['Windows settled', fmt.num(b.trades)],
      ['Won / lost', `${b.wins} / ${b.losses}`],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

function renderEarningsTable(d) {
  const rows = (d.buckets || []).slice().reverse();
  const head = '<thead><tr><th>Period</th><th>Realised</th>'
    + '<th>Running total</th><th>Settled</th>'
    + '<th>Won</th><th>Lost</th></tr></thead>';
  const body = rows.map((b) => `<tr>
      <td>${b.label}</td>
      <td class="${b.pnl < 0 ? 'neg' : ''}">${b.trades ? fmt.signed(b.pnl) : '—'}</td>
      <td>${fmt.signed(b.cum)}</td>
      <td>${b.trades || '—'}</td>
      <td>${b.wins || '—'}</td>
      <td>${b.losses || '—'}</td>
    </tr>`).join('');
  $('#earnings-table').innerHTML = head + `<tbody>${body}</tbody>`;
}

function markSeg(sel, attr, value) {
  $$(`${sel} .seg-btn`).forEach((b) => b.classList.toggle('is-on', b.dataset[attr] === value));
}

function wireEarnings() {
  markSeg('#earn-period', 'period', state.earn.period);
  markSeg('#earn-scope', 'scope', state.earn.scope);
  $('#earn-period').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-period]');
    if (!btn) return;
    state.earn.period = btn.dataset.period;
    markSeg('#earn-period', 'period', state.earn.period);
    loadEarnings();
  });
  $('#earn-scope').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-scope]');
    if (!btn) return;
    state.earn.scope = btn.dataset.scope;
    markSeg('#earn-scope', 'scope', state.earn.scope);
    loadEarnings();
  });
}

/* ══════════════════════════════ the tuning panel ══════════════════════════
   Writes data/controls.json; a running bot re-reads it within ~2s and applies
   the whitelisted values to the very objects the strategy is using.

   A field left empty means "no override" — the bot keeps its own default, and
   the placeholder shows what that currently is. That distinction matters: an
   override equal to today's default is still an override, and would pin the
   value if the default ever changed.

   Nothing here can arm real money. Starting an armed bot stays a command-line
   act, so a stray click on a local web page can never begin spending.
   ========================================================================= */

const CTRL = { spec: [], startArgs: {}, stored: {}, edits: {}, kill: false, loaded: false };

const ctrlScope = () => $('#ctrl-scope').value;
const ctrlStored = () => CTRL.stored[ctrlScope()] || {};
const ctrlRunning = () => (state.live && state.live.controls && state.live.controls.values) || {};

async function loadControls() {
  try {
    const r = await fetch('/api/controls');
    const d = await r.json();
    CTRL.spec = d.spec || [];
    CTRL.startArgs = d.start_args || {};
    CTRL.stored = d.stored || {};
    CTRL.kill = !!d.kill_active;
    CTRL.killFile = d.kill_file || 'data/KILL';
    CTRL.loaded = true;
  } catch {
    return;
  }
  renderControls();
}

function ctrlShown(key) {
  if (key in CTRL.edits) return CTRL.edits[key];
  const stored = ctrlStored();
  return key in stored ? stored[key] : null;       // null = no override
}

function ctrlFmt(row, v) {
  if (v == null) return '—';
  return row.kind === 'bool' ? (v ? 'on' : 'off') : String(v);
}

function renderControls() {
  if (!CTRL.loaded) return;
  const stored = ctrlStored();
  const running = ctrlRunning();
  const groups = [];
  CTRL.spec.forEach((row) => {
    let g = groups.find((x) => x.name === row.group);
    if (!g) { g = { name: row.group, rows: [], liveOnly: true }; groups.push(g); }
    g.rows.push(row);
    if (row.applies_to !== 'live') g.liveOnly = false;
  });

  $('#ctrl-groups').innerHTML = groups.map((g, gi) => {
    const n = g.rows.filter((r) => r.key in stored || r.key in CTRL.edits).length;
    const fields = g.rows.map((row) => {
      const shown = ctrlShown(row.key);
      const dirty = row.key in CTRL.edits;
      const live = running[row.key];
      const runTxt = Object.keys(running).length
        ? `Running: <b>${ctrlFmt(row, live == null ? null : live)}</b>`
        : 'No bot running';
      const input = row.kind === 'bool'
        ? `<label class="ctrl-switch">
             <input type="checkbox" data-key="${row.key}" data-kind="bool"
                    ${shown === true ? 'checked' : ''}>
             <span>${shown == null ? 'not overridden' : (shown ? 'on' : 'off')}</span>
           </label>`
        : `<input type="number" data-key="${row.key}" data-kind="${row.kind}"
                  min="${row.lo}" max="${row.hi}" step="${row.step}"
                  value="${shown == null ? '' : shown}"
                  placeholder="${live == null ? 'default' : live}"
                  aria-label="${row.label}">`;
      return `<div class="ctrl-row${dirty ? ' is-dirty' : ''}" data-row="${row.key}">
          <div class="ctrl-row-head">
            <span class="ctrl-label">${row.label}</span>
            ${row.unit ? `<span class="ctrl-unit">${row.unit}</span>` : ''}
          </div>
          ${input}
          <span class="ctrl-running">${runTxt}</span>
          <span class="ctrl-help">${row.help}</span>
        </div>`;
    }).join('');
    return `<details class="ctrl-group"${gi === 0 ? ' open' : ''}${g.liveOnly ? ' data-live-only="1"' : ''}>
        <summary>${g.name}${n ? `<span class="ctrl-count">${n} set</span>` : ''}</summary>
        <div class="ctrl-grid">${fields}</div>
      </details>`;
  }).join('');

  const dirty = Object.keys(CTRL.edits).length;
  $('#ctrl-apply').disabled = !dirty;
  $('#ctrl-revert').disabled = !dirty;
  const killBtn = $('#ctrl-kill');
  killBtn.setAttribute('aria-pressed', CTRL.kill ? 'true' : 'false');
  killBtn.textContent = CTRL.kill ? 'Orders halted — resume' : 'Halt orders';

  const c = (state.live && state.live.controls) || null;
  const bits = [];
  if (CTRL.kill) bits.push(`Every order is being refused while ${CTRL.killFile} exists.`);
  if (c && c.applied_at) {
    bits.push(`The ${c.scope} bot last applied: ${c.applied.join('; ')}`
      + ` (${new Date(c.applied_at * 1000).toLocaleTimeString()}).`);
  }
  if (c && c.errors && c.errors.length) bits.push(`Bot reported: ${c.errors.join('; ')}`);
  bits.push('Bankroll, assets and spot exchanges are start-time arguments: '
    + 'change them in the header and press Restart.');
  $('#ctrl-foot').textContent = bits.join(' ');
}

function ctrlStatus(msg, kind = 'ok') {
  const s = $('#ctrl-status');
  s.textContent = msg || '';
  s.dataset.kind = kind;
}

function wireControls() {
  $('#ctrl-groups').addEventListener('input', (ev) => {
    const input = ev.target.closest('[data-key]');
    if (!input) return;
    const key = input.dataset.key;
    if (input.dataset.kind === 'bool') {
      CTRL.edits[key] = input.checked;
    } else if (input.value === '') {
      delete CTRL.edits[key];                    // emptied: drop the override
    } else {
      const n = Number(input.value);
      if (Number.isFinite(n)) CTRL.edits[key] = n;
    }
    input.closest('.ctrl-row').classList.add('is-dirty');
    $('#ctrl-apply').disabled = false;
    $('#ctrl-revert').disabled = false;
    ctrlStatus('');
  });

  $('#ctrl-scope').addEventListener('change', () => {
    CTRL.edits = {};
    ctrlStatus('');
    renderControls();
  });

  $('#ctrl-revert').addEventListener('click', () => {
    CTRL.edits = {};
    ctrlStatus('');
    renderControls();
  });

  $('#ctrl-apply').addEventListener('click', async () => {
    const scope = ctrlScope();
    const values = { ...CTRL.edits };
    // an emptied field must be removed from the stored section, which the
    // server does by rewriting the scope with what is left
    const keep = { ...ctrlStored(), ...values };
    Object.keys(ctrlStored()).forEach((k) => {
      const input = $(`[data-key="${k}"]`);
      if (input && input.dataset.kind !== 'bool' && input.value === '') delete keep[k];
    });
    $('#ctrl-apply').disabled = true;
    try {
      const res = await post('/api/controls', { scope, values: keep, replace: true });
      CTRL.stored = res.stored || CTRL.stored;
      CTRL.kill = !!res.kill_active;
      CTRL.edits = {};
      const warn = (res.warnings || []).join(' ');
      const err = (res.errors || []).join(' ');
      if (err) ctrlStatus(err, 'error');
      else if (warn) ctrlStatus(`Saved, but check this: ${warn}`, 'warn');
      else ctrlStatus(`Saved. A running bot picks this up within a couple of seconds.`, 'ok');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
      $('#ctrl-apply').disabled = false;
    }
  });

  $('#ctrl-reset').addEventListener('click', async () => {
    const scope = ctrlScope();
    try {
      const res = await post('/api/controls', { scope, reset: true });
      CTRL.stored = res.stored || CTRL.stored;
      CTRL.edits = {};
      ctrlStatus('Overrides cleared. A bot already running keeps the values it '
        + 'has until you restart it.', 'warn');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
    }
  });

  $('#ctrl-kill').addEventListener('click', async () => {
    try {
      const res = await post('/api/kill', { active: !CTRL.kill });
      CTRL.kill = !!res.kill_active;
      ctrlStatus(CTRL.kill
        ? 'Halted. Every order is refused until you resume; positions already open are untouched.'
        : 'Resumed. Orders can be sent again.', CTRL.kill ? 'warn' : 'ok');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
    }
  });
}

/* The tuning panel shows each value the running bot is actually using. That
   arrives with the 200ms state tick, but rebuilding the form that often would
   steal focus mid-keystroke and wipe an unsaved edit, so only the running
   figures are patched in place. */
function refreshControlRunning() {
  const running = ctrlRunning();
  const any = Object.keys(running).length;
  $$('.ctrl-row').forEach((rowEl) => {
    const key = rowEl.dataset.row;
    const span = rowEl.querySelector('.ctrl-running');
    const input = rowEl.querySelector('input');
    const v = running[key];
    if (span) {
      span.innerHTML = any
        ? `Running: <b>${v == null ? '—' : (typeof v === 'boolean' ? (v ? 'on' : 'off') : v)}</b>`
        : 'No bot running';
    }
    if (input && input.type === 'number' && v != null) input.placeholder = String(v);
  });
}

wireEarnings();
wireControls();
loadEarnings();
loadControls();
// The earnings log only changes when a window settles, so a slow refresh is
// plenty; the controls file changes when another tab or a hand edit writes it.
setInterval(loadEarnings, 60000);
setInterval(refreshControlRunning, 2000);

/* ═══════════════════════════════ page router ══════════════════════════════
   One document, five pages, switched on the hash. Everything stays on one
   connection and one state file; what changes is which panels are in the DOM's
   way and, more importantly, which work runs at all.

   The market page redraws ten times a second. Leaving that running while you
   read the ledger would burn the same CPU for nothing, so the per-tick render
   and the two slow polls are gated on the page actually being open. Switching
   to a page refreshes it immediately, so nothing is ever stale on arrival.
   ========================================================================= */

const PAGES = ['market', 'earnings', 'ledger', 'saved', 'variables'];

function pageFromHash() {
  const h = (location.hash || '').replace(/^#/, '').split('?')[0];
  return PAGES.includes(h) ? h : 'market';
}

function showPage(name, { push = false } = {}) {
  state.page = PAGES.includes(name) ? name : 'market';
  $$('.page').forEach((p) => { p.hidden = p.dataset.page !== state.page; });
  $$('.tab').forEach((a) => {
    const on = a.dataset.page === state.page;
    a.classList.toggle('is-on', on);
    a.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (push && location.hash !== `#${state.page}`) location.hash = state.page;
  try { localStorage.setItem('tpb-page', state.page); } catch { /* private mode */ }

  // arriving at a page refreshes it rather than waiting for its next tick
  if (state.page === 'market') render(true);
  if (state.page === 'earnings') loadEarnings();
  if (state.page === 'ledger') { pollHistory(); pollAccount(); }
  if (state.page === 'saved') loadSaved();
  if (state.page === 'variables') { loadControls(); refreshControlRunning(); }
  // ?window=<slug> deep-links one archived window
  const want = new URLSearchParams(location.search).get('window');
  if (state.page === 'saved' && want && (!SAVED.open || SAVED.open.slug !== want)) openSaved(want);
  scrollTo({ top: 0, behavior: 'instant' in document.documentElement.style ? 'instant' : 'auto' });
}

function wireRouter() {
  addEventListener('hashchange', () => showPage(pageFromHash()));
  const initial = location.hash
    ? pageFromHash()
    : (() => { try { return localStorage.getItem('tpb-page') || 'market'; } catch { return 'market'; } })();
  showPage(initial);
}

/* ═════════════════════════════ saved markets ══════════════════════════════
   The archive in data/charts: one JSON per closed window holding its whole
   200ms sample path, plus the PNG the dashboard rendered at the time.

   The list is served from a cached summary index, so paging through 876
   windows never touches the 175 MB of sample points. The PNG is the thumbnail
   because it already exists and costs nothing to show; opening a window
   fetches that one JSON and redraws it live, which a picture cannot do.
   ========================================================================= */

const SAVED = { data: null, page: 1, open: null, busy: false };

function savedQuery() {
  const p = new URLSearchParams({
    page: String(SAVED.page),
    asset: $('#saved-asset').value,
    outcome: $('#saved-outcome').value,
    traded: $('#saved-traded').value,
    sort: $('#saved-sort').value,
    q: $('#saved-q').value.trim(),
  });
  return p.toString();
}

async function loadSaved(resetPage = false) {
  if (resetPage) SAVED.page = 1;
  if (SAVED.busy) return;
  SAVED.busy = true;
  try {
    SAVED.data = await fetch(`/api/saved?${savedQuery()}`).then((r) => r.json());
  } catch {
    return;
  } finally {
    SAVED.busy = false;
  }
  renderSaved();
}

function renderSaved() {
  const d = SAVED.data;
  if (!d) return;
  SAVED.page = d.page || 1;
  const s = d.stats || {};

  const count = $('#tab-saved-count');
  if (count) count.textContent = fmt.num(s.windows || 0);

  $('#saved-kpis').innerHTML = [
    { l: 'Windows kept', v: fmt.num(s.windows || 0), n: `${(s.assets || []).length} assets` },
    { l: 'We traded', v: fmt.num(s.traded || 0),
      n: s.windows ? `${fmt.pct((s.traded || 0) / s.windows)} of them` : '—' },
    { l: 'Realised on those', v: fmt.signed(s.pnl || 0),
      n: s.win_rate == null ? 'nothing settled' : `${s.wins}W / ${s.losses}L` },
    { l: 'Resolved Up', v: s.up_share == null ? '—' : fmt.pct(s.up_share),
      n: 'of decided windows' },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  // the asset filter is built from what is actually on disk
  const sel = $('#saved-asset');
  const want = ['', ...(d.all_assets || [])];
  if (sel.options.length !== want.length) {
    const cur = sel.value;
    sel.innerHTML = want.map((a) => `<option value="${a}">${a || 'All'}</option>`).join('');
    sel.value = cur;
  }

  const rows = d.rows || [];
  $('#saved-gallery').innerHTML = rows.length ? rows.map((r) => {
    const outcome = r.outcome ? r.outcome.toUpperCase() : null;
    const badge = outcome
      ? `<span class="pill pill-${outcome === 'UP' ? 'up' : 'down'}">${outcome}</span>`
      : '<span class="pill">unresolved</span>';
    const pnl = r.traded
      ? `<span class="${r.pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(r.pnl)}</span>`
      : '<span class="dim">watched</span>';
    const thumb = r.has_png
      ? `<img loading="lazy" src="/charts/${r.slug}.png" alt="">`
      : '<div class="thumb-none">no image</div>';
    return `<button class="gcard" type="button" data-slug="${r.slug}">
        <div class="gcard-thumb">${thumb}</div>
        <div class="gcard-body">
          <div class="gcard-top"><strong>${r.asset}</strong>${badge}</div>
          <div class="gcard-when">${fmt.stamp(r.close_ts)}</div>
          <div class="gcard-foot">
            <span>${r.move_bps == null ? '—' : (r.move_bps >= 0 ? '+' : '') + fmt.num(r.move_bps, 0) + ' bps'}</span>
            ${pnl}
          </div>
        </div>
      </button>`;
  }).join('') : '<p class="empty-hint">No saved window matches these filters.</p>';

  const p = $('#saved-pager');
  p.innerHTML = `<span class="pager-info">${fmt.num(d.total_rows)} windows · page ${d.page} of ${d.total_pages}</span>
    <button class="btn btn-sm" type="button" id="saved-prev" ${d.page <= 1 ? 'disabled' : ''}>Previous</button>
    <button class="btn btn-sm" type="button" id="saved-next" ${d.page >= d.total_pages ? 'disabled' : ''}>Next</button>`;
  $('#saved-prev').onclick = () => { SAVED.page = Math.max(1, SAVED.page - 1); loadSaved(); };
  $('#saved-next').onclick = () => { SAVED.page = SAVED.page + 1; loadSaved(); };
}

async function openSaved(slug) {
  let doc;
  try {
    doc = await fetch(`/api/saved/${slug}`).then((r) => r.json());
  } catch {
    notice(`Could not read the saved window ${slug}`);
    return;
  }
  if (!doc || doc.error) { notice(`No saved window called ${slug}`); return; }
  SAVED.open = doc;
  // make the open window linkable without reloading the page
  try {
    const u = new URL(location.href);
    u.searchParams.set('window', slug);
    history.replaceState(null, '', u);
  } catch { /* older browsers: the view still works, the link just is not updated */ }
  const card = $('#saved-detail');
  card.hidden = false;

  const outcome = (doc.outcome || '').toUpperCase();
  $('#saved-detail-eyebrow').textContent = `${doc.asset || ''} · ${outcome || 'unresolved'}`
    + (doc.outcome_source ? ` · graded by the ${doc.outcome_source}` : '');
  $('#saved-detail-title').textContent = doc.question || fmt.mkt(doc.slug || slug);
  $('#saved-detail-sub').textContent =
    `${fmt.stamp(doc.open_ts)} → ${fmt.stamp(doc.close_ts)} · ${(doc.points || []).length} samples`;
  $('#saved-detail-json').href = `/api/saved/${slug}`;
  $('#saved-detail-json').setAttribute('download', `${slug}.json`);

  const fills = doc.fills || [];
  const shares = fills.reduce((a, f) => a + Math.abs(Number(f.size) || 0), 0);
  const fees = fills.reduce((a, f) => a + (Number(f.fee) || 0), 0);
  const pts = doc.points || [];
  const lastSpot = [...pts].reverse().find((p) => typeof p.spot === 'number');
  const move = (lastSpot && doc.strike) ? (lastSpot.spot - doc.strike) / doc.strike * 1e4 : null;
  $('#saved-detail-kpis').innerHTML = [
    { l: 'Strike', v: fmtSpot(doc.strike || 0), n: '60s TWAP at the open' },
    { l: 'Closed at', v: lastSpot ? fmtSpot(lastSpot.spot) : '—',
      n: move == null ? '' : `${move >= 0 ? '+' : ''}${fmt.num(move, 1)} bps from strike` },
    { l: 'Our position', v: fills.length ? `${fmt.num(shares, 2)} sh` : 'none',
      n: fills.length ? `${fills.length} fill${fills.length === 1 ? '' : 's'} · fees ${fmt.money(fees)}` : 'watched only' },
    { l: 'Realised', v: fills.length ? fmt.signed(doc.pnl || 0) : '—',
      n: fills.length ? 'after fees' : 'nothing at risk' },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  renderSavedChart(doc);
  renderSavedFills(doc);
  $('#saved-detail-foot').textContent =
    `Fee schedule ${((doc.fee || {}).rate ?? 0) * 100}% · spot from ${(doc.exchanges || []).join(', ') || 'n/a'}`
    + ` · saved as data/charts/${slug}.json`;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSavedChart(doc) {
  const host = $('#saved-detail-chart');
  // a saved window is not the selected one; its length comes from its own slug
  const win = (Number(doc.window_s) > 0) ? Number(doc.window_s) : windowForSlug(doc.slug);
  const pts = (doc.points || []).filter((p) => inWindow(p, win));
  if (!pts.length) { host.innerHTML = '<p class="empty-hint">This window has no sample path.</p>'; return; }

  const H = 280, m = { t: 18, r: 54, b: 34, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const { X } = fixedAxis(svg, m, pw, H, win);
  const Y = (v) => m.t + ph - v * ph;                   // probabilities: a fixed 0..1
  const up = css('--series-1'), down = css('--series-2');

  for (let k = 0; k <= 4; k++) {
    const v = k / 4, y = Y(v);
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: y + 4, 'text-anchor': 'end' });
    lab.textContent = v.toFixed(2);
    svg.appendChild(lab);
  }

  [['up', up, 'Up'], ['down', down, 'Down']].forEach(([key, colour]) => {
    const d = pts.filter((p) => typeof p[key] === 'number')
      .map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p[key])}`).join(' ');
    if (d) svg.appendChild(el('path', { class: 'series-line', d, stroke: colour }));
  });

  (doc.fills || []).forEach((f) => {
    const left = typeof f.left === 'number' ? f.left
      : (doc.close_ts && f.ts ? (doc.close_ts - f.ts) / 1000 : null);
    if (left == null) return;
    const side = String(f.tag || f.side || '').toUpperCase().includes('DOWN') ? 'down' : 'up';
    const price = Number(f.price);
    if (!Number.isFinite(price)) return;
    const cx = X({ left }), cy = Y(price);
    svg.appendChild(el('circle', {
      cx, cy, r: 5, fill: side === 'up' ? up : down,
      stroke: css('--surface-1'), 'stroke-width': 2,
    }));
  });

  legend($('#saved-detail-legend'), [
    { label: 'Up', color: up }, { label: 'Down', color: down },
  ]);

  pts.forEach((p) => {
    const hit = el('circle', { class: 'hit', cx: X(p), cy: m.t + ph / 2, r: 0 });
    svg.appendChild(hit);
  });
  const overlay = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(overlay);
  overlay.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw, win);
    if (!p) return;
    showTip(tipRows(fmt.mmss(p.left) + ' left', [
      ['Up', p.up == null ? '—' : fmt.cents(p.up), up],
      ['Down', p.down == null ? '—' : fmt.cents(p.down), down],
      ['Spot', p.spot == null ? '—' : fmtSpot(p.spot)],
    ]), ev);
  });
  overlay.addEventListener('mouseleave', hideTip);
}

function renderSavedFills(doc) {
  const fills = doc.fills || [];
  const host = $('#saved-detail-fills');
  if (!fills.length) { host.innerHTML = ''; return; }
  host.innerHTML = '<table class="data-table"><thead><tr><th>Side</th><th>Shares</th>'
    + '<th>Price</th><th>Fee</th><th>Cost</th></tr></thead><tbody>'
    + fills.map((f) => `<tr>
        <td>${f.tag || f.side || '—'}</td>
        <td>${fmt.num(Number(f.size) || 0, 2)}</td>
        <td>${fmt.cents(Number(f.price) || 0)}</td>
        <td>${fmt.money(Number(f.fee) || 0)}</td>
        <td>${fmt.money((Number(f.price) || 0) * (Number(f.size) || 0))}</td>
      </tr>`).join('') + '</tbody></table>';
}

function wireSaved() {
  ['#saved-asset', '#saved-outcome', '#saved-traded', '#saved-sort'].forEach((sel) => {
    $(sel).addEventListener('change', () => loadSaved(true));
  });
  let typing;
  $('#saved-q').addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => loadSaved(true), 250);
  });
  $('#saved-rescan').addEventListener('click', () => loadSaved());
  $('#saved-gallery').addEventListener('click', (ev) => {
    const card = ev.target.closest('[data-slug]');
    if (card) openSaved(card.dataset.slug);
  });
  $('#saved-detail-close').addEventListener('click', () => {
    $('#saved-detail').hidden = true;
    SAVED.open = null;
    try {
      const u = new URL(location.href);
      u.searchParams.delete('window');
      history.replaceState(null, '', u);
    } catch { /* nothing to undo */ }
  });
}

/* The tab badge should say how many windows are kept before you have opened
   the page. One row is enough to learn the total, and the index is cached, so
   this costs a single cheap request on load. */
async function loadSavedCount() {
  try {
    const d = await fetch('/api/saved?page_size=1').then((r) => r.json());
    const el = $('#tab-saved-count');
    if (el && d && d.stats) el.textContent = fmt.num(d.stats.windows || 0);
  } catch { /* the badge simply stays empty */ }
}

wireSaved();
wireRouter();
if (state.page !== 'saved') setTimeout(loadSavedCount, 1200);
