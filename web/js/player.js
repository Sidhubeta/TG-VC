// Music: synced playback engine, Apple Music style UI, lyrics, queue, search, playlists, effects.
import { $, S, actionRow, api, avatarEl, confirmBox, dragToClose, emit, fmtTime, fullView, h, haptic, icon, media, off, on, pushBack, send, serverNow, sheet, store, tg, toast } from './core.js';
import { getCtx } from './sounds.js';

// ======================= engine =======================
const audio = new Audio();
audio.preload = 'auto';
audio.crossOrigin = 'anonymous';
audio.preservesPitch = audio.mozPreservesPitch = audio.webkitPreservesPitch = false; // Slowed / Nightcore change pitch too
const video = h('video', { class: 'vid', playsinline: true, muted: true, preload: 'auto' });
video.muted = true;
const isApple = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent) && 'ontouchend' in document || /iPhone|iPad|iPod/.test(navigator.userAgent);
const CODEC = !isApple && audio.canPlayType('audio/webm; codecs="opus"') ? 'opus' : 'aac';

const P = {
  mode: store.get('mode', 'audio'),          // audio | video
  aq: store.get('aq', 'medium'),             // low | medium | high
  vq: store.get('vq', 'auto'),               // auto | 144..1080
  vol: store.get('vol', 1),
  localPaused: false, loadedQid: null, blocked: false, heights: [], autoH: 480, maxH: 480, stalls: [], stableSince: 0,
};
// Effects are shared: the server holds them and everyone hears the same thing, in sync.
const FX_NONE = { preset: 'normal', bass: 0, mid: 0, treble: 0, reverb: 0, rate: 1, mode: 'none' };
const fx = () => S.music?.fx || FX_NONE;
const rate = () => S.music?.rate || 1;
let G = null; // WebAudio graph

const cur = () => S.music?.current || null;
const playing = () => S.music?.status === 'playing';
export function expectedPos() {
  const m = S.music; if (!m?.current) return 0;
  const p = m.status === 'playing' ? m.pos + (serverNow() - m.upd) * (m.rate || 1) : m.pos;
  const d = m.current.duration || Infinity;
  return Math.max(0, Math.min(p, d));
}

function audioSrc(t) { return media(`/api/stream/${t.id}/audio?c=${CODEC}&q=${P.aq}`); }
function videoHeight() {
  if (P.vq !== 'auto') return Number(P.vq);
  return P.autoH;
}
const STEPS = [144, 240, 360, 480, 720, 1080];
function pickAutoH() {
  const c = navigator.connection;
  const dl = c?.downlink || 5;
  P.maxH = c?.saveData ? 240 : dl >= 8 ? 720 : dl >= 3 ? 480 : dl >= 1.2 ? 360 : 240;
  P.autoH = Math.min(P.maxH, 480); // start modest so the video appears fast, then step up if it plays smoothly
  P.stalls = []; P.stableSince = Date.now();
}
pickAutoH();

function load(t) {
  P.loadedQid = t ? t.qid : null;
  P.localPaused = false;
  if (!t) { audio.removeAttribute('src'); audio.load(); unloadVideo(); return; }
  audio.src = audioSrc(t);
  audio.load();
  if (P.mode === 'video') loadVideo(t);
  else unloadVideo();
}
let lastVideoLoad = 0;
function loadVideo(t) {
  t = t || cur(); if (!t) return;
  const src = media(`/api/stream/${t.id}/video?q=${videoHeight()}`);
  if (video.dataset.src === src) return;
  video.dataset.src = src;
  video.poster = media(t.thumb); // show the artwork while the video loads instead of a black box
  video.src = src;
  video.load();
  lastVideoLoad = Date.now();
  setVideoLoading(true);
}
function setVideoLoading(on) { video.parentNode?.classList.toggle('loading', on); }
video.addEventListener('waiting', () => setVideoLoading(true));
video.addEventListener('playing', () => setVideoLoading(false));
video.addEventListener('canplay', () => setVideoLoading(false));
function unloadVideo() { if (video.dataset.src) { video.removeAttribute('src'); delete video.dataset.src; video.load(); } }

audio.addEventListener('loadedmetadata', () => sync(true));
audio.addEventListener('ended', () => { const c = cur(); if (c && !c.duration) send({ t: 'music.ended', qid: c.qid }); });
// retry a failed stream a few times (with backoff), not forever every 3s
let errQid = null, errCount = 0;
audio.addEventListener('error', () => {
  const t = cur(); if (!t || !audio.getAttribute('src')) return;
  console.warn('audio error', audio.error);
  if (errQid !== t.qid) { errQid = t.qid; errCount = 0; }
  if (++errCount > 4) { if (errCount === 5) toast('This song can’t be played right now'); return; }
  setTimeout(() => { const c = cur(); if (c && c.qid === t.qid && audio.error) { audio.src = audioSrc(c); audio.load(); } }, 2000 * errCount);
});
// Auto quality. Only real stalls count: "waiting" also fires after every load and every sync seek, and
// counting those made Auto drop a step every few seconds until it got stuck at 144p.
video.addEventListener('waiting', () => {
  if (P.vq !== 'auto' || audio.paused) return;
  const now = Date.now();
  if (now - lastVideoLoad < 8000 || now - lastVideoSeek < 4000) return;
  P.stalls = P.stalls.filter(x => now - x < 30000); P.stalls.push(now);
  P.stableSince = now;
  const i = STEPS.indexOf(P.autoH);
  if (P.stalls.length >= 3 && i > 1) { // never drop below 240p on its own
    setAutoH(STEPS[i - 1], `Video lowered to ${STEPS[i - 1]}p for smoother playback`);
  }
});
function setAutoH(hgt, msg) {
  P.autoH = hgt; P.stalls = []; P.stableSince = Date.now();
  if (msg) toast(msg);
  const t = cur(); if (t && video.dataset.src) { delete video.dataset.src; loadVideo(t); }
}
function maybeStepUp() {
  // after 90s without a stall, try one step better (up to what the connection allows)
  if (P.vq !== 'auto' || !video.dataset.src || video.paused || P.autoH >= P.maxH) return;
  if (Date.now() - P.stableSince > 90000) setAutoH(STEPS[STEPS.indexOf(P.autoH) + 1]);
}
video.addEventListener('loadedmetadata', () => { seekVideo(); if (!audio.paused) video.play().catch(() => {}); });

let lastVideoSeek = 0;
function seekVideo() {
  // aim a little ahead: after a seek the video needs a moment to buffer before it moves again
  try { video.currentTime = Math.min(audio.currentTime + 0.3, video.duration || Infinity); lastVideoSeek = Date.now(); } catch {}
}

function sync(force = false) {
  const t = cur();
  if (!t) { if (!audio.paused) audio.pause(); return; }
  if (t.qid !== P.loadedQid) load(t);
  const want = playing() && !P.localPaused;
  const r = rate();
  if (audio.playbackRate !== r) audio.playbackRate = r;
  if (audio.readyState >= 1 && !P.localPaused) {
    const drift = audio.currentTime - expectedPos();
    // Jump only when clearly out of sync. Tiny corrections (and speeding up / slowing down to catch up,
    // which the old code did every 0.7s) are what made songs glitch and wobble.
    if ((force && Math.abs(drift) > 0.35) || Math.abs(drift) > 1.2 || (!want && Math.abs(drift) > 0.5)) {
      try { audio.currentTime = expectedPos(); } catch {}
    }
  }
  if (want && audio.paused) audio.play().then(() => setBlocked(false)).catch(() => setBlocked(true));
  else if (!want && !audio.paused) audio.pause();
  // The music runs through our effects (Web Audio). Phones suspend that after a call interruption, locking
  // the screen or switching apps, and the song kept "playing" silently until it was paused and resumed.
  if (want && G && G.c.state !== 'running') {
    G.c.resume().then(() => setBlocked(false), () => {});
    if (G.c.state !== 'running') setBlocked(true);
  }
  maybeStepUp();
  // the video follows the audio, but never chases it while it's still loading (that made it loop one second)
  if (video.dataset.src && video.readyState >= 1) {
    if (video.playbackRate !== r) video.playbackRate = r;
    const vd = video.currentTime - audio.currentTime;
    if (Math.abs(vd) > 1 && !video.seeking && video.readyState >= 3 && Date.now() - lastVideoSeek > 4000) seekVideo();
    if (!audio.paused && video.paused && video.isConnected) video.play().catch(() => {});
    if (audio.paused && !video.paused) video.pause();
  }
}
setInterval(sync, 700);
// coming back to the app: jump to where the room is now (not where it was when you left)
const back = () => { G?.c.resume().catch(() => {}); sync(true); };
document.addEventListener('visibilitychange', () => { if (!document.hidden) back(); });
window.addEventListener('focus', back);
try { tg?.onEvent?.('activated', back); } catch {}

