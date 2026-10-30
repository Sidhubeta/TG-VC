// Music: synced playback engine, Apple Music style UI, lyrics, queue, search, playlists, effects.
import { $, S, actionRow, api, can, report, confirmBox, deny, dragToClose, emit, fmtTime, fullView, h, haptic, icon, media, off, on, pushBack, send, serverNow, sheet, store, tg, toast } from './core.js';
import { getCtx } from './sounds.js';

// ======================= engine =======================
// The song plays from a plain <audio> element. It only goes through Web Audio (our effects graph) while an
// effect is on: phones starve Web Audio when Telegram is in the background, which made the music crackle.
let audio = makeAudio();
function makeAudio() {
  const a = new Audio();
  a.preload = 'auto';
  a.crossOrigin = 'anonymous';
  // (a spare element waiting to be used must not steer the music)
  a.addEventListener('loadedmetadata', () => a === audio && !isSilence(a) && sync(true));
  a.addEventListener('ended', () => a === audio && !isSilence(a) && onEnded());
  a.addEventListener('error', () => a === audio && !isSilence(a) && onAudioError());
  // Minimized Telegram freezes the page's timers, but the playing song still sends time updates: use them to
  // keep the music on track in the background.
  a.addEventListener('timeupdate', () => {
    if (a !== audio) return;
    if (fade) stepFade();
    if (document.hidden && Date.now() - lastSync > 900) sync();
  });
  return a;
}
let lastSync = 0;
// play(), and treat only a real "needs a tap" refusal as blocked sound. A play() cut short because the song
// changed or reloaded (AbortError) used to light up the "sound off" button on new songs for no reason.
function tryPlay() {
  return audio.play().then(() => setBlocked(false), (e) => {
    if (e?.name === 'NotAllowedError') { setBlocked(true); report('music-blocked', `unlocked=${!!audio.__ok}`); }
  });
}
// The phone switched its audio between media and call mode (mic on/off, joining voice, new speaker). iPhone and
// many Androids then keep the song "playing" with no sound until it's paused and played again — exactly what
// people had to do by hand. So the player does that itself, quickly, once the switch has settled.
let kickTimer = null;
function kickAudio(why) {
  clearTimeout(kickTimer);
  kickTimer = setTimeout(() => {
    if (audio.paused || !playing() || P.localPaused || !audio.getAttribute('src') || isSilence(audio)) return;
    const t = audio.currentTime;
    audio.pause();
    setTimeout(() => {
      if (!playing() || P.localPaused || !audio.paused) return;
      try { audio.currentTime = t + 0.08; } catch {}
      tryPlay();
      if (G && G.c.state !== 'running') G.c.resume().catch(() => {});
    }, 60);
    console.info('music: audio route changed (' + why + '), restarted the sound');
  }, 500);
}
on('voice:session', kickAudio);
const video = h('video', { class: 'vid', playsinline: true, muted: true, preload: 'auto' });
video.muted = true;
const isApple = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent) && 'ontouchend' in document || /iPhone|iPad|iPod/.test(navigator.userAgent);
const CODEC = !isApple && audio.canPlayType('audio/webm; codecs="opus"') ? 'opus' : 'aac';
const HEIGHTS = [144, 240, 360, 480, 720, 1080];

const P = {
  aq: store.get('aq', 'medium'),             // low | medium | high
  // video quality: 480p unless the person picks another one (it never changes by itself)
  vq: HEIGHTS.includes(Number(store.get('vq', 480))) ? Number(store.get('vq', 480)) : 480,
  vol: store.get('vol', 1),
  localPaused: false, loadedQid: null, blocked: false, heights: [],
  optOut: null,   // qid of a video song this person switched back to audio
  localVid: null, // qid this person watches as video (without the right to turn it on for everyone)
};
audio.volume = P.vol;
// Effects are shared: the server holds them and everyone hears the same thing, in sync.
const FX_NONE = { preset: 'normal', bass: 0, mid: 0, treble: 0, reverb: 0, rate: 1, mode: 'none' };
const fx = () => S.music?.fx || FX_NONE;
const rate = () => S.music?.rate || 1;
let G = null; // WebAudio graph

const cur = () => S.music?.current || null;
const playing = () => S.music?.status === 'playing';
const loadingSong = () => S.music?.status === 'loading'; // the server is still finding the stream: the song starts at 0 once it's ready
// video is on when someone turned it on for this song (and you didn't switch back), or you turned it on for you
export const isVideo = () => { const c = cur(); return !!c && ((c.video && P.optOut !== c.qid) || P.localVid === c.qid); };
export function expectedPos() {
  const m = S.music; if (!m?.current) return 0;
  const p = m.status === 'playing' ? m.pos + (serverNow() - m.upd) * (m.rate || 1) : m.pos;
  const d = m.current.duration || Infinity;
  return Math.max(0, Math.min(p, d));
}
// Songs start where their sound starts and end where it ends (the server skips silent intros and tails).
const startOf = (t) => t.start || 0;
const endOf = (t) => t.end || t.duration;
// Where the room moves on from the current song: a few seconds before its end with crossfade, else just after.
const switchAt = (c) => endOf(c) + (c.xf ? -c.xf : 0.8);
// If the server's news hasn't arrived (phones pause the app's connection in the background), play the next songs
// from the queue ourselves instead of sitting on the old one. Also used the moment our own copy of a song ends
// (P.endedQid), so the next one starts without a gap.
function predicted() {
  const m = S.music, c = m?.current;
  if (!c || m.status !== 'playing' || !c.duration) return null;
  const r = m.rate || 1;
  const into = (m.pos + (serverNow() - m.upd) * r - switchAt(c)) / r; // seconds since the room should have moved on
  if (into < 0.7 && !(P.endedQid === c.qid && into > -3.8)) return null;
  let pos = Math.max(0, into);
  if (m.loop === 'one') return { track: c, pos: startOf(c) + pos % (endOf(c) - startOf(c) + 0.8) };
  for (const t of m.queue) {
    const len = endOf(t) - startOf(t);
    const done = t.duration && (pos >= len + 0.8 || (P.endedQid === t.qid && pos > len - 3));
    if (!done) return { track: t, pos: startOf(t) + pos };
    pos = Math.max(0, pos - len - 0.8);
  }
  return m.loop === 'all' ? { track: m.queue[0] || c, pos: startOf(m.queue[0] || c) } : { ended: true };
}

