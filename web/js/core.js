// Shared state, helpers, UI primitives (sheets, toasts), API + WebSocket.
export const tg = window.Telegram?.WebApp || null;

export const S = {
  session: '', uid: 0, chat: 0, admin: false, group: '',
  parts: new Map(), room: { settings: {}, rec: {}, count: 0 },
  music: null, chat: [], offset: 0, liked: new Set(), unread: 0,
};

// ---------- tiny event bus ----------
const listeners = {};
export const on = (t, fn) => ((listeners[t] ||= []).push(fn), fn);
export const emit = (t, d) => (listeners[t] || []).forEach(fn => { try { fn(d); } catch (e) { console.error(e); } });

// ---------- storage (may be unavailable) ----------
export const store = {
  get(k, d) { try { const v = localStorage.getItem('vc:' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('vc:' + k, JSON.stringify(v)); } catch {} },
};

// ---------- DOM ----------
// replaceChildren/append that silently skip null/false (lets UI code use `cond ? el : null`)
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const fn of ['replaceChildren', 'append']) {
    const orig = proto[fn];
    proto[fn] = function (...k) { return orig.apply(this, k.filter(x => x != null && x !== false && x !== '')); };
  }
}
export function h(tag, attrs, ...kids) {
  if (attrs != null && (attrs.nodeType || typeof attrs !== 'object' || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') { for (const [sk, sv] of Object.entries(v)) sk.startsWith('--') ? el.style.setProperty(sk, sv) : (el.style[sk] = sv); }
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
}
export function icon(name, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(ns, 'svg');
  s.setAttribute('class', 'ico ' + cls);
  const u = document.createElementNS(ns, 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}
export const $ = (s, r = document) => r.querySelector(s);

export function haptic(kind = 'light') {
  try {
    if (['success', 'warning', 'error'].includes(kind)) tg?.HapticFeedback?.notificationOccurred(kind);
    else if (kind === 'select') tg?.HapticFeedback?.selectionChanged();
    else tg?.HapticFeedback?.impactOccurred(kind);
  } catch {}
}

// ---------- formatting ----------
export function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60), r = s % 60;
  if (m >= 60) return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  return `${m}:${String(r).padStart(2, '0')}`;
}
export function fmtDur(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const hh = Math.floor(m / 60);
  return `${hh}h ${m % 60}m`;
}
export function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  const y = new Date(Date.now() - 864e5);
  if (d.toDateString() === y.toDateString()) return `Yesterday ${time}`;
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + time;
}
const GRADS = [['#ff5e62', '#ff9966'], ['#5f72bd', '#9b23ea'], ['#11998e', '#38ef7d'], ['#fc5c7d', '#6a82fb'],
  ['#f7971e', '#ffd200'], ['#00c6ff', '#0072ff'], ['#ee0979', '#ff6a00'], ['#8e2de2', '#4a00e0']];
export function grad(id) {
  const g = GRADS[Math.abs(Number(id) || 0) % GRADS.length];
  return `linear-gradient(135deg, ${g[0]}, ${g[1]})`;
}
export function initials(name = '?') {
  const p = name.trim().split(/\s+/);
  return ((p[0]?.[0] || '?') + (p[1]?.[0] || '')).toUpperCase();
}
export function avatarEl(p, cls = 'img') {
  const el = h('div', { class: cls, style: { '--g': grad(p.uid) } });
  el.textContent = initials(p.name);
  if (p.photo) {
    const img = h('img', { src: p.photo.startsWith('/api/') ? media(p.photo) : p.photo, alt: '', referrerpolicy: 'no-referrer' });
    img.style.opacity = '0';
    img.onload = () => { el.textContent = ''; el.append(img); img.style.opacity = '1'; };
  }
  return el;
}

// ---------- server time ----------
export const serverNow = () => Date.now() / 1000 + S.offset;
export const media = (path) => path + (path.includes('?') ? '&' : '?') + 's=' + encodeURIComponent(S.session);

// ---------- API ----------
export async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: { 'X-Session': S.session } };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, init);
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