let hintAt = 0;
function setBlocked(b) {
  if (P.blocked === b) return;
  P.blocked = b;
  emit('music:blocked', b);
  // no "Tap to listen" button any more; a short hint once in a while is enough (any tap fixes it)
  if (b && cur() && Date.now() - hintAt > 30000) { hintAt = Date.now(); toast('🔈 Tap anywhere to hear the music'); }
}
export const musicBlocked = () => P.blocked;
export function unlockAudio() {
  getCtx();
  buildGraph();
  if (cur() && playing() && !P.localPaused) audio.play().then(() => setBlocked(false)).catch(() => {});
}

// ======================= picture in picture =======================
export function pipSupported(v) {
  return !!(document.pictureInPictureEnabled && v.requestPictureInPicture) || !!v.webkitSupportsPresentationMode?.('picture-in-picture');
}
export async function pip(v) {
  try {
    if (document.pictureInPictureElement === v) return document.exitPictureInPicture();
    if (v.requestPictureInPicture && document.pictureInPictureEnabled) return await v.requestPictureInPicture();
    if (v.webkitSetPresentationMode) return v.webkitSetPresentationMode(v.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');
  } catch (e) { toast('Picture in picture isn’t available here'); }
}

// ======================= effects (WebAudio) =======================
function impulse(c, secs, decay) {
  const len = Math.floor(c.sampleRate * secs);
  const buf = c.createBuffer(2, len, c.sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
  }
  return buf;
}
function buildGraph() {
  if (G) return;
  const c = getCtx(); if (!c) return;
  try {
    const src = c.createMediaElementSource(audio);
    const bass = c.createBiquadFilter(); bass.type = 'lowshelf'; bass.frequency.value = 110;
    const mid = c.createBiquadFilter(); mid.type = 'peaking'; mid.frequency.value = 1300; mid.Q.value = 0.9;
    const treble = c.createBiquadFilter(); treble.type = 'highshelf'; treble.frequency.value = 7000;
    const pan = c.createStereoPanner ? c.createStereoPanner() : c.createGain();
    const lfo = c.createOscillator(); lfo.frequency.value = 0.11;
    const lfoAmt = c.createGain(); lfoAmt.gain.value = 0;
    lfo.connect(lfoAmt); if (pan.pan) lfoAmt.connect(pan.pan); lfo.start();
    const dry = c.createGain();
    const conv = c.createConvolver(); conv.buffer = impulse(c, 2.6, 2.4);
    const wet = c.createGain(); wet.gain.value = 0;
    const comp = c.createDynamicsCompressor(); comp.threshold.value = -12; comp.ratio.value = 3; comp.attack.value = 0.01; comp.release.value = 0.2;
    const master = c.createGain(); master.gain.value = P.vol;
    src.connect(bass).connect(mid).connect(treble).connect(pan);
    pan.connect(dry).connect(comp);
    pan.connect(conv).connect(wet).connect(comp);
    comp.connect(master).connect(c.destination);
    G = { c, bass, mid, treble, pan, lfoAmt, dry, wet, master };
    applyFx();
    const sink = store.get('dev:audiooutput', '');
    if (sink) setMusicSink(sink);
  } catch (e) { console.warn('fx graph failed', e); }
}
// Effects that change only small details or needed headphones to notice (Spatial 3D, Treble) were dropped.
const PRESETS = {
  normal: { label: 'Normal', e: '🎵', v: { bass: 0, mid: 0, treble: 0, reverb: 0, rate: 1, mode: 'none' } },
  bass: { label: 'Bass Boost', e: '🔊', v: { bass: 9, mid: 0, treble: 1, reverb: 0, rate: 1, mode: 'none' } },
  '8d': { label: '8D Audio', e: '🎧', v: { bass: 2, mid: 0, treble: 1, reverb: 0.22, rate: 1, mode: '8d' } },
  hall: { label: 'Concert Hall', e: '🏛️', v: { bass: 2, mid: 0, treble: 1, reverb: 0.5, rate: 1, mode: 'none' } },
  vocal: { label: 'Vocal', e: '🎤', v: { bass: -4, mid: 6, treble: 3, reverb: 0.05, rate: 1, mode: 'none' } },
  slowed: { label: 'Slowed + Reverb', e: '🌙', v: { bass: 3, mid: 0, treble: -1, reverb: 0.42, rate: 0.85, mode: 'none' } },
  nightcore: { label: 'Nightcore', e: '⚡', v: { bass: 1, mid: 0, treble: 2, reverb: 0, rate: 1.25, mode: 'none' } },
};
function applyFx(f = fx()) {
  if (!G) return;
  const t = G.c.currentTime;
  G.bass.gain.setTargetAtTime(f.bass, t, 0.02);
  G.mid.gain.setTargetAtTime(f.mid, t, 0.02);
  G.treble.gain.setTargetAtTime(f.treble, t, 0.02);
  G.wet.gain.setTargetAtTime(f.reverb * 1.4, t, 0.04);
  G.dry.gain.setTargetAtTime(1 - f.reverb * 0.35, t, 0.04);
  G.lfoAmt.gain.setTargetAtTime(f.mode === '8d' ? 0.95 : 0, t, 0.04);
  if (f.mode !== '8d' && G.pan.pan) G.pan.pan.setTargetAtTime(0, t, 0.04);
}
function setVol(v) {
  P.vol = v; store.set('vol', v);
  if (G) G.master.gain.setTargetAtTime(v, G.c.currentTime, 0.05);
  else audio.volume = v;
}
export const getMusicVolume = () => P.vol;
export const setMusicVolume = (v) => setVol(v);
// speaker / Bluetooth choice from the Audio sheet also moves the music
async function setMusicSink(id) {
  try {
    if (G?.c.setSinkId) await G.c.setSinkId(id);
    else if (audio.setSinkId) await audio.setSinkId(id);
  } catch (e) { console.warn('music output switch failed', e); }
}
on('voice:sink', setMusicSink);
export function musicTap() { return G ? { ctx: G.c, node: G.master } : null; }

// ======================= colors from artwork =======================
const colorCache = new Map();
function setColors(cols) {
  const r = document.documentElement.style;
  r.setProperty('--c1', cols[0]); r.setProperty('--c2', cols[1]); r.setProperty('--c3', cols[2]);
}
function extractColors(url) {
  if (colorCache.has(url)) return setColors(colorCache.get(url));
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    try {
      const cv = document.createElement('canvas'); cv.width = cv.height = 24;
      const x = cv.getContext('2d', { willReadFrequently: true });
      x.drawImage(img, 0, img.height * 0.125, img.width, img.height * 0.75, 0, 0, 24, 24);
      const d = x.getImageData(0, 0, 24, 24).data;
      const buckets = new Map();
      for (let i = 0; i < d.length; i += 4) {
        const [r, g, b] = [d[i], d[i + 1], d[i + 2]];
        const key = (r >> 5) << 6 | (g >> 5) << 3 | (b >> 5);
        const e = buckets.get(key) || { r: 0, g: 0, b: 0, n: 0 };
        e.r += r; e.g += g; e.b += b; e.n++; buckets.set(key, e);
      }
      const cols = [...buckets.values()].map(e => {
        const r = e.r / e.n, g = e.g / e.n, b = e.b / e.n;
        const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
        const sat = mx ? (mx - mn) / mx : 0, lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        return { r, g, b, n: e.n, sat, lum, score: e.n * (0.35 + sat) * (lum > 0.08 && lum < 0.92 ? 1 : 0.2) };
      }).sort((a, b) => b.score - a.score);
      const css = (c, k = 1) => `rgb(${Math.round(c.r * k)},${Math.round(c.g * k)},${Math.round(c.b * k)})`;
      const pick = [cols[0], cols[1] || cols[0], cols[2] || cols[0]];
      const vivid = [...cols].sort((a, b) => (b.sat * (b.lum > 0.25 ? 1 : 0.3)) - (a.sat * (a.lum > 0.25 ? 1 : 0.3)))[0] || cols[0];
      const boost = vivid.lum < 0.35 ? 1.6 : 1;
      const res = [css(pick[0]), css(pick[1]), css(pick[2]), css(vivid, boost)];
      colorCache.set(url, res); setColors(res);
    } catch (e) { /* tainted or failed */ }
  };
  img.src = url;
}