function audioSrc(t) { return media(`/api/stream/${t.id}/audio?c=${CODEC}&q=${P.aq}`); }

function load(t) {
  // the song reached its end and the next one starts: blend them (crossfade) instead of a hard switch
  const old = P.loadedTrack;
  if (t && old && old.qid !== t.qid && crossfadeOn() && canFade() && !audio.paused && !document.hidden) {
    const left = (endOf(old) - audio.currentTime) / (audio.playbackRate || 1);
    if (left > 0.5 && left < (S.music?.xfade || 4) + 1.5) startFade(Math.min(left, S.music?.xfade || 4));
  }
  P.loadedQid = t ? t.qid : null;
  P.loadedTrack = t || null;
  P.localPaused = false;
  if (!t) { audio.removeAttribute('src'); audio.load(); unloadVideo(); return; }
  audio.src = audioSrc(t);
  audio.load();
  if (isVideo()) loadVideo(t);
  else unloadVideo();
}
function loadVideo(t) {
  t = t || P.loadedTrack || cur(); if (!t) return;
  const src = media(`/api/stream/${t.id}/video?q=${P.vq}`);
  if (video.dataset.src === src) return;
  video.dataset.src = src;
  video.poster = media(t.thumb); // show the artwork while the video loads instead of a black box
  video.src = src;
  video.load();
  setVideoLoading(true);
}
function setVideoLoading(on) { video.parentNode?.classList.toggle('loading', on); }
video.addEventListener('waiting', () => setVideoLoading(true));
video.addEventListener('playing', () => setVideoLoading(false));
video.addEventListener('canplay', () => setVideoLoading(false));
function unloadVideo() { if (video.dataset.src) { video.removeAttribute('src'); delete video.dataset.src; video.load(); } }

// Start the next song right here, inside the "ended" event. When Telegram is minimized its timers are frozen,
// so waiting for the next tick left the old song stuck (or looping) until the app was opened again.
function onEnded() {
  const c = cur(); if (!c) return;
  if (!c.duration) send({ t: 'music.ended', qid: c.qid });
  P.endedQid = P.loadedQid;
  sync();
}
// retry a failed stream a few times (with backoff), not forever every 3s
let errQid = null, errCount = 0;
function onAudioError() {
  // retry the song that's loaded: after moving on by ourselves (no news from the server) that's not cur()
  const t = P.loadedTrack; if (!t || !audio.getAttribute('src')) return;
  console.warn('audio error', audio.error);
  report('music-error', `code=${audio.error?.code} ${audio.error?.message || ''} song=${t.id} try=${errCount + 1}`);
  if (errQid !== t.qid) { errQid = t.qid; errCount = 0; }
  if (++errCount > 4) { if (errCount === 5) toast('This song can’t be played right now'); return; }
  setTimeout(() => { if (P.loadedQid === t.qid && audio.error) { audio.src = audioSrc(t); audio.load(); } }, 2000 * errCount);
}
video.addEventListener('loadedmetadata', () => { seekVideo(); if (!audio.paused) video.play().catch(() => {}); });
// A video that fails to load (network blip, expired link) is retried by itself; before, it stayed blank until
// you switched to audio and back.
let vErr = 0;
video.addEventListener('error', () => {
  if (!video.dataset.src || ++vErr > 4) return;
  const t = P.loadedTrack;
  setTimeout(() => { if (t && P.loadedQid === t.qid && isVideo() && video.error) { delete video.dataset.src; loadVideo(t); } }, 1500 * vErr);
});
video.addEventListener('playing', () => { vErr = 0; });

let lastVideoSeek = 0, seekCost = 0.25, seekAt = 0;
function seekVideo() {
  // Aim ahead by as long as the last seek took: after a seek the video needs a moment before it moves again,
  // so aiming at "now" left it behind the sound every time.
  try {
    video.currentTime = Math.min(audio.currentTime + seekCost * (audio.playbackRate || 1), video.duration || Infinity);
    lastVideoSeek = seekAt = Date.now();
  } catch {}
}
video.addEventListener('seeked', () => { if (seekAt) { seekCost = Math.min(1.5, Math.max(0.1, (Date.now() - seekAt) / 1000 + 0.05)); seekAt = 0; } });
// The time of the frame really on screen (Chrome / Safari 15.4+). video.currentTime runs ahead of what's shown
// when the phone is slow to decode, which hid the "picture behind the sound" gap from the old check.
let frameTime = null;
function watchFrames() {
  if (!video.requestVideoFrameCallback) return;
  video.requestVideoFrameCallback((now, meta) => { frameTime = { media: meta.mediaTime, at: now }; watchFrames(); });
}
watchFrames();
function shownVideoTime() {
  if (frameTime && !video.paused && performance.now() - frameTime.at < 400) return frameTime.media + (performance.now() - frameTime.at) / 1000 * video.playbackRate;
  return video.currentTime;
}

