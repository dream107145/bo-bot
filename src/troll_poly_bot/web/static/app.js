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
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const WINDOW_S = 300;           // a market is five minutes; the axis is fixed to it

const state = {
  live: null,
  bot: null,
  selected: null,        // slug shown in the price chart
  timer: null,
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
  money: (v) => (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  signed: (v) => (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  pct: (v) => (v * 100).toFixed(1) + '%',
  num: (v, d = 0) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }),
  ms: (v) => Math.round(v) + ' ms',
  cents: (v) => v.toFixed(3),
  clock: (ms) => new Date(ms).toLocaleTimeString(),
  mkt: (slug) => (slug || '').replace('-updown-5m-', ' '),
  mmss: (s) => {
    const t = Math.max(0, Math.round(s));
    return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
  },
};

/* ═════════════════════════════════ theme ═════════════════════════════════ */

function initTheme() {
  const stored = (() => { try { return localStorage.getItem('tpb-theme'); } catch { return null; } })();
  if (stored === 'dark' || stored === 'light') document.documentElement.dataset.theme = stored;
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

async function pollLive() {
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
  } catch { /* transient; next tick retries */ } finally {
    inFlight = false;
  }
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

/* ═══════════════════════════ the fixed 5m axis ══════════════════════════ */

/* x is elapsed time into the window, not sample index. Mapping x to the
   sample index made every new point recompress the whole line leftward; a
   fixed 0..300s domain lets the line grow rightward and stay put. Points
   before the open are not drawn; post-close samples (left clamps to 0) sit at
   the right edge. */

const elapsedOf = (p) => WINDOW_S - Math.max(0, Math.min(WINDOW_S, p.left));
const inWindow = (p) => p.left <= WINDOW_S;

function fixedAxis(svg, m, pw, H) {
  const X = (p) => m.l + (elapsedOf(p) / WINDOW_S) * pw;
  const Xe = (elapsed) => m.l + (elapsed / WINDOW_S) * pw;
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: m.t + (H - m.t - m.b), y2: m.t + (H - m.t - m.b) }));
  for (let k = 0; k <= 5; k++) {
    const lab = el('text', { class: 'axis-text', x: Xe(k * 60), y: H - 12,
      'text-anchor': k === 0 ? 'start' : k === 5 ? 'end' : 'middle' });
    lab.textContent = fmt.mmss(WINDOW_S - k * 60);
    svg.appendChild(lab);
  }
  return { X, Xe };
}