// ======================= mini card =======================
const card = () => $('#np-card');
let likedLoaded = false;
async function loadLiked() {
  if (likedLoaded) return; likedLoaded = true;
  try { const r = await api('/api/like'); S.liked = new Set(r.ids); } catch {}
}
export function renderCard() {
  const el = card(); if (!el) return;
  const t = cur();
  el.replaceChildren();
  el.classList.toggle('empty', !t);
  document.body.classList.toggle('playing', !!t && playing() && !P.localPaused);
  if (!t) {
    el.onclick = () => openSearch('home');
    el.append(h('div', { class: 'art' }, icon('music')),
      h('div', { class: 'meta' }, h('div', { class: 'ttl' }, 'Nothing playing'), h('div', { class: 'art-sub' }, canAdd() ? 'Tap to search songs or paste a YouTube link' : 'Admins will play music here')),
      canAdd() ? h('div', { class: 'ctl' }, h('button', { 'aria-label': 'Search', onclick: (e) => { e.stopPropagation(); openSearch(); } }, icon('plus'))) : null);
    return;
  }
  el.onclick = () => openNP();
  const isPlay = playing() && !P.localPaused;
  const showVid = P.mode === 'video' && !NP.open && !FV;
  el.classList.toggle('has-video', showVid);
  el.append(
    showVid ? videoBox('card-vid') : null,
    h('div', { class: 'art', style: { backgroundImage: `url("${media(t.thumb)}")` } }),
    h('div', { class: 'meta' },
      h('div', { class: 'ttl' }, h('span', { class: 'eq' }, h('i'), h('i'), h('i')), t.title),
      h('div', { class: 'art-sub' }, t.artist + (t.by ? ` · added by ${t.by}` : ''))),
    h('div', { class: 'ctl' },
      h('button', { 'aria-label': isPlay ? 'Pause' : 'Play', onclick: (e) => { e.stopPropagation(); togglePlay(); } }, icon(isPlay ? 'pause' : 'play', 'fill')),
      h('button', { 'aria-label': 'Next', onclick: (e) => { e.stopPropagation(); next(); } }, icon('next', 'fill'))),
    h('div', { class: 'bar', id: 'np-bar' }));
}
function canAdd() { return S.admin || !S.room.settings?.music_admin_only; }

// ---- music video: one <video> element that lives in the player, in the room card, or full screen ----
let FV = null;
function videoBox(cls) {
  const box = h('div', { class: cls + (video.readyState < 3 ? ' loading' : ''), onclick: (e) => e.stopPropagation() }, video, h('span', { class: 'vspin' }),
    h('button', { class: 'round vbtn', 'aria-label': 'Full screen', onclick: (e) => { e.stopPropagation(); openVideoFull(); } }, icon('expand')),
    pipSupported(video) ? h('button', { class: 'round vbtn pip', 'aria-label': 'Picture in picture', onclick: (e) => { e.stopPropagation(); pip(video); } }, icon('pip')) : null);
  if (P.mode === 'video') loadVideo();
  return box;
}
function openVideoFull() {
  if (FV) return;
  haptic('light');
  FV = fullView(video, { label: cur()?.title || '', onClose: () => { FV = null; renderCard(); if (NP.open) renderStage(); } });
}

// ======================= controls =======================
function togglePlay() {
  haptic('light');
  unlockAudio();
  const m = S.music; if (!m) return;
  if (S.admin) {
    if (P.localPaused) { P.localPaused = false; sync(true); refresh(); return; }
    send({ t: m.status === 'playing' ? 'music.pause' : 'music.play' });
  } else {
    // non-admins pause only for themselves; resuming jumps back to live
    P.localPaused = !P.localPaused;
    sync(true); refresh();
    if (P.localPaused) toast('Paused for you only');
  }
}
function next() {
  haptic('medium');
  const t = cur(); if (!t) return;
  if (S.admin || t.byUid === S.uid) send({ t: 'music.skip' });
  else { send({ t: 'music.voteskip' }); toast('Voted to skip'); }
}
function prev() { haptic('light'); if (S.admin) send({ t: 'music.prev' }); }

// ======================= full player =======================
const NP = { open: false, el: null, view: 'art', lyrics: null, lyrQid: null, scrubbing: false };
function buildNP() {
  if (NP.el) return NP.el;
  const el = h('div', { class: 'np', role: 'dialog', 'aria-label': 'Now playing' },
    h('div', { class: 'bg' }, h('i'), h('i'), h('i')),
    h('div', { class: 'grab' }, h('i')),
    h('div', { class: 'body' },
      h('div', { class: 'stage-np', id: 'np-stage' }),
      h('div', { class: 'info' },
        h('div', { class: 't' }, h('h2', { id: 'np-t' }), h('p', { id: 'np-a' }), h('small', { id: 'np-by' })),
        h('button', { class: 'round heart', id: 'np-like', 'aria-label': 'Like', onclick: like }, icon('heart')),
        h('button', { class: 'round', 'aria-label': 'More', onclick: trackMenu }, icon('more'))),
      h('div', { class: 'scrub', id: 'np-scrub' }, h('div', { class: 'tr' })),
      h('div', { class: 'times' }, h('span', { id: 'np-cur' }, '0:00'), h('span', { id: 'np-rem' }, '-0:00')),
      h('div', { class: 'ctrls' },
        h('button', { class: 'sm', id: 'np-prev', 'aria-label': 'Previous', onclick: prev }, icon('prev', 'fill')),
        h('button', { class: 'lg', id: 'np-pp', 'aria-label': 'Play', onclick: togglePlay }, icon('play', 'fill')),
        h('button', { class: 'sm', id: 'np-next', 'aria-label': 'Next', onclick: next }, icon('next', 'fill'), h('span', { class: 'votes', id: 'np-votes' }))),
      h('div', { class: 'vol' }, icon('vol-x'), volSlider(), icon('vol')),
      h('div', { class: 'np-tabs' },
        h('button', { id: 'tab-lyr', 'aria-label': 'Lyrics', onclick: () => setView(NP.view === 'lyrics' ? 'art' : 'lyrics') }, icon('lyrics')),
        h('button', { id: 'tab-vid', 'aria-label': 'Audio or video', onclick: toggleMode }, icon('video')),
        h('button', { id: 'tab-hq', 'aria-label': 'Quality', onclick: openQuality }, icon('hq')),
        h('button', { id: 'tab-fx', 'aria-label': 'Sound effects', onclick: openFx }, icon('sparkle')),
        h('button', { id: 'tab-q', 'aria-label': 'Queue', onclick: () => setView(NP.view === 'queue' ? 'art' : 'queue') }, icon('queue')))));
  document.body.append(el);
  dragToClose(el, [el.querySelector('.grab'), el.querySelector('.info')], closeNP);
  setupScrub(el.querySelector('#np-scrub'));
  NP.el = el;
  return el;
}
function volSlider() {
  const r = h('input', { type: 'range', min: 0, max: 1, step: 0.01, value: P.vol, 'aria-label': 'Volume', style: { color: '#fff' } });
  const paint = () => r.style.setProperty('--v', (r.value * 100) + '%');
  r.oninput = () => { unlockAudio(); setVol(Number(r.value)); paint(); };
  paint();
  return r;
}
let popBack = null;
export function openNP() {
  if (!cur()) return openSearch('home');
  unlockAudio();
  loadLiked();
  const el = buildNP();
  NP.open = true;
  requestAnimationFrame(() => el.classList.add('show'));
  popBack = pushBack(closeNP);
  if (P.mode === 'video') loadVideo();
  setView(NP.view);
  refresh();
}
function closeNP() {
  if (!NP.open) return;
  NP.open = false;
  NP.el.classList.remove('show');
  NP.el.style.transform = '';
  popBack?.(); popBack = null;
  renderCard(); // in video mode the video moves to the room screen and keeps playing
}
function setView(v) {
  NP.view = v;
  if (!NP.el) return;
  ['lyr', 'q'].forEach(k => NP.el.querySelector('#tab-' + k).classList.toggle('on', (k === 'lyr' && v === 'lyrics') || (k === 'q' && v === 'queue')));
  renderStage();
}
function renderStage() {
  if (!NP.el) return;
  const st = NP.el.querySelector('#np-stage');
  const t = cur();
  st.replaceChildren();
  if (!t) return;
  if (NP.view === 'lyrics') return renderLyrics(st, t);
  if (NP.view === 'queue') return renderQueue(st);
  if (P.mode === 'video' && !FV) st.append(videoBox('np-vid'));
  else st.append(h('div', { class: 'big-art', style: { backgroundImage: `url("${media(t.thumb)}")` } }));
}