function sync(force = false) {
  lastSync = Date.now();
  let pr = predicted();
  // The room's clock says the song is over (our copy may be stopped a moment before its own "ended" fires):
  // count it as ended and go straight on to the next song, rather than pausing and waiting.
  if (!pr) {
    const c = cur();
    if (c?.duration && playing() && !P.localPaused && P.loadedQid === c.qid && expectedPos() >= endOf(c) - 0.3) { P.endedQid = c.qid; pr = predicted(); }
  }
  if (pr?.ended) { if (!audio.paused) audio.pause(); return; } // song over, nothing queued: wait, don't replay it
  const t = pr ? pr.track : cur();
  if (!t) { if (!audio.paused) audio.pause(); return; }
  if (t.qid !== P.loadedQid) load(t);
  const want = playing() && !P.localPaused;
  const exp = pr ? pr.pos : expectedPos();
  const r = pr ? 1 : rate();
  // Slowed / Nightcore change the pitch on purpose; otherwise keep the pitch when we fine-tune the speed
  const keepPitch = r === 1;
  if (audio.preservesPitch !== keepPitch) audio.preservesPitch = audio.mozPreservesPitch = audio.webkitPreservesPitch = keepPitch;
  let speed = r;
  if (audio.readyState >= 1 && !P.localPaused) {
    const drift = audio.currentTime - exp;
    // Big gaps: jump. In the background timers run late, so only big jumps are corrected there.
    const limit = document.hidden ? 4 : 1.2;
    if ((force && Math.abs(drift) > 0.35 && !document.hidden) || Math.abs(drift) > limit || (!want && Math.abs(drift) > 0.5)) {
      try { audio.currentTime = exp; } catch {}
      fine = 0;
    } else if (want && keepPitch && !document.hidden) {
      // Small gaps (everyone a bit apart): play 2% faster/slower until we're within 0.04 s. With the pitch kept,
      // 2% can't be heard, unlike the jumps. (It used to start only at 0.25 s, so a phone could stay a fifth of
      // a second off for the whole song: an echo between phones.)
      if (Math.abs(drift) > 0.12) fine = drift > 0 ? -1 : 1;
      else if (Math.abs(drift) < 0.04) fine = 0;
      speed = r * (1 + 0.02 * fine);
    }
  }
  if (Math.abs(audio.playbackRate - speed) > 0.001) audio.playbackRate = speed;
  watchdog(want, exp);
  // A finished <audio> restarts from 0 when played again. That was the "first 1-2 seconds on repeat" in the
  // background: never replay a song whose time is up.
  const over = !pr && t.duration && exp >= t.duration - 0.3;
  if (want && audio.paused && !over) tryPlay();
  else if ((!want || over) && !audio.paused) audio.pause();
  // With an effect on, the song goes through Web Audio; if the phone suspended it, the song "plays" in silence.
  if (want && G && G.c.state !== 'running') { setBlocked(true); G.c.resume().then(() => { if (G?.c.state === 'running') setBlocked(false); }, () => {}); }
  syncVideo();
}
let fine = 0; // -1 slower, 0 normal, +1 faster (small catch-up on the audio)
setInterval(sync, 700);

// The video (muted) is kept on the audio's clock 4x a second. Small gaps are closed by playing the video a little
// faster or slower (it's silent, so nobody hears it); only big gaps seek. Before, anything under 1 s was left,
// so the picture ran visibly behind the sound.
let vStuck = { t: -1, since: 0 };
function syncVideo() {
  if (document.hidden) { if (!video.paused) video.pause(); return; } // nobody sees it: save the phone's power for the sound
  if (!video.dataset.src || video.readyState < 1) return;
  const r = audio.playbackRate || 1;
  if (!audio.paused && video.paused && video.isConnected) video.play().catch(() => {});
  if (audio.paused && !video.paused) video.pause();
  const vd = shownVideoTime() - audio.currentTime;
  let vr = r;
  if (Math.abs(vd) > 0.8) {
    if (!video.seeking && video.readyState >= 2 && Date.now() - lastVideoSeek > 2000) seekVideo();
  } else if (Math.abs(vd) > 0.05) {
    // catch up in about 2 s: the further apart, the bigger the nudge (it's muted, so nobody hears it)
    vr = r * (1 - Math.max(-0.25, Math.min(0.25, vd / 2)));
  }
  if (Math.abs(video.playbackRate - vr) > 0.001) video.playbackRate = vr;
  // the picture froze while the sound plays (stalled download): reload it at the right spot
  if (!audio.paused && !video.paused && !video.seeking && video.isConnected) {
    const now = Date.now();
    if (Math.abs(video.currentTime - vStuck.t) > 0.05) vStuck = { t: video.currentTime, since: now };
    else if (now - vStuck.since > 6000) { vStuck.since = now; const t = P.loadedTrack; if (t) { delete video.dataset.src; loadVideo(t); } }
  } else vStuck.since = Date.now();
}
setInterval(() => { if (video.dataset.src && !document.hidden) syncVideo(); }, 250);

// "Playing" but silent: the stream stalled and the time stopped moving (pausing and playing again fixed it by
// hand). If it's stuck for 2.5 s, reload the stream at the right spot.
let stuck = { t: -1, since: 0 };
function watchdog(want, exp) {
  if (!want || audio.paused || audio.seeking || P.localPaused || !audio.getAttribute('src')) { stuck.since = 0; return; }
  const now = Date.now();
  if (Math.abs(audio.currentTime - stuck.t) > 0.05) { stuck = { t: audio.currentTime, since: now }; return; }
  if (!stuck.since) { stuck.since = now; return; }
  // has data but isn't moving: 2.5 s. Still downloading (slow internet): give it 10 s, or it would never start.
  if (now - stuck.since > (audio.readyState >= 3 ? 2500 : 10000)) {
    stuck.since = 0;
    console.warn('music stalled, reloading at', exp.toFixed(1));
    report('music-stall', `song=${P.loadedTrack?.id} at=${exp.toFixed(1)} ready=${audio.readyState} net=${audio.networkState}`);
    const src = audio.getAttribute('src');
    audio.src = src; audio.load(); // loadedmetadata -> sync(true) seeks to the room's position
    tryPlay();
  }
}
// coming back to the app: jump to where the room is now (not where it was when you left)
const back = () => { G?.c.resume().catch(() => {}); sync(true); emit('app:back'); };
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) return back();
  if (!G) getCtx()?.suspend().catch(() => {}); // the sound-effects context sleeps while hidden (music doesn't use it)
});
window.addEventListener('focus', back);
try { tg?.onEvent?.('activated', back); } catch {}