// ---------- WebSocket ----------
let ws = null, wsTimer = null, retry = 0, closedByUs = false, asGroup = false;
const outbox = [];
export function send(msg) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg));
  else if (outbox.length < 20) outbox.push(msg);
}
export function connectWS(opts = {}) {
  if (opts.asGroup != null) asGroup = opts.asGroup;
  closedByUs = false;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?s=${encodeURIComponent(S.session)}${asGroup ? '&asg=1' : ''}`);
  ws.onopen = () => {
    retry = 0;
    while (outbox.length) ws.send(JSON.stringify(outbox.shift()));
    syncClock();
    clearInterval(wsTimer);
    wsTimer = setInterval(syncClock, 30000);
  };
  ws.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.t === 'pong') return onPong(m);
    if (m.now && m.t === 'init') S.offset = m.now - Date.now() / 1000;
    emit('ws:' + m.t, m);
  };
  ws.onclose = (e) => {
    clearInterval(wsTimer);
    if (closedByUs) return;
    if (e.code === 4003) return emit('fatal', { title: 'Removed', text: 'You were removed from this voice chat.' });
    if (e.code === 4004) return emit('fatal', { title: 'Locked', text: 'Admins locked this voice chat.' });
    if (e.code === 4001) return emit('fatal', { title: 'Session expired', text: 'Close and reopen the voice chat from your group.' });
    emit('ws:down');
    retry = Math.min(retry + 1, 6);
    setTimeout(() => connectWS(), 500 * 2 ** retry);
  };
}
export function reconnectWS(opts) { closedByUs = true; try { ws?.close(); } catch {} setTimeout(() => connectWS(opts), 200); }
export function closeWS() { closedByUs = true; try { ws?.close(); } catch {} }
let pingId = 0; const pings = {};
function syncClock() { const id = ++pingId; pings[id] = Date.now() / 1000; send({ t: 'ping', id }); }
function onPong(m) {
  const t0 = pings[m.id]; delete pings[m.id];
  if (!t0) return;
  const t1 = Date.now() / 1000;
  S.offset = m.now - (t0 + t1) / 2;
}

// ---------- toasts ----------
export function toast(text, ms = 2600) {
  const el = h('div', { class: 'toast' }, text);
  const box = $('#toasts');
  while (box.children.length >= 2) box.firstChild.remove();
  box.append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, ms);
}

// ---------- bottom sheets ----------
const stack = [];
export function sheet({ title = '', body, tall = false, actions = null, onClose = null, cls = '' }) {
  const bd = h('div', { class: 'backdrop' });
  const head = h('div', { class: 'sh-head' }, h('h2', null, title), actions, h('button', { class: 'close-x', 'aria-label': 'Close', onclick: () => close() }, icon('close')));
  const bodyEl = h('div', { class: 'sh-body' });
  if (body) bodyEl.append(body);
  const el = h('div', { class: `sheet ${tall ? 'tall' : ''} ${cls}`, role: 'dialog' }, h('div', { class: 'grab' }, h('i')), head, bodyEl);
  document.body.append(bd, el);
  requestAnimationFrame(() => requestAnimationFrame(() => { bd.classList.add('show'); el.classList.add('show'); }));
  let closed = false;
  function close() {
    if (closed) return; closed = true;
    bd.classList.remove('show'); el.classList.remove('show');
    el.style.transform = '';
    setTimeout(() => { bd.remove(); el.remove(); }, 450);
    const i = stack.indexOf(api_); if (i >= 0) stack.splice(i, 1);
    syncBack();
    onClose?.();
  }
  bd.onclick = close;
  dragToClose(el, [el.querySelector('.grab'), head], close);
  const api_ = { el, body: bodyEl, head, close, setTitle: (t) => (head.querySelector('h2').textContent = t) };
  stack.push(api_);
  syncBack();
  return api_;
}
export function dragToClose(el, handles, close) {
  let y0 = 0, dy = 0, dragging = false;
  const down = (e) => { if (e.target.closest('button,input')) return; dragging = true; y0 = e.clientY; dy = 0; el.style.transition = 'none'; e.target.setPointerCapture?.(e.pointerId); };
  const move = (e) => { if (!dragging) return; dy = Math.max(0, e.clientY - y0); el.style.transform = `translateY(${dy}px)`; };
  const up = () => {
    if (!dragging) return; dragging = false; el.style.transition = '';
    if (dy > 110) close(); else el.style.transform = '';
  };
  handles.filter(Boolean).forEach(hd => { hd.addEventListener('pointerdown', down); hd.addEventListener('pointermove', move); hd.addEventListener('pointerup', up); hd.addEventListener('pointercancel', up); });
}
export const closeTopSheet = () => { const s = stack[stack.length - 1]; if (s) { s.close(); return true; } return false; };
const backHandlers = [];
export function pushBack(fn) { backHandlers.push(fn); syncBack(); return () => { const i = backHandlers.indexOf(fn); if (i >= 0) backHandlers.splice(i, 1); syncBack(); }; }
function syncBack() {
  try { (stack.length || backHandlers.length) ? tg?.BackButton?.show() : tg?.BackButton?.hide(); } catch {}
}
try { tg?.BackButton?.onClick(() => { if (!closeTopSheet()) backHandlers[backHandlers.length - 1]?.(); }); } catch {}

export function confirmBox(text, okLabel = 'OK', danger = false) {
  return new Promise((res) => {
    const body = h('div', null,
      h('p', { style: { margin: '0 0 16px', color: 'var(--fg2)' } }, text),
      h('div', { style: { display: 'grid', gap: '8px' } },
        h('button', { class: 'btn block ' + (danger ? 'danger' : ''), onclick: () => { res(true); s.close(); } }, okLabel),
        h('button', { class: 'btn soft block', onclick: () => s.close() }, 'Cancel')));
    const s = sheet({ title: 'Are you sure?', body, onClose: () => res(false) });
  });
}

export function switchRow(label, sub, value, onchange) {
  const sw = h('span', { class: 'switch' + (value ? ' on' : '') });
  const row = h('button', { class: 'row', role: 'switch', 'aria-checked': String(!!value), onclick: () => {
    const v = !sw.classList.contains('on'); sw.classList.toggle('on', v); row.setAttribute('aria-checked', String(v)); haptic('select'); onchange(v);
  } }, h('span', { class: 'lab' }, label, sub ? h('small', null, sub) : null), sw);
  return row;
}
export function actionRow(ic, label, onclick, cls = '', sub = null) {
  return h('button', { class: 'row ' + cls, onclick }, ic ? icon(ic) : null, h('span', { class: 'lab' }, label, sub ? h('small', null, sub) : null));
}
export function copyText(text) {
  try { navigator.clipboard.writeText(text); toast('Copied'); }
  catch { const t = h('textarea', null, text); document.body.append(t); t.select(); document.execCommand('copy'); t.remove(); toast('Copied'); }
}
