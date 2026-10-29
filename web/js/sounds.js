// Shared AudioContext + synthesized UI sounds and soundboard (no audio files needed).
let ctx = null;
export function getCtx() {
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC({ latencyHint: 'playback' });
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  return ctx;
}
export const ctxReady = () => ctx && ctx.state === 'running';

let sfxGain = null;
function out() {
  const c = getCtx();
  if (!c) return null;
  if (!sfxGain) { sfxGain = c.createGain(); sfxGain.gain.value = 0.5; sfxGain.connect(c.destination); }
  return c;
}

function tone(freq, start, dur, type = 'sine', vol = 0.3, slideTo = null) {
  const c = out(); if (!c) return;
  const t = c.currentTime + start;
  const o = c.createOscillator(), g = c.createGain();
  o.type = type; o.frequency.setValueAtTime(freq, t);
  if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(vol, t + 0.015);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  o.connect(g).connect(sfxGain);
  o.start(t); o.stop(t + dur + 0.05);
}
function noise(start, dur, vol = 0.3, filterFreq = 2000, type = 'bandpass', q = 0.8) {
  const c = out(); if (!c) return;
  const t = c.currentTime + start;
  const len = Math.ceil(c.sampleRate * dur);
  const buf = c.createBuffer(1, len, c.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
  const src = c.createBufferSource(); src.buffer = buf;
  const f = c.createBiquadFilter(); f.type = type; f.frequency.value = filterFreq; f.Q.value = q;
  const g = c.createGain();
  g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  src.connect(f).connect(g).connect(sfxGain);
  src.start(t);
}

export function joinSound() { tone(660, 0, 0.18, 'sine', 0.18); tone(990, 0.09, 0.25, 'sine', 0.15); }
export function leaveSound() { tone(880, 0, 0.18, 'sine', 0.15); tone(587, 0.1, 0.28, 'sine', 0.13); }
export function micSound(on) { on ? tone(1200, 0, 0.07, 'sine', 0.12) : tone(700, 0, 0.07, 'sine', 0.1); }

export const BOARD = [
  ['airhorn', '📯', 'Air horn'], ['applause', '👏', 'Applause'], ['drumroll', '🥁', 'Drum roll'], ['rimshot', '🥁', 'Ba dum tss'],
  ['bell', '🔔', 'Bell'], ['whoosh', '💨', 'Whoosh'], ['laugh', '😂', 'Laugh'], ['wow', '😮', 'Wow'],
];
export function playBoard(id) {
  switch (id) {
    case 'airhorn':
      for (let i = 0; i < 3; i++) { tone(466, i * 0.28, 0.24, 'sawtooth', 0.12); tone(587, i * 0.28, 0.24, 'sawtooth', 0.08); }
      break;
    case 'applause':
      for (let i = 0; i < 45; i++) noise(Math.random() * 1.8, 0.04 + Math.random() * 0.05, 0.25 + Math.random() * 0.2, 1500 + Math.random() * 2500);
      break;
    case 'drumroll':
      for (let i = 0; i < 26; i++) noise(i * 0.05, 0.06, 0.18 + i * 0.008, 900, 'bandpass', 1.2);
      noise(1.35, 0.6, 0.5, 4000, 'highpass', 0.5); tone(70, 1.35, 0.4, 'sine', 0.5, 40);
      break;
    case 'rimshot':
      tone(180, 0, 0.12, 'sine', 0.5, 90); noise(0, 0.08, 0.25, 1800);
      tone(160, 0.22, 0.12, 'sine', 0.5, 80); noise(0.22, 0.08, 0.25, 1800);
      noise(0.45, 0.9, 0.35, 6000, 'highpass', 0.6);
      break;
    case 'bell':
      [1, 2.76, 5.4].forEach((m, i) => tone(660 * m, 0, 1.6 - i * 0.4, 'sine', 0.18 / (i + 1)));
      break;
    case 'whoosh': {
      const c = out(); if (!c) return;
      noise(0, 0.7, 0.35, 400, 'bandpass', 1.5);
      break;
    }
    case 'laugh':
      for (let i = 0; i < 6; i++) tone(420 - i * 18, i * 0.14, 0.11, 'triangle', 0.18, 330 - i * 18);
      break;
    case 'wow':
      tone(260, 0, 0.7, 'sawtooth', 0.09, 420); tone(390, 0, 0.7, 'triangle', 0.08, 620);
      break;
  }
}