// If the browser blocks sound until the first tap, the next tap anywhere starts it (no prompt, as asked).
function setBlocked(b) {
  if (P.blocked === b) return;
  P.blocked = b;
  emit('music:blocked', b);
  renderCard(); // the card's play button turns into a "sound off" button while the phone holds the sound back
}
export const musicBlocked = () => P.blocked;
// Called on every tap (touchend / click count as a "user gesture" on phones; pointerdown doesn't).
export function unlockAudio() {
  getCtx();
  if (needGraph()) buildGraph();
  if (G && G.c.state !== 'running') G.c.resume().then(() => { if (G?.c.state === 'running' && !audio.paused) setBlocked(false); }, () => {});
  // (sync decides: it never replays a song that's already over, which a bare play() would restart from 0)
  if (cur() && playing() && !P.localPaused && audio.paused) sync();
  else if (P.blocked && !audio.paused && (!G || G.c.state === 'running')) setBlocked(false);
  unlockElement(audio);
  primeSpare(); // once: ready for when effects switch off later (that moves the song to the spare player)
}
// iPhone lets an <audio> element play by itself later only if a tap has started it once. The first tap usually
// comes before any song is playing (mic, chat…), and that left the music player locked: every song stayed silent
// until the "sound off" button was tapped. So the first tap now starts the player itself — a moment of silence,
// or the loaded song started and stopped at once — and it stays unlocked for every song after that.
function unlockElement(a) {
  if (a.__ok) return;
  if (!a.paused) { a.__ok = true; return; } // already playing: it's unlocked
  const song = a.getAttribute('src');
  if (!song) a.src = SILENCE;
  a.__ok = true; // the play() inside this tap is what counts, even if it's stopped right away
  const pr = a.play();
  if (song) a.pause();
  pr.then(() => {
    if (a.getAttribute('src') === SILENCE) { a.pause(); a.removeAttribute('src'); a.load(); }
  }, (e) => { if (e?.name === 'NotAllowedError') a.__ok = false; }); // not a real tap (a scroll): next one
}
const isSilence = (a) => a.getAttribute('src') === SILENCE;
// iPhone unlocks sound per <audio> element. Keep one spare element that a tap already played (a short bit of
// silence), so switching the effects off later can move the song to it without needing another tap.
let spare = null, priming = false;
const SILENCE = (() => {
  const n = 800, b = new Uint8Array(44 + n).fill(128), v = new DataView(b.buffer);
  const str = (o, x) => [...x].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, 'RIFF'); v.setUint32(4, 36 + n, true); str(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true);
  v.setUint16(34, 8, true); str(36, 'data'); v.setUint32(40, n, true);
  return URL.createObjectURL(new Blob([b], { type: 'audio/wav' }));
})();
function primeSpare() {
  if (spare || priming) return;
  priming = true;
  const a = makeAudio();
  a.src = SILENCE;
  a.play().then(() => { a.pause(); spare = a; }, () => {}).finally(() => { priming = false; });
}