// ---- scrubber (admins seek for everyone) ----
function setupScrub(sc) {
  let drag = false, ratio = 0;
  const at = (e) => { const r = sc.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
  sc.addEventListener('pointerdown', (e) => { if (!S.admin || !cur()?.duration) return; drag = true; NP.scrubbing = true; sc.classList.add('drag'); sc.setPointerCapture(e.pointerId); ratio = at(e); paintScrub(ratio); });
  sc.addEventListener('pointermove', (e) => { if (!drag) return; ratio = at(e); paintScrub(ratio); });
  const end = () => {
    if (!drag) return; drag = false; NP.scrubbing = false; sc.classList.remove('drag');
    const d = cur()?.duration; if (d) { send({ t: 'music.seek', pos: ratio * d }); haptic('select'); }
  };
  sc.addEventListener('pointerup', end); sc.addEventListener('pointercancel', end);
}
function paintScrub(ratio) {
  const el = NP.el; if (!el) return;
  const d = cur()?.duration || 0;
  el.querySelector('#np-scrub').style.setProperty('--p', (ratio * 100) + '%');
  el.querySelector('#np-cur').textContent = fmtTime(ratio * d);
  el.querySelector('#np-rem').textContent = d ? '-' + fmtTime(d - ratio * d) : '';
}
function tickUI() {
  const t = cur();
  const pos = t ? (P.localPaused ? audio.currentTime : expectedPos()) : 0;
  const d = t?.duration || audio.duration || 0;
  const bar = $('#np-bar'); if (bar) bar.style.width = d ? `${Math.min(100, pos / d * 100)}%` : '0';
  if (NP.open && !NP.scrubbing && d) paintScrub(Math.min(1, pos / d));
  if (NP.open && NP.view === 'lyrics') highlightLyrics(pos);
}
setInterval(tickUI, 250);

export function refresh() {
  renderCard();
  if (!NP.el) return;
  const t = cur();
  if (!t) { if (NP.open) closeNP(); return; }
  const el = NP.el;
  const isPlay = playing() && !P.localPaused;
  el.classList.toggle('paused', !isPlay);
  el.querySelector('#np-t').textContent = t.title;
  el.querySelector('#np-a').textContent = t.artist;
  el.querySelector('#np-by').textContent = t.by ? `Added by ${t.by}` : '';
  const pp = el.querySelector('#np-pp'); pp.replaceChildren(icon(isPlay ? 'pause' : 'play', 'fill'));
  pp.setAttribute('aria-label', isPlay ? 'Pause' : 'Play');
  el.querySelector('#np-prev').disabled = !S.admin;
  const v = S.music;
  el.querySelector('#np-votes').textContent = !S.admin && t.byUid !== S.uid && v.votes ? `${v.votes}/${v.need}` : '';
  el.querySelector('#np-scrub').classList.toggle('ro', !S.admin);
  el.querySelector('#np-like').classList.toggle('on', S.liked.has(t.id));
  el.querySelector('#np-like').replaceChildren(icon('heart', S.liked.has(t.id) ? 'fill' : ''));
  el.querySelector('#tab-vid').classList.toggle('on', P.mode === 'video');
  el.querySelector('#tab-fx').classList.toggle('on', fx().preset !== 'normal');
  if (NP.view === 'queue') renderStage();
}

// ======================= server updates =======================
let lastQid = null, lastTimeline = '', lastFx = '';
on('ws:music', (m) => onMusic(m.m));
on('ws:init', (m) => onMusic(m.music));
function onMusic(m) {
  S.music = m;
  const t = m.current;
  if ((t?.qid ?? null) !== lastQid) {
    lastQid = t?.qid ?? null;
    NP.lyrics = null;
    if (t) extractColors(media(t.thumb));
    if (NP.open) renderStage();
  }
  // Re-align playback only when the timeline really changed (new song, pause, seek, speed). The old code
  // re-seeked on every update (someone adding a song, a vote…), which made the music skip for everyone.
  const timeline = [t?.qid, m.status, m.pos, m.upd, m.rate].join('|');
  sync(timeline !== lastTimeline);
  lastTimeline = timeline;
  const fxKey = JSON.stringify(m.fx || FX_NONE);
  if (fxKey !== lastFx) { lastFx = fxKey; applyFx(); emit('music:fx'); }
  refresh();
  emit('music:changed');
}

// ======================= lyrics =======================
async function renderLyrics(st, t) {
  const box = h('div', { class: 'lyr' });
  st.append(box);
  if (!NP.lyrics || NP.lyrQid !== t.qid) {
    box.append(h('p', { class: 'on', style: { fontSize: '18px' } }, 'Loading lyrics…'));
    try {
      const q = new URLSearchParams({ title: t.title, artist: t.artist, duration: t.duration || 0 });
      NP.lyrics = await api(`/api/lyrics/${t.id}?${q}`);
      NP.lyrQid = t.qid;
    } catch { NP.lyrics = { synced: null, plain: null }; }
    if (NP.view !== 'lyrics' || cur()?.qid !== t.qid) return;
  }
  box.replaceChildren();
  const L = NP.lyrics;
  if (L.synced) {
    NP.lines = L.synced.split('\n').map(l => {
      const m = l.match(/^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$/);
      return m ? { t: Number(m[1]) * 60 + Number(m[2]), text: m[3] || '♪' } : null;
    }).filter(Boolean);
    NP.lines.forEach((ln, i) => box.append(h('p', { 'data-i': i, onclick: () => { if (S.admin) send({ t: 'music.seek', pos: ln.t }); } }, ln.text)));
    NP.lyrIdx = -1;
  } else if (L.plain) {
    NP.lines = null;
    box.classList.add('plain');
    L.plain.split('\n').forEach(l => box.append(h('p', null, l || ' ')));
  } else {
    NP.lines = null;
    box.append(h('p', { class: 'on', style: { fontSize: '20px' } }, 'No lyrics found for this song'));
  }
}
function highlightLyrics(pos) {
  if (!NP.lines) return;
  let i = NP.lines.findIndex(l => l.t > pos + 0.25) - 1;
  if (i < -1) i = NP.lines.length - 1;
  if (i === NP.lyrIdx) return;
  NP.lyrIdx = i;
  const box = NP.el.querySelector('.lyr'); if (!box) return;
  box.querySelectorAll('p.on').forEach(p => p.classList.remove('on'));
  const p = box.querySelector(`p[data-i="${i}"]`);
  if (p) { p.classList.add('on'); box.scrollTo({ top: p.offsetTop - box.clientHeight * 0.32, behavior: 'smooth' }); }
}

// ======================= queue =======================
function renderQueue(st) {
  const m = S.music;
  const box = h('div', { class: 'qv' });
  const loopIcon = m.loop === 'one' ? 'repeat1' : 'repeat';
  box.append(
    h('div', { class: 'qbtns' },
      h('button', { onclick: () => S.admin ? send({ t: 'music.shuffle' }) : toast('Admins only'), 'aria-label': 'Shuffle' }, icon('shuffle'), 'Shuffle'),
      h('button', { class: m.loop !== 'off' ? 'on' : '', onclick: () => S.admin ? send({ t: 'music.loop', mode: { off: 'all', all: 'one', one: 'off' }[m.loop] }) : toast('Admins only') }, icon(loopIcon), m.loop === 'one' ? 'Repeat 1' : 'Repeat'),
      h('button', { class: m.autoplay ? 'on' : '', onclick: () => S.admin ? send({ t: 'music.autoplay', on: !m.autoplay }) : toast('Admins only') }, icon('infinity'), 'Autoplay')),
    h('div', { class: 'qh' }, h('span', null, 'Playing next'),
      h('span', { style: { display: 'flex', gap: '6px' } },
        m.queue.length ? h('button', { class: 'round', 'aria-label': 'Save queue as playlist', onclick: saveQueue }, icon('list-plus')) : null,
        S.admin && m.queue.length ? h('button', { class: 'round', 'aria-label': 'Clear queue', onclick: async () => { if (await confirmBox('Clear the whole queue?', 'Clear', true)) send({ t: 'music.clear' }); } }, icon('trash')) : null,
        canAdd() ? h('button', { class: 'round', 'aria-label': 'Add songs', onclick: () => openSearch('search') }, icon('plus')) : null)));
  if (!m.queue.length) box.append(h('p', { style: { color: 'rgba(255,255,255,.6)', margin: '14px 0' } }, m.autoplay ? 'Autoplay is on — similar songs will play when the queue ends.' : 'Queue is empty. When this song ends, the music stops.'));
  m.queue.forEach((t, i) => box.append(trackRow(t, {
    sub: `${t.artist}${t.by ? ' · ' + t.by : ''}`,
    btn: icon('more'),
    onBtn: () => queueMenu(t, i),
  })));
  st.append(box);
}
function queueMenu(t, i) {
  const mine = t.byUid === S.uid;
  const body = h('div', { class: 'list' },
    S.admin ? actionRow('play', 'Play now', () => { send({ t: 'music.playnow', qid: t.qid }); s.close(); }) : null,
    S.admin && i > 0 ? actionRow('up', 'Move to top', () => { send({ t: 'music.move', qid: t.qid, to: 0 }); s.close(); }) : null,
    actionRow('list-plus', 'Add to a playlist', () => { s.close(); addToPlaylist([t]); }),
    S.admin || mine ? actionRow('trash', 'Remove from queue', () => { send({ t: 'music.remove', qid: t.qid }); s.close(); }, 'red') : null);
  const s = sheet({ title: t.title, body });
}

// ======================= track helpers =======================
function trackRow(t, { sub, btn, onBtn, onClick, now: isNow } = {}) {
  return h('div', { class: 'trk' + (isNow ? ' now' : ''), onclick: onClick || null },
    h('div', { class: 'ta', style: { backgroundImage: `url("${media(t.thumb || '/api/thumb/' + t.id)}")` } }),
    h('div', { class: 'tm' }, h('div', { class: 'tt' }, t.title), h('div', { class: 'ts' }, sub ?? `${t.artist}${t.duration ? ' · ' + fmtTime(t.duration) : ''}`)),
    btn ? h('button', { class: 'tb', onclick: (e) => { e.stopPropagation(); onBtn?.(e); } }, btn) : null);
}
async function like() {
  const t = cur(); if (!t) return;
  haptic('light');
  try {
    const r = await api('/api/like', { body: { track: t } });
    r.liked ? S.liked.add(t.id) : S.liked.delete(t.id);
    toast(r.liked ? 'Added to Liked Songs' : 'Removed from Liked Songs');
    refresh();
  } catch (e) { toast(e.message); }
}
function trackMenu() {
  const t = cur(); if (!t) return;
  const body = h('div', { class: 'list' },
    actionRow('list-plus', 'Add to a playlist', () => { s.close(); addToPlaylist([t]); }),
    actionRow('heart', S.liked.has(t.id) ? 'Remove from Liked Songs' : 'Add to Liked Songs', () => { s.close(); like(); }),
    actionRow('copy', 'Copy YouTube link', () => { s.close(); import('./core.js').then(c => c.copyText(`https://youtu.be/${t.id}`)); }),
    S.admin ? actionRow('close', 'Stop music', async () => { s.close(); if (await confirmBox('Stop the music and clear the queue?', 'Stop', true)) send({ t: 'music.stop' }); }, 'red') : null);
  const s = sheet({ title: t.title, body });
}

// ======================= audio / video + quality =======================
function toggleMode() {
  haptic('select');
  P.mode = P.mode === 'video' ? 'audio' : 'video';
  store.set('mode', P.mode);
  if (P.mode === 'video') loadVideo(); else { FV?.close(); unloadVideo(); }
  toast(P.mode === 'video' ? 'Video on' : 'Audio only — uses less data');
  renderStage(); refresh();
}
async function openQuality() {
  const t = cur();
  const body = h('div');
  const s = sheet({ title: 'Quality', body });
  const render = () => {
    body.replaceChildren(
      h('div', { class: 'grp-h' }, 'Mode'),
      h('div', { class: 'seg', style: { margin: '8px 0 16px' } },
        ...[['audio', 'Audio only'], ['video', 'Video']].map(([k, l]) => h('button', { class: P.mode === k ? 'on' : '', onclick: () => { if (P.mode !== k) toggleMode(); render(); } }, l))),
      h('div', { class: 'grp-h' }, 'Audio quality'),
      h('div', { class: 'seg', style: { margin: '8px 0 16px' } },
        ...[['low', 'Data saver'], ['medium', 'Standard'], ['high', 'High']].map(([k, l]) => h('button', { class: P.aq === k ? 'on' : '', onclick: () => setAq(k) }, l))),
      h('div', { class: 'grp-h' }, 'Video quality'),
      h('div', { class: 'list' },
        ...['auto', ...(P.heights.length ? P.heights : [144, 240, 360, 480, 720, 1080])].map(q =>
          h('button', { class: 'row', onclick: () => setVq(q) },
            h('span', { class: 'lab' }, q === 'auto' ? 'Auto' : `${q}p`, q === 'auto' ? h('small', null, `Adjusts to your connection (now ${P.autoH}p)`) : null),
            String(P.vq) === String(q) ? icon('check') : null))));
  };
  const setAq = (k) => { P.aq = k; store.set('aq', k); const c = cur(); if (c) { audio.src = audioSrc(c); audio.load(); } render(); };
  const setVq = (q) => { P.vq = q; store.set('vq', q); if (q === 'auto') pickAutoH(); delete video.dataset.src; if (P.mode === 'video') loadVideo(); render(); };
  render();
  if (t) { try { P.heights = (await api(`/api/formats/${t.id}`)).heights; render(); } catch {} }
}

// ======================= effects sheet =======================
// Everyone hears the same effect. Only admins, or whoever added the playing song, can change it.
const canFx = () => S.admin || (cur() && cur().byUid === S.uid);
function openFx() {
  unlockAudio();
  const body = h('div');
  let draft = { ...fx() }, tmr = null;
  // you hear your change at once (EQ / reverb / 8D); the server then applies it for everyone, speed included
  const push = () => { applyFx(draft); clearTimeout(tmr); tmr = setTimeout(() => send({ t: 'music.fx', fx: draft }), 80); };
  const slider = (key, label, min, max, step, fmt, allowed) => {
    const out = h('output', null, fmt(draft[key]));
    const r = h('input', { type: 'range', min, max, step, value: draft[key], 'aria-label': label, disabled: !allowed });
    const paint = () => r.style.setProperty('--v', ((r.value - min) / (max - min) * 100) + '%');
    r.oninput = () => {
      draft[key] = Number(r.value); draft.preset = 'custom'; out.textContent = fmt(draft[key]); paint(); push();
      body.querySelectorAll('.fx').forEach(b => b.classList.remove('on'));
    };
    paint();
    return h('div', { class: 'slider-row' + (allowed ? '' : ' ro') }, h('label', null, label), r, out);
  };
  const render = () => {
    draft = { ...fx() };
    const allowed = canFx();
    body.replaceChildren(
      allowed ? null : h('p', { class: 'muted', style: { fontSize: '13px', margin: '0 0 6px' } }, 'Only admins or the person who added this song can change the sound.'),
      h('div', { class: 'fx-grid' }, ...Object.entries(PRESETS).map(([k, p]) =>
        h('button', { class: 'fx' + (draft.preset === k ? ' on' : ''), disabled: !allowed, onclick: (e) => { draft = { ...p.v, preset: k }; applyFx(draft); send({ t: 'music.fx', fx: draft }); haptic('select');
          body.querySelectorAll('.fx').forEach(b => b.classList.toggle('on', b === e.currentTarget)); } }, h('span', null, p.e), p.label))),
      slider('bass', 'Bass', -12, 12, 1, v => (v > 0 ? '+' : '') + v + ' dB', allowed),
      slider('mid', 'Mids', -12, 12, 1, v => (v > 0 ? '+' : '') + v + ' dB', allowed),
      slider('treble', 'Treble', -12, 12, 1, v => (v > 0 ? '+' : '') + v + ' dB', allowed),
      slider('reverb', 'Reverb', 0, 0.8, 0.01, v => Math.round(v * 100) + '%', allowed),
      slider('rate', 'Speed', 0.7, 1.4, 0.05, v => Number(v).toFixed(2) + '×', allowed),
      h('p', { class: 'muted', style: { fontSize: '13px', marginTop: '12px' } }, 'Everyone in the voice chat hears this effect, in sync. 8D sounds best with headphones.'));
  };
  // redraw when someone else changes it (not on the echo of our own slider, which would break the drag)
  const onFx = () => {
    const f = fx(), same = ['bass', 'mid', 'treble', 'reverb', 'rate', 'mode'].every(k => String(f[k]) === String(draft[k]));
    if (!same || f.preset !== draft.preset) render();
  };
  on('music:fx', onFx);
  on('music:changed', onFxOwner);
  let owner = canFx();
  function onFxOwner() { if (canFx() !== owner) { owner = canFx(); render(); } }
  sheet({ title: 'Sound effects', body, onClose: () => { off('music:fx', onFx); off('music:changed', onFxOwner); } });
  render();
}

// ======================= search + library =======================
// ======================= Music page (Apple Music style: Home / Browse / Library / Search) =======================
const BR = { el: null, open: false, tab: 'home', view: null, back: null, added: new Set(), cache: {} };
const TABS = [['home', 'home', 'Home'], ['browse', 'sound', 'Browse'], ['library', 'library', 'Library'], ['search', 'search', 'Search']];

export function openSearch(tab = 'home') {
  loadLiked();
  const el = buildBrowse();
  BR.open = true;
  BR.tab = tab;
  BR.view = null;
  requestAnimationFrame(() => el.classList.add('show'));
  BR.back = pushBack(() => (BR.view ? (BR.view = null, renderBrowse()) : closeBrowse()));
  renderBrowse();
}
function closeBrowse() {
  if (!BR.open) return;
  BR.open = false;
  BR.el.classList.remove('show');
  BR.el.style.transform = '';
  BR.back?.(); BR.back = null;
}
function buildBrowse() {
  if (BR.el) return BR.el;
  const el = h('div', { class: 'br', role: 'dialog', 'aria-label': 'Music' },
    h('div', { class: 'br-top' }, h('div', { class: 'grab' }, h('i')),
      h('button', { class: 'br-close', 'aria-label': 'Close', onclick: closeBrowse }, icon('down'))),
    h('div', { class: 'br-scroll', id: 'br-scroll' }),
    h('nav', { class: 'br-tabs', id: 'br-tabs' }));
  document.body.append(el);
  dragToClose(el, [el.querySelector('.br-top')], closeBrowse);
  BR.el = el;
  return el;
}
function setTab(t) { BR.tab = t; BR.view = null; haptic('select'); renderBrowse(); }
function renderBrowse() {
  const tabs = BR.el.querySelector('#br-tabs');
  tabs.replaceChildren(...TABS.map(([id, ic, label]) => h('button', { class: BR.tab === id ? 'on' : '', onclick: () => setTab(id) }, icon(ic), h('span', null, label))));
  const sc = BR.el.querySelector('#br-scroll');
  sc.scrollTop = 0;
  sc.replaceChildren();
  if (BR.view) return renderCollection(sc, BR.view);
  ({ home: renderHome, browse: renderGenres, library: renderLibrary, search: renderSearchTab })[BR.tab](sc);
}

// ---- shared pieces ----
const bigTitle = (t, extra) => h('div', { class: 'br-h1' }, h('h1', null, t), extra || null);
function sectionHead(title, onAll) {
  return h('div', { class: 'br-sec' }, h('h2', null, title), onAll ? h('button', { onclick: onAll }, 'See all') : null);
}
function addTrack(t, el) {
  if (!canAdd()) return toast('Only admins can add songs right now');
  send({ t: 'music.add', track: t });
  BR.added.add(t.id);
  haptic('success');
  el?.classList.add('added');
}
function songMenu(t) {
  const m = sheet({ title: t.title, body: h('div', null,
    h('p', { class: 'muted', style: { margin: '-6px 0 10px' } }, t.artist),
    h('div', { class: 'list' },
      canAdd() ? actionRow('plus', 'Add to queue', () => { addTrack(t); m.close(); }) : null,
      S.admin ? actionRow('play', 'Play next', () => { send({ t: 'music.add', track: t, next: true }); m.close(); toast('Plays next'); }) : null,
      actionRow('list-plus', 'Add to a playlist', () => { m.close(); addToPlaylist([t]); }),
      actionRow('heart', S.liked.has(t.id) ? 'Remove from Liked Songs' : 'Add to Liked Songs', async () => {
        m.close();
        try {
          const r = await api('/api/like', { body: { track: t } });
          r.liked ? S.liked.add(t.id) : S.liked.delete(t.id);
          toast(r.liked ? 'Added to Liked Songs' : 'Removed from Liked Songs');
        } catch (e) { toast(e.message); }
      }))) });
}
function pressable(el, onTap, onHold) {
  let timer = null, held = false;
  el.addEventListener('pointerdown', () => { held = false; timer = setTimeout(() => { held = true; haptic('medium'); onHold(); }, 480); });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(e => el.addEventListener(e, () => clearTimeout(timer)));
  el.addEventListener('click', (e) => { if (held) { e.preventDefault(); return; } onTap(); });
  el.addEventListener('contextmenu', (e) => { e.preventDefault(); });
  return el;
}
function songCard(t) {
  const el = h('div', { class: 'cc' + (BR.added.has(t.id) ? ' added' : ''), role: 'button', tabindex: 0 },
    h('div', { class: 'cc-art', style: { backgroundImage: `url("${media(t.thumb || '/api/thumb/' + t.id)}")` } },
      h('span', { class: 'cc-add' }, icon('plus')), h('span', { class: 'cc-ok' }, icon('check'))),
    h('div', { class: 'cc-t' }, t.title), h('div', { class: 'cc-a' }, t.artist));
  return pressable(el, () => canAdd() ? addTrack(t, el) : songMenu(t), () => songMenu(t));
}
function skeletonRow() {
  return h('div', { class: 'carousel' }, ...Array.from({ length: 4 }, () => h('div', { class: 'cc sk' }, h('div', { class: 'cc-art' }), h('div', { class: 'cc-t' }, ' '), h('div', { class: 'cc-a' }, ' '))));
}
async function carouselSection(parent, title, loader, onAll) {
  const head = sectionHead(title, onAll);
  const row = skeletonRow();
  parent.append(head, row);
  try {
    const tracks = await loader();
    if (!tracks?.length) { head.remove(); row.remove(); return; }
    row.replaceChildren(...tracks.slice(0, 15).map(songCard));
  } catch (e) {
    row.replaceChildren(h('div', { class: 'muted', style: { padding: '8px 4px', fontSize: '14px' } }, 'Couldn’t load this right now.'));
  }
}
async function cachedApi(path, ttl = 10 * 60e3) {
  const c = BR.cache[path];
  if (c && Date.now() - c.t < ttl) return c.v;
  const v = await api(path);
  BR.cache[path] = { t: Date.now(), v };
  return v;
}
function genreTile(g, big = false) {
  return h('button', { class: 'gt' + (big ? ' big' : ''), style: { background: `linear-gradient(135deg, ${g.c[0]}, ${g.c[1]})` }, onclick: () => openCollection(g.id) },
    h('span', null, g.title));
}
function openCollection(id) { BR.view = id; haptic('light'); renderBrowse(); }

// ---- Home ----
async function renderHome(sc) {
  sc.append(bigTitle('Home'));
  let base = { genres: [], recent: [] };
  try { base = await cachedApi('/api/browse', 60e3); } catch {}
  if (!BR.open || BR.tab !== 'home' || BR.view) return;
  const wrap = h('div');
  sc.append(wrap);
  carouselSection(wrap, 'Made for you', async () => (await cachedApi('/api/browse/foryou')).tracks, () => openCollection('foryou'));
  if (base.recent.length) {
    wrap.append(sectionHead('Recently played'), h('div', { class: 'carousel' }, ...base.recent.map(songCard)));
  }
  const trending = h('div'), fresh = h('div');
  wrap.append(trending);
  carouselSection(trending, 'Trending now', async () => (await cachedApi('/api/browse/trending')).tracks, () => openCollection('trending'));
  wrap.append(sectionHead('Genres', () => setTab('browse')),
    h('div', { class: 'gt-grid' }, ...base.genres.slice(0, 6).map(g => genreTile(g))));
  wrap.append(fresh);
  carouselSection(fresh, 'New releases', async () => (await cachedApi('/api/browse/new')).tracks, () => openCollection('new'));
  wrap.append(h('div', { style: { height: '20px' } }));
}

// ---- Browse (all genres) ----
async function renderGenres(sc) {
  sc.append(bigTitle('Browse'));
  let base = { genres: [] };
  try { base = await cachedApi('/api/browse', 60e3); } catch (e) { sc.append(h('div', { class: 'empty-note' }, e.message)); return; }
  sc.append(
    h('div', { class: 'gt-grid two' },
      genreTile({ id: 'trending', title: 'Trending Now', c: ['#fa2d48', '#a8183a'] }, true),
      genreTile({ id: 'new', title: 'New Releases', c: ['#5e5ce6', '#bf5af2'] }, true)),
    sectionHead('Genres & moods'),
    h('div', { class: 'gt-grid' }, ...base.genres.map(g => genreTile(g))));
}

// ---- collection page (genre / trending / for you) ----
async function renderCollection(sc, id) {
  const hero = h('div', { class: 'col-hero' },
    h('button', { class: 'br-back', 'aria-label': 'Back', onclick: () => { BR.view = null; renderBrowse(); } }, icon('down')),
    h('h1', null, ' '), h('p', null, 'Loading…'));
  const list = h('div', { class: 'col-list' }, ...Array.from({ length: 6 }, () => h('div', { class: 'trk sk' }, h('div', { class: 'ta' }), h('div', { class: 'tm' }, h('div', { class: 'tt' }, ' '), h('div', { class: 'ts' }, ' ')))));
  sc.append(hero, list);
  let data;
  try { data = await cachedApi('/api/browse/' + id); } catch (e) { list.replaceChildren(h('div', { class: 'empty-note' }, e.message)); return; }
  if (BR.view !== id) return;
  const c = data.c || ['#fa2d48', '#a8183a'];
  hero.style.background = `linear-gradient(180deg, ${c[0]}, ${c[1]} 70%, transparent)`;
  hero.querySelector('h1').textContent = id === 'foryou' ? 'Made for you' : data.title;
  hero.querySelector('p').textContent = id === 'foryou' ? (data.fallback ? 'Listen to a few songs and this fills up with picks for you' : data.personal ? 'Based on what you listen to and like' : 'Based on what this room plays') : `${data.tracks.length} songs`;
  if (canAdd() && data.tracks.length) {
    hero.append(h('div', { class: 'col-btns' },
      h('button', { class: 'btn', onclick: () => { send({ t: 'music.addMany', tracks: data.tracks }); haptic('success'); } }, icon('plus'), 'Add all'),
      h('button', { class: 'btn soft', onclick: () => { const sh = [...data.tracks].sort(() => Math.random() - 0.5); send({ t: 'music.addMany', tracks: sh }); haptic('success'); } }, icon('shuffle'), 'Shuffle')));
  }
  list.replaceChildren(...data.tracks.map(t => {
    const row = trackRow(t, { btn: icon(BR.added.has(t.id) ? 'check-c' : 'plus-c'), onBtn: () => { addTrack(t, row); row.querySelector('.tb').replaceChildren(icon('check-c')); } });
    return pressable(row, () => songMenu(t), () => songMenu(t));
  }));
}

// ---- Library ----
async function renderLibrary(sc, openId = null) {
  sc.append(bigTitle('Library'));
  const box = h('div');
  sc.append(box);
  const showList = async () => {
    box.replaceChildren(h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } })));
    let pls = [];
    try { pls = (await api('/api/playlists')).playlists; } catch (e) { box.replaceChildren(h('div', { class: 'empty-note' }, e.message)); return; }
    box.replaceChildren(
      h('div', { class: 'list' },
        actionRow('history', 'Recently played in this room', showHistory, 'acc'),
        actionRow('plus', 'New playlist', async () => {
          const name = await prompt_('New playlist', 'Playlist name');
          if (name) { await api('/api/playlists', { body: { name } }); showList(); }
        }, 'acc')),
      h('div', { class: 'gt-grid two' }, ...pls.map(p => h('button', { class: 'pl-tile', onclick: () => showPlaylist(p.id) },
        h('div', { class: 'pl-art', style: { backgroundImage: p.thumb ? `url("${media(p.thumb)}")` : '' } }, p.thumb ? null : icon(p.name === 'Liked Songs' ? 'heart' : 'music')),
        h('div', { class: 'cc-t' }, p.name), h('div', { class: 'cc-a' }, `${p.count} songs`)))));
  };
  const backBtn = () => h('button', { class: 'br-back inline', 'aria-label': 'Back', onclick: showList }, icon('down'));
  const showHistory = async () => {
    const r = await api('/api/history');
    box.replaceChildren(h('div', { class: 'br-sub' }, backBtn(), h('h2', null, 'Recently played')),
      r.tracks.length ? h('div', null, ...r.tracks.map(t => { const row = trackRow(t, { sub: `${t.artist} · ${t.by_name || ''}`, btn: icon('plus-c'), onBtn: () => addTrack(t, row) }); return pressable(row, () => songMenu(t), () => songMenu(t)); }))
        : h('div', { class: 'empty-note' }, 'Songs played in this voice chat show up here.'));
  };
  const showPlaylist = async (id) => {
    const p = await api('/api/playlists/' + id);
    const list = h('div', null, ...p.tracks.map(t => {
      const row = trackRow(t, { btn: icon('more'), onBtn: () => {
        const m = sheet({ title: t.title, body: h('div', { class: 'list' },
          canAdd() ? actionRow('plus', 'Add to queue', () => { addTrack(t); m.close(); }) : null,
          actionRow('trash', 'Remove from playlist', async () => { await api('/api/playlists/' + id, { body: { remove: t.item } }); m.close(); row.remove(); if (p.name === 'Liked Songs') S.liked.delete(t.id); }, 'red')) });
      } });
      return pressable(row, () => canAdd() ? addTrack(t, row) : songMenu(t), () => songMenu(t));
    }));
    box.replaceChildren(
      h('div', { class: 'br-sub' }, backBtn(), h('h2', null, p.name)),
      h('div', { class: 'col-btns', style: { margin: '4px 0 14px' } },
        p.tracks.length && canAdd() ? h('button', { class: 'btn sm', onclick: () => { send({ t: 'music.addMany', tracks: p.tracks }); haptic('success'); } }, 'Add all to queue') : null,
        p.name !== 'Liked Songs' ? h('button', { class: 'btn sm danger', onclick: async () => { if (await confirmBox(`Delete “${p.name}”?`, 'Delete', true)) { await api('/api/playlists/' + id, { method: 'DELETE' }); showList(); } } }, 'Delete') : null),
      p.tracks.length ? list : h('div', { class: 'empty-note' }, p.name === 'Liked Songs' ? 'Tap the heart in the player to save songs here.' : 'Empty playlist. Hold any song and choose “Add to a playlist”.'));
  };
  if (openId) showPlaylist(openId); else showList();
}