/* nearest drawn point to a mouse x, by elapsed time */
function nearestByX(pts, ev, svg, m, pw) {
  const bx = svg.getBoundingClientRect();
  const elapsed = Math.max(0, Math.min(WINDOW_S, ((ev.clientX - bx.left - m.l) / (pw || 1)) * WINDOW_S));
  let best = null, bd = Infinity;
  for (const p of pts) {
    const d = Math.abs(elapsedOf(p) - elapsed);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}

/* ════════════════════════════ price chart ═══════════════════════════════ */

/* Only the CURRENT and NEXT window per asset. From the bot's own
   exchange-aligned clock: 0 < secs_left <= 300 is the window running now,
   300 < secs_left <= 600 is the one after; a closed window has 0 and drops
   off the picker rather than lingering until it is reaped. */
function pickerMarkets() {
  return (state.live.live_markets || [])
    .filter((m) => m.secs_left > 0 && m.secs_left <= 2 * WINDOW_S)
    .sort((a, b) => ((a.secs_left > WINDOW_S) - (b.secs_left > WINDOW_S))
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

  if (state.selected && !slugs.has(state.selected)) state.selected = null;

  // Default to something worth looking at: a market we hold, else the struck
  // current window closest to resolving, else a current window, else next.
  if (!state.selected) {
    const rank = (m) => {
      if (held.has(m.slug)) return [0, m.secs_left];
      if (m.strike > 0 && m.secs_left <= WINDOW_S) return [1, m.secs_left];
      if (m.secs_left <= WINDOW_S) return [2, m.secs_left];
      return [3, m.secs_left];
    };
    state.selected = markets.slice().sort((a, b) => {
      const ra = rank(a), rb = rank(b);
      return ra[0] - rb[0] || ra[1] - rb[1];
    })[0].slug;
  }

  // rebuilding eight buttons five times a second is pointless and eats clicks
  const sig = JSON.stringify(markets.map((m) => [m.slug, m.secs_left > WINDOW_S, held.has(m.slug), (meta[m.slug] || 0) >= 2]))
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
      + (m.secs_left > WINDOW_S ? '<span class="pending">next</span>' : '')
      + (held.has(m.slug) ? '<span class="held">held</span>' : '');
    b.addEventListener('click', () => { state.selected = m.slug; pollLive(); });
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

function emptyChart(host, legendHost, forExport, hist) {
  if (forExport) return null;
  const opensIn = hist && hist.length ? Math.round(hist[hist.length - 1].left - WINDOW_S) : null;
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
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p));
  if (!pts.length) return emptyChart(host, $('#spot-legend'), forExport, hist);

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
  const { X } = fixedAxis(svg, m, pw, H);

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
    const p = nearestByX(pts, ev, svg, m, pw);
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
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p));
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
  const { X } = fixedAxis(svg, m, pw, H);

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
    const p = nearestByX(pts, ev, svg, m, pw);
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
  const pts = (hist || []).filter(inWindow);
  if (pts.length < 2) return emptyChart(host, $('#price-legend'), forExport, hist);

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
  const { X } = fixedAxis(svg, m, pw, H);

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
    const p = nearestByX(pts, ev, svg, m, pw);
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
    ? `${fmt.mkt(slug)} · strike ${mk.strike ? mk.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'} · ${fmt.mmss(mk.secs_left)} left · ${mk.status || ''}`
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
  $('#hero-sub').textContent =
    `${fmt.signed(d.pnl)} from ${fmt.money(d.starting_balance)} · `
    + `realised ${fmt.signed(d.realised_pnl || 0)} · up ${Math.round((d.uptime_s || 0) / 60)}m`
    + byAsset(d.pnl_by_asset) + feedsLine(d.feeds);

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
      + '<th scope="col">Left</th><th scope="col">Model</th><th scope="col">Market</th>'
      + '<th scope="col">Net edge</th><th scope="col">Flow</th><th scope="col">Regime</th>'
      + '<th scope="col">Decision</th><th scope="col">Position</th></tr></thead><tbody>'
      + ordered.map((m) => { const dc = m.decision || {}; const best = bestSide(dc); const st = marketStatus(m); return `<tr>
          <td>${fmt.mkt(m.slug)}</td>
          <td><span class="status" data-status="${st.replace(' ', '-')}">${st}</span></td>
          <td>${m.strike ? m.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'}</td>
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

function renderHistoryTable(rows) {
  // rows arrive as one page, newest first, straight from the server --
  // no client-side reversing or slicing left to do.
  const t = $('#history-table');
  t.innerHTML = '<thead><tr><th scope="col">Time</th><th scope="col">Event</th>'
    + '<th scope="col">Market</th><th scope="col">Detail</th><th scope="col">Result</th></tr></thead><tbody>'
    + rows.map((r) => {
      if (r.event === 'fill') {
        return `<tr><td>${fmt.clock(r.ts)}</td><td>fill</td><td>${fmt.mkt(r.slug)}</td>`
          + `<td><span class="side-tag" data-side="${r.side || ''}">${r.side || '—'}</span> `
          + `${fmt.num(r.size, 1)} sh @ ${Number(r.price).toFixed(3)}</td>`
          + `<td>cost ${fmt.money(r.cost ?? r.price * r.size)}</td></tr>`;
      }
      const pnl = Number(r.pnl || 0);
      // The venue outcome is authoritative. When Gamma had not published one
      // in time the bot settled on its own Binance-proxy TWAP; say so rather
      // than render a blank, so a basis disagreement is visible in the record.
      const venue = r.venue_up != null;
      const up = venue ? r.venue_up : r.ours_up;
      const who = up == null ? '' : (venue ? '' : ' (our TWAP)');
      return `<tr><td>${fmt.clock(r.ts)}</td><td>settle</td><td>${fmt.mkt(r.slug)}</td>`
        + `<td>${up == null ? '—' : (up ? 'UP' : 'DOWN')} won${who}</td>`
        + `<td class="${pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(pnl)} · bal ${fmt.money(r.balance || 0)}</td></tr>`;
    }).join('') + '</tbody>';
}

async function pollHistory() {
  let h;
  try {
    const qs = `?page=${historyPage}&page_size=${HISTORY_PAGE_SIZE}`;
    h = await fetch(`/api/history${qs}`).then((r) => r.json());
  } catch { return; }
  const card = $('#history-card');
  if (!h || (!h.settled && !h.fills)) { card.hidden = true; return; }
  card.hidden = false;
  // the server clamps an out-of-range page (e.g. after a log rotation);
  // follow it so Prev/Next keep working off the page that actually exists
  historyPage = h.page || 1;
  const pnl = h.realised_pnl || 0;
  $('#history-kpis').innerHTML = [
    { l: 'Realised PnL, all runs', v: fmt.signed(pnl), n: 'sum of every settlement on disk',
      cls: pnl >= 0 ? 'pos' : 'neg' },
    { l: 'Settled', v: fmt.num(h.settled), n: `${h.wins}W / ${h.losses}L` },
    { l: 'Fills', v: fmt.num(h.fills), n: 'across every restart' },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value ${k.cls || ''}">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');
  renderHistoryChart(h.curve || []);
  renderHistoryTable(h.rows || []);
  renderHistoryPager(h);
}

pollHistory();
setInterval(pollHistory, 5000);

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
state.timer = setInterval(pollLive, 100);      // chart cadence
state.botTimer = setInterval(pollBot, 3000);   // process status