// ======================= crossfade =======================
// On by default; anyone can switch it off for themselves (Queue → Crossfade). The room starts the next song a few
// seconds before the end; with crossfade the old song fades out on a second player while the new one fades in.
// iPhone can't change an <audio> element's volume, and with a sound effect on the song runs through Web Audio:
// there the songs simply switch.
export const crossfadeOn = () => store.get('xfade', true) !== false;
const canFade = () => !isApple && !G;
let fade = null; // { old, t0, len }
function startFade(secs) {
  if (fade) endFade();
  const old = audio;
  audio = spare || makeAudio(); spare = null; // the spare was unlocked by a tap already
  audio.removeAttribute('src');
  audio.volume = 0;
  const sink = store.get('dev:audiooutput', '');
  if (sink && audio.setSinkId) audio.setSinkId(sink).catch(() => {});
  fade = { old, t0: performance.now(), len: secs * 1000 };
}
function stepFade() {
  if (!fade) return;
  const k = Math.min(1, (performance.now() - fade.t0) / fade.len);
  if (k >= 1 || document.hidden) return endFade();
  fade.old.volume = P.vol * Math.cos(k * Math.PI / 2); // equal-power: no dip in loudness halfway
  audio.volume = P.vol * Math.sin(k * Math.PI / 2);
}
function endFade() {
  if (!fade) return;
  const old = fade.old; fade = null;
  audio.volume = P.vol;
  old.pause(); old.removeAttribute('src'); old.load(); old.volume = P.vol;
  if (!spare) spare = old; // already unlocked: ready for the next crossfade
}
setInterval(stepFade, 50);
document.addEventListener('visibilitychange', () => { if (document.hidden) endFade(); }); // background: just switch
function toggleCrossfade() {
  const on = !crossfadeOn();
  store.set('xfade', on); haptic('select');
  if (!on) endFade();
  toast(on ? (isApple ? 'Crossfade on — iPhone switches songs without blending' : 'Crossfade on — songs blend into each other') : 'Crossfade off');
  if (NP.view === 'queue') { NP.qsig = ''; renderStage(); }
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
let tapped = false;
const needGraph = () => {
  const f = fx();
  return tapped || f.mode !== 'none' || !!(f.bass || f.mid || f.treble || f.reverb) || (isApple && P.vol < 0.99); // iPhone ignores <audio>.volume
};
// effects switched off: move the song back to a plain <audio> element (keeps playing where it was)
function releaseGraph() {
  if (!G || needGraph()) return;
  const old = audio, src = old.getAttribute('src'), was = !old.paused;
  // a fresh element would need a new tap on iPhone: use the spare one that a tap already unlocked
  audio = spare || makeAudio(); spare = null;
  audio.removeAttribute('src');
  audio.volume = P.vol;
  audio.playbackRate = rate();
  if (src) { audio.src = src; audio.load(); if (was) tryPlay(); }
  old.pause(); old.removeAttribute('src'); old.load();
  try { G.master.disconnect(); } catch {}
  G = null;
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
  if (fade) return stepFade();
  if (!G && isApple && v < 0.99) buildGraph();
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
on('voice:sink', (id) => { setMusicSink(id); kickAudio('speaker'); });
// the recorder needs the music as Web Audio
export function musicTap() { tapped = true; buildGraph(); return G ? { ctx: G.c, node: G.master } : null; }

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
// The card is rebuilt only when something on it changes. Rebuilding it on every music update (a vote, someone
// adding a song…) swapped the buttons out from under your finger, so taps were sometimes lost, and it moved
// the video around, which made it stutter.
let cardSig = '', cardQid;
export function renderCard() {
  const el = card(); if (!el) return;
  const t = cur();
  const vidHere = !!t && isVideo() && !NP.open && !FV;
  const sig = t ? [t.qid, t.title, t.by, playing() && !P.localPaused, loadingSong(), P.blocked, vidHere, can('add')].join('|') : 'empty|' + can('add');
  // (full screen and the player borrow the video element: rebuild if it should be here but isn't)
  if (sig === cardSig && el.firstChild && (!vidHere || el.contains(video))) return;
  cardSig = sig;
  // a new song slides in (only when the song really changes, not on play/pause)
  if (t && t.qid !== cardQid && cardQid !== undefined) { el.classList.remove('swap'); void el.offsetWidth; el.classList.add('swap'); }
  cardQid = t?.qid ?? null;
  el.replaceChildren();
  el.classList.toggle('empty', !t);
  document.body.classList.toggle('playing', !!t && playing() && !P.localPaused);
  if (!t) {
    el.onclick = () => openSearch('home');
    el.append(h('div', { class: 'art' }, icon('music')),
      h('div', { class: 'meta' }, h('div', { class: 'ttl' }, 'Nothing playing'), h('div', { class: 'art-sub' }, canAdd() ? 'Tap to search songs or paste a YouTube / Spotify link' : 'Admins will play music here')),
      canAdd() ? h('div', { class: 'ctl' }, h('button', { 'aria-label': 'Search', onclick: (e) => { e.stopPropagation(); openSearch(); } }, icon('plus'))) : null);
    return;
  }
  el.onclick = () => openNP();
  const isPlay = playing() && !P.localPaused;
  const showVid = isVideo() && !NP.open && !FV;
  el.classList.toggle('has-video', showVid);
  el.append(
    showVid ? videoBox('card-vid') : null,
    h('div', { class: 'art', style: { backgroundImage: `url("${media(t.thumb)}")` } }),
    h('div', { class: 'meta' },
      h('div', { class: 'ttl' }, h('span', { class: 'eq' }, h('i'), h('i'), h('i')), t.title),
      h('div', { class: 'art-sub' }, t.artist + (t.by ? ` · added by ${t.by}` : ''))),
    h('div', { class: 'ctl' },
      showVid ? null : h('button', { class: 'vsw', 'aria-label': 'Watch the video', onclick: (e) => { e.stopPropagation(); setVideo(true); } }, icon('video')),
      loadingSong()
        ? h('button', { class: 'loading-btn', 'aria-label': 'Loading the song', onclick: (e) => { e.stopPropagation(); togglePlay(); } }, h('span', { class: 'spin' }))
        : isPlay && P.blocked
        // the phone stopped the sound (call, lock screen, no tap yet): one tap here brings it back
        ? h('button', { class: 'muted-btn', 'aria-label': 'Sound is off — tap to turn it on', onclick: (e) => { e.stopPropagation(); unlockAudio(); } }, icon('vol-x'))
        : h('button', { 'aria-label': isPlay ? 'Pause' : 'Play', onclick: (e) => { e.stopPropagation(); togglePlay(); } }, icon(isPlay ? 'pause' : 'play', 'fill')),
      h('button', { 'aria-label': 'Next', onclick: (e) => { e.stopPropagation(); next(); } }, icon('next', 'fill'))),
    h('div', { class: 'bar', id: 'np-bar' }));
}
function canAdd() { return can('add'); }

// ---- music video: one <video> element that lives in the player, in the room card, or full screen ----
let FV = null;
const vbtn = (ic, label, fn, cls = '') => h('button', { class: 'round vbtn ' + cls, 'aria-label': label, onclick: (e) => { e.stopPropagation(); fn(); } }, icon(ic));
function videoBox(cls) {
  const inPlayer = cls === 'np-vid';
  const box = h('div', { class: cls + (video.readyState < 3 ? ' loading' : ''), onclick: (e) => e.stopPropagation() }, video, h('span', { class: 'vspin' }),
    h('div', { class: 'vbtns' },
      inPlayer ? null : vbtn('headphones', 'Switch to audio only', () => setVideo(false)),
      pipSupported(video) ? vbtn('pip', 'Picture in picture', () => pip(video)) : null,
      vbtn('expand', 'Full screen', () => openVideoFull(inPlayer ? true : null))));
  if (isVideo()) loadVideo();
  return box;
}
// Video on: for everyone (if allowed), otherwise just for you. Video off: just for you, for this song.
export function setVideo(on) {
  const c = cur(); if (!c) return;
  haptic('select');
  if (on) {
    P.optOut = null;
    P.localVid = c.qid;
    if (!c.video && can('video')) { send({ t: 'music.video', on: true }); toast('Video on for everyone'); }
    else if (!c.video) toast('Video on for you');
  } else {
    P.optOut = c.qid; P.localVid = null;
    FV?.close();
    toast('Audio only — uses less data');
  }
  applyVideo();
}
let lastVid = false;
function applyVideo() {
  const v = isVideo();
  if (v) loadVideo(); else unloadVideo();
  if (v !== lastVid || !v) { lastVid = v; renderCard(); if (NP.open) renderStage(); refresh(); }
}
function openVideoFull(rotate) {
  if (FV) return;
  haptic('light');
  FV = fullView(video, { label: cur()?.title || '', rotate, onClose: () => { FV = null; renderCard(); if (NP.open) renderStage(); } });
}

// ======================= controls =======================
function togglePlay() {
  haptic('light');
  unlockAudio();
  const m = S.music; if (!m) return;
  if (can('playback')) {
    if (P.localPaused) { P.localPaused = false; sync(true); refresh(); return; }
    send({ t: m.status === 'playing' || m.status === 'loading' ? 'music.pause' : 'music.play' });
  } else {
    // without playback rights you pause only for yourself; resuming jumps back to live
    P.localPaused = !P.localPaused;
    sync(true); refresh();
    if (P.localPaused) toast('Paused for you only');
  }
}
function next() {
  haptic('medium');
  const t = cur(); if (!t) return;
  if ((t.byUid === S.uid && can('skip_own')) || can('playback')) send({ t: 'music.skip' });
  else if (can('vote')) { send({ t: 'music.voteskip' }); toast('Voted to skip'); }
  else deny('vote');
}
function prev() { haptic('light'); if (can('playback')) send({ t: 'music.prev' }); else deny('playback'); }

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
  if (isVideo()) loadVideo();
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
  // video in the player always spreads edge to edge and blends into the colours behind it
  NP.el.classList.toggle('vfill', isVideo() && NP.view === 'art');
  if (isVideo() && !FV) st.append(videoBox('np-vid'));
  else st.append(h('div', { class: 'big-art', style: { backgroundImage: `url("${media(t.thumb)}")` } }));
}

// ---- scrubber (admins seek for everyone) ----
function setupScrub(sc) {
  let drag = false, ratio = 0;
  const at = (e) => { const r = sc.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
  sc.addEventListener('pointerdown', (e) => { if (!cur()?.duration) return; if (!can('playback')) return deny('playback'); drag = true; NP.scrubbing = true; sc.classList.add('drag'); sc.setPointerCapture(e.pointerId); ratio = at(e); paintScrub(ratio); });
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
  const pp = el.querySelector('#np-pp');
  if (pp.getAttribute('aria-label') !== (isPlay ? 'Pause' : 'Play')) { pp.replaceChildren(icon(isPlay ? 'pause' : 'play', 'fill')); pp.setAttribute('aria-label', isPlay ? 'Pause' : 'Play'); }
  el.querySelector('#np-prev').classList.toggle('locked', !can('playback'));
  const v = S.music;
  el.querySelector('#np-votes').textContent = !can('playback') && t.byUid !== S.uid && v.votes ? `${v.votes}/${v.need}` : '';
  el.querySelector('#np-scrub').classList.toggle('ro', !can('playback'));
  el.querySelector('#np-like').classList.toggle('on', S.liked.has(t.id));
  const lk = el.querySelector('#np-like'), liked = S.liked.has(t.id) ? '1' : '0';
  if (lk.dataset.on !== liked) { lk.dataset.on = liked; lk.replaceChildren(icon('heart', liked === '1' ? 'fill' : '')); }
  el.querySelector('#tab-vid').classList.toggle('on', isVideo());
  el.querySelector('#tab-fx').classList.toggle('on', fx().preset !== 'normal');
  if (NP.view === 'queue' && queueSig() !== NP.qsig) renderStage();
}
const queueSig = () => { const m = S.music; return [m.loop, m.autoplay, can('queue'), can('modes'), can('add'), ...m.queue.map(q => q.qid + q.title + q.by)].join('|'); };

// ======================= server updates =======================
let lastQid = null, lastTimeline = '', lastFx = '';
on('ws:music', (m) => onMusic(m.m));
on('ws:init', (m) => onMusic(m.music));
function onMusic(m) {
  S.music = m;
  const t = m.current;
  if (t && t.qid === P.loadedQid) P.loadedTrack = t; // keep what we know about the playing song fresh (start/end arrive later)
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
  if (isVideo() !== lastVid) applyVideo(); // someone turned the video on for everyone (or a new song started)
  const fxKey = JSON.stringify(m.fx || FX_NONE);
  if (fxKey !== lastFx) {
    lastFx = fxKey;
    if (needGraph()) buildGraph(); else releaseGraph();
    applyFx(); emit('music:fx');
  }
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
    NP.lines.forEach((ln, i) => box.append(h('p', { 'data-i': i, onclick: () => { if (can('playback')) send({ t: 'music.seek', pos: ln.t }); } }, ln.text)));
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
  NP.qsig = queueSig();
  const box = h('div', { class: 'qv' });
  const loopIcon = m.loop === 'one' ? 'repeat1' : 'repeat';
  box.append(
    h('div', { class: 'qbtns' },
      h('button', { class: can('queue') ? '' : 'locked', onclick: () => can('queue') ? send({ t: 'music.shuffle' }) : deny('queue'), 'aria-label': 'Shuffle' }, icon('shuffle'), 'Shuffle'),
      h('button', { class: (m.loop !== 'off' ? 'on' : '') + (can('modes') ? '' : ' locked'), onclick: () => can('modes') ? send({ t: 'music.loop', mode: { off: 'all', all: 'one', one: 'off' }[m.loop] }) : deny('modes') }, icon(loopIcon), m.loop === 'one' ? 'Repeat 1' : 'Repeat'),
      h('button', { class: (m.autoplay ? 'on' : '') + (can('modes') ? '' : ' locked'), onclick: () => can('modes') ? send({ t: 'music.autoplay', on: !m.autoplay }) : deny('modes') }, icon('infinity'), 'Autoplay'),
      // just for you: blend the end of a song into the next
      h('button', { class: crossfadeOn() ? 'on' : '', 'aria-label': 'Crossfade', onclick: toggleCrossfade }, icon('xfade'), 'Crossfade')),
    h('div', { class: 'qh' }, h('span', null, 'Playing next'),
      h('span', { style: { display: 'flex', gap: '6px' } },
        m.queue.length ? h('button', { class: 'round', 'aria-label': 'Save queue as playlist', onclick: saveQueue }, icon('list-plus')) : null,
        m.queue.length ? h('button', { class: 'round' + (can('queue') ? '' : ' locked'), 'aria-label': 'Clear queue', onclick: async () => { if (!can('queue')) return deny('queue'); if (await confirmBox('Clear the whole queue?', 'Clear', true)) send({ t: 'music.clear' }); } }, icon('trash')) : null,
        h('button', { class: 'round', 'aria-label': 'Play history', onclick: openPlayHistory }, icon('history')),
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
    actionRow('play', 'Play now', () => { if (!can('queue')) return deny('queue'); send({ t: 'music.playnow', qid: t.qid }); s.close(); }, can('queue') ? '' : 'locked'),
    i > 0 ? actionRow('up', 'Move to top', () => { if (!can('queue')) return deny('queue'); send({ t: 'music.move', qid: t.qid, to: 0 }); s.close(); }, can('queue') ? '' : 'locked') : null,
    actionRow('list-plus', 'Add to a playlist', () => { s.close(); addToPlaylist([t]); }),
    actionRow('trash', 'Remove from queue', () => { if (!(mine || can('queue'))) return deny('queue'); send({ t: 'music.remove', qid: t.qid }); s.close(); }, mine || can('queue') ? 'red' : 'red locked'));
  const s = sheet({ title: t.title, body });
}

// ======================= play history =======================
// What played in this voice chat, who added it and when — grouped by day, more loads as you scroll.
export function openPlayHistory() {
  const list = h('div'), foot = h('div', { class: 'inv-foot' });
  let mine = false, next = 0, more = true, busy = false, gen = 0, lastDay = '';
  const seg = h('div', { class: 'seg', style: { margin: '2px 0 10px' } });
  const drawSeg = () => seg.replaceChildren(...[[false, 'Everyone'], [true, 'Added by me']].map(([v, l]) =>
    h('button', { class: mine === v ? 'on' : '', onclick: () => { mine = v; drawSeg(); restart(); } }, l)));
  const day = (ts) => {
    const d = new Date(ts * 1000), t = new Date(); const y = new Date(t); y.setDate(t.getDate() - 1);
    return d.toDateString() === t.toDateString() ? 'Today' : d.toDateString() === y.toDateString() ? 'Yesterday'
      : d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' });
  };
  const time = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const s = sheet({ title: 'Play history', body: h('div', null, seg, list, foot), tall: true, onClose: () => io.disconnect() });
  const load = async () => {
    if (busy || !more) return;
    busy = true; const my = gen;
    foot.replaceChildren(h('div', { class: 'spinner', style: { margin: '14px auto' } }));
    try {
      const r = await api(`/api/history?offset=${next}&limit=40${mine ? '&mine=1' : ''}`);
      if (my !== gen) return;
      for (const t of r.tracks) {
        const d = day(t.ts);
        if (d !== lastDay) { list.append(h('div', { class: 'grp-h' }, d)); lastDay = d; }
        const row = trackRow(t, { sub: `${t.artist ? t.artist + ' · ' : ''}${time(t.ts)} · ${t.by_uid === S.uid ? 'you' : t.by_name || 'autoplay'}`, btn: icon('plus-c'), onBtn: () => addTrack(t, row) });
        list.append(pressable(row, () => songMenu(t), () => songMenu(t)));
      }
      more = r.more; next = r.next;
      foot.replaceChildren(list.children.length ? '' : h('div', { class: 'empty-note' }, mine ? 'You haven’t added songs here yet.' : 'Nothing has played in this voice chat yet.'));
    } catch (e) { if (my === gen) foot.replaceChildren(h('div', { class: 'empty-note' }, e.message)); }
    finally { if (my === gen) busy = false; }
  };
  const restart = () => { gen++; busy = false; more = true; next = 0; lastDay = ''; list.replaceChildren(); load(); };
  const io = new IntersectionObserver((es) => { if (es.some(e => e.isIntersecting)) load(); }, { root: s.body, rootMargin: '300px' });
  io.observe(foot);
  drawSeg(); restart();
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
    actionRow('close', 'Stop music', async () => { if (!can('stop')) return deny('stop'); s.close(); if (await confirmBox('Stop the music and clear the queue?', 'Stop', true)) send({ t: 'music.stop' }); }, can('stop') ? 'red' : 'red locked'));
  const s = sheet({ title: t.title, body });
}

// ======================= audio / video + quality =======================
function toggleMode() { setVideo(!isVideo()); }
async function openQuality() {
  const t = cur();
  const body = h('div');
  sheet({ title: 'Quality', body });
  const render = () => {
    body.replaceChildren(
      h('div', { class: 'grp-h' }, 'Mode'),
      h('div', { class: 'seg', style: { margin: '8px 0 16px' } },
        ...[['audio', 'Audio only'], ['video', 'Video']].map(([k, l]) => h('button', { class: (isVideo() ? 'video' : 'audio') === k ? 'on' : '', onclick: () => { if ((k === 'video') !== isVideo()) toggleMode(); render(); } }, l))),
      h('div', { class: 'grp-h' }, 'Audio quality'),
      h('div', { class: 'seg', style: { margin: '8px 0 16px' } },
        ...[['low', 'Data saver'], ['medium', 'Standard'], ['high', 'High']].map(([k, l]) => h('button', { class: P.aq === k ? 'on' : '', onclick: () => setAq(k) }, l))),
      h('div', { class: 'grp-h' }, 'Video quality'),
      h('div', { class: 'list' },
        ...(P.heights.length ? P.heights : HEIGHTS).map(q =>
          h('button', { class: 'row', onclick: () => setVq(q) },
            h('span', { class: 'lab' }, `${q}p`, q === 480 ? h('small', null, 'Default') : null),
            String(P.vq) === String(q) ? icon('check') : null))));
  };
  const setAq = (k) => { P.aq = k; store.set('aq', k); const c = P.loadedTrack; if (c) { audio.src = audioSrc(c); audio.load(); } render(); };
  const setVq = (q) => { P.vq = Number(q); store.set('vq', P.vq); delete video.dataset.src; if (isVideo()) loadVideo(); render(); };
  render();
  if (t) { try { P.heights = (await api(`/api/formats/${t.id}`)).heights; render(); } catch {} }
}

// ======================= effects sheet =======================
// Everyone hears the same effect. Only admins, or whoever added the playing song, can change it.
const canFx = () => can('fx') || (!!cur() && cur().byUid === S.uid && can('fx_own'));
function openFx() {
  unlockAudio();
  const body = h('div');
  let draft = { ...fx() }, tmr = null;
  // you hear your change at once (EQ / reverb / 8D); the server then applies it for everyone, speed included
  const push = () => { buildGraph(); applyFx(draft); clearTimeout(tmr); tmr = setTimeout(() => send({ t: 'music.fx', fx: draft }), 80); };
  const slider = (key, label, min, max, step, fmt, allowed) => {
    const out = h('output', null, fmt(draft[key]));
    const r = h('input', { type: 'range', min, max, step, value: draft[key], 'aria-label': label });
    const paint = () => r.style.setProperty('--v', ((r.value - min) / (max - min) * 100) + '%');
    r.oninput = () => {
      draft[key] = Number(r.value); draft.preset = 'custom'; out.textContent = fmt(draft[key]); paint(); push();
      body.querySelectorAll('.fx').forEach(b => b.classList.remove('on'));
    };
    paint();
    // not allowed: the slider can't move, and tapping it says why (in red)
    return h('div', { class: 'slider-row' + (allowed ? '' : ' ro'), onclick: () => { if (!allowed) deny('fx'); } }, h('label', null, label), r, out);
  };
  const render = () => {
    draft = { ...fx() };
    const allowed = canFx();
    body.replaceChildren(
      allowed ? null : h('p', { class: 'muted', style: { fontSize: '13px', margin: '0 0 6px' } }, 'You can’t change the sound of this song.'),
      h('div', { class: 'fx-grid' }, ...Object.entries(PRESETS).map(([k, p]) =>
        h('button', { class: 'fx' + (draft.preset === k ? ' on' : '') + (allowed ? '' : ' locked'), onclick: (e) => { if (!allowed) return deny('fx'); draft = { ...p.v, preset: k }; if (k !== 'normal') buildGraph(); applyFx(draft); send({ t: 'music.fx', fx: draft }); haptic('select');
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
let lastAdd = { id: '', at: 0 };
function addTrack(t, el) {
  if (!canAdd()) return deny('add');
  if (lastAdd.id === t.id && Date.now() - lastAdd.at < 1500) return; // a double tap shouldn't queue it twice
  lastAdd = { id: t.id, at: Date.now() };
  send({ t: 'music.add', track: t });
  BR.added.add(t.id);
  haptic('success');
  el?.classList.add('added');
}
// Tapping a song: three clearly different choices, so people can read and pick (no accidental adds)
function songMenu(t) {
  const big = (cls, ic, label, sub, perm, fn) => h('button', { class: 'pick ' + cls + (can(perm) && can('add') ? '' : ' locked'), onclick: () => {
    if (!can('add')) return deny('add');
    if (!can(perm)) return deny(perm);
    fn(); m.close(); haptic('success');
  } }, icon(ic, ic === 'plus' ? '' : 'fill'), h('span', null, h('b', null, label), h('small', null, sub)));
  const m = sheet({ title: t.title, body: h('div', null,
    h('p', { class: 'muted', style: { margin: '-6px 0 12px' } }, [t.artist, t.duration ? fmtTime(t.duration) : ''].filter(Boolean).join(' · ')),
    h('div', { class: 'picks' },
      big('now', 'play', 'Play now', 'Starts right away for everyone', 'playback', () => send({ t: 'music.add', track: t, now: true })),
      big('next', 'next', 'Play next', 'Plays after the current song', 'next', () => send({ t: 'music.add', track: t, next: true })),
      big('queue', 'plus', 'Add to queue', 'Plays after the songs already queued', 'add', () => addTrack(t))),
    h('div', { class: 'list' },
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
  return pressable(el, () => songMenu(t), () => songMenu(t));
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
        actionRow('history', 'Play history of this voice chat', () => openPlayHistory(), 'acc'),
        actionRow('plus', 'New playlist', async () => {
          const name = await prompt_('New playlist', 'Playlist name');
          if (name) { await api('/api/playlists', { body: { name } }); showList(); }
        }, 'acc')),
      h('div', { class: 'gt-grid two' }, ...pls.map(p => h('button', { class: 'pl-tile', onclick: () => showPlaylist(p.id) },
        h('div', { class: 'pl-art', style: { backgroundImage: p.thumb ? `url("${media(p.thumb)}")` : '' } }, p.thumb ? null : icon(p.name === 'Liked Songs' ? 'heart' : 'music')),
        h('div', { class: 'cc-t' }, p.name), h('div', { class: 'cc-a' }, `${p.count} songs`)))));
  };
  const backBtn = () => h('button', { class: 'br-back inline', 'aria-label': 'Back', onclick: showList }, icon('down'));
  const showPlaylist = async (id) => {
    const p = await api('/api/playlists/' + id);
    const list = h('div', null, ...p.tracks.map(t => {
      const row = trackRow(t, { btn: icon('more'), onBtn: () => {
        const m = sheet({ title: t.title, body: h('div', { class: 'list' },
          canAdd() ? actionRow('plus', 'Add to queue', () => { addTrack(t); m.close(); }) : null,
          actionRow('trash', 'Remove from playlist', async () => { await api('/api/playlists/' + id, { body: { remove: t.item } }); m.close(); row.remove(); if (p.name === 'Liked Songs') S.liked.delete(t.id); }, 'red')) });
      } });
      return pressable(row, () => songMenu(t), () => songMenu(t));
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
      if (r.spotify) {
        const sp = r.spotify;
        results.replaceChildren(
          h('div', { class: 'pl-card spotify' },
            h('div', { class: 'ta sp-logo' }, icon('music')),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { style: { fontWeight: 700, fontSize: '17px' } }, sp.title),
              h('div', { class: 'muted', style: { fontSize: '13px', margin: '2px 0 10px' } }, `Spotify ${sp.kind} · ${sp.count} song${sp.count === 1 ? '' : 's'} · each one is found on YouTube`),
              canAdd() ? h('button', { class: 'btn sm', onclick: (e) => { send({ t: 'music.spotify', url: sp.url }); haptic('success'); e.currentTarget.disabled = true; e.currentTarget.textContent = 'Adding…'; } }, 'Add all to queue') : null)),
          ...sp.songs.map(t => h('div', { class: 'trk' }, h('div', { class: 'tm' }, h('div', { class: 'tt' }, t.title), h('div', { class: 'ts' }, `${t.artist}${t.duration ? ' · ' + fmtTime(t.duration) : ''}`)))));
      } else if (r.playlist) {
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