// ---- Search ----
function renderSearchTab(sc) {
  sc.append(bigTitle('Search'));
  const input = h('input', { type: 'search', placeholder: 'Artists, songs or a YouTube link', enterkeyhint: 'search', 'aria-label': 'Search' });
  const bar = h('div', { class: 'searchbar br-search' }, icon('search'), input);
  const results = h('div');
  sc.append(bar, results);
  let timer = null, seq = 0;
  const empty = async () => {
    results.replaceChildren(sectionHead('Browse categories'));
    try { const b = await cachedApi('/api/browse', 60e3); results.append(h('div', { class: 'gt-grid' }, ...b.genres.map(g => genreTile(g)))); } catch {}
  };
  async function go() {
    const q = input.value.trim();
    if (!q) return empty();
    const my = ++seq;
    results.replaceChildren(h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } })));
    try {
      const r = await api('/api/search?q=' + encodeURIComponent(q));
      if (my !== seq) return;
      if (r.playlist) {
        const pl = r.playlist;
        results.replaceChildren(
          h('div', { class: 'pl-card' },
            h('div', { class: 'ta', style: { backgroundImage: pl.tracks[0] ? `url("${media(pl.tracks[0].thumb)}")` : '' } }),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { style: { fontWeight: 700, fontSize: '17px' } }, pl.title),
              h('div', { class: 'muted', style: { fontSize: '13px', margin: '2px 0 10px' } }, `${pl.tracks.length} songs`),
              h('div', { style: { display: 'flex', gap: '8px' } },
                canAdd() ? h('button', { class: 'btn sm', onclick: () => { send({ t: 'music.addMany', tracks: pl.tracks }); haptic('success'); } }, 'Add all to queue') : null,
                h('button', { class: 'btn sm soft', onclick: async () => { await api('/api/playlists', { body: { name: pl.title, tracks: pl.tracks } }); toast('Saved to your Library'); } }, 'Save')))),
          ...pl.tracks.map(t => { const row = trackRow(t, { btn: icon('plus-c'), onBtn: () => addTrack(t, row) }); return pressable(row, () => songMenu(t), () => songMenu(t)); }));
      } else if (!r.tracks.length) {
        results.replaceChildren(h('div', { class: 'empty-note' }, `No results for “${q}”. Try other words or paste a link.`));
      } else {
        const [top, ...rest] = r.tracks;
        const topCard = h('div', { class: 'top-res' },
          h('div', { class: 'ta', style: { backgroundImage: `url("${media(top.thumb)}")` } }),
          h('div', { style: { flex: 1, minWidth: 0 } }, h('div', { class: 'tt' }, top.title), h('div', { class: 'ts' }, `Song · ${top.artist}`)),
          canAdd() ? h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); addTrack(top); } }, icon('plus'), 'Add') : null);
        pressable(topCard, () => songMenu(top), () => songMenu(top));
        results.replaceChildren(sectionHead('Top result'), topCard, sectionHead('Songs'),
          ...rest.map(t => { const row = trackRow(t, { btn: icon(BR.added.has(t.id) ? 'check-c' : 'plus-c'), onBtn: () => { addTrack(t, row); row.querySelector('.tb').replaceChildren(icon('check-c')); } }); return pressable(row, () => songMenu(t), () => songMenu(t)); }));
      }
    } catch (e) { if (my === seq) results.replaceChildren(h('div', { class: 'empty-note' }, e.message)); }
  }
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(go, 800); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(timer); go(); input.blur(); } });
  empty();
  setTimeout(() => input.focus(), 450);
}

async function addToPlaylist(tracks) {
  let pls;
  try { pls = (await api('/api/playlists')).playlists; } catch (e) { return toast(e.message); }
  const body = h('div', { class: 'list' },
    actionRow('plus', 'New playlist', async () => {
      s.close();
      const name = await prompt_('New playlist', 'Playlist name');
      if (name) { try { await api('/api/playlists', { body: { name, tracks } }); toast(`Added to ${name}`); } catch (e) { toast(e.message); } }
    }, 'acc'),
    ...pls.map(p => actionRow(p.name === 'Liked Songs' ? 'heart' : 'music', p.name, async () => {
      s.close();
      try { await api('/api/playlists/' + p.id, { body: { add: tracks } }); } catch (e) { return toast(e.message); }
      toast(`Added to ${p.name}`);
      if (p.name === 'Liked Songs') tracks.forEach(t => S.liked.add(t.id));
    }, '', `${p.count} songs`)));
  const s = sheet({ title: 'Add to playlist', body });
}
async function saveQueue() {
  const tracks = [cur(), ...S.music.queue].filter(Boolean);
  const name = await prompt_('Save queue', 'Playlist name', 'Queue ' + new Date().toLocaleDateString());
  if (name) { await api('/api/playlists', { body: { name, tracks } }); toast(`Saved ${tracks.length} songs to ${name}`); }
}
export function prompt_(title, placeholder, value = '') {
  return new Promise((res) => {
    const input = h('input', { class: 'field', placeholder, value, maxlength: 60 });
    const body = h('div', { style: { display: 'grid', gap: '12px' } }, input,
      h('button', { class: 'btn block', onclick: () => { res(input.value.trim()); done = true; s.close(); } }, 'Save'));
    let done = false;
    const s = sheet({ title, body, onClose: () => { if (!done) res(''); } });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { res(input.value.trim()); done = true; s.close(); } });
    setTimeout(() => input.focus(), 350);
  });
}

export function stopMusic() { audio.pause(); unloadVideo(); }
