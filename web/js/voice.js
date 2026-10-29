// Voice / video via LiveKit (SFU). Our WebSocket is the source of truth for the room; LiveKit only moves media.
import { S, api, emit, h, send, store, toast } from './core.js';

let LK = null, room = null, info = null, connecting = false;
const volumes = new Map(Object.entries(store.get('vols', {})).map(([k, v]) => [Number(k), v]));
const mutedForMe = new Set(store.get('mutedForMe', []));
export const V = { ready: false, mic: false, cam: false, screen: false, levels: new Map(), speaking: new Set() };

async function loadSDK() {
  if (LK) return LK;
  for (const src of ['/static/vendor/livekit-client.esm.mjs', 'https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.esm.mjs']) {
    try { LK = await import(src); return LK; } catch (e) { console.warn('livekit load failed', src, e); }
  }
  return null;
}

export async function startVoice(lk) {
  info = lk;
  if (!lk) { emit('voice', { state: 'off' }); return; }
  if (!(await loadSDK())) { toast('Voice could not load. Check your connection.'); emit('voice', { state: 'off' }); return; }
  await connect();
}

async function connect() {
  if (connecting) return;
  connecting = true;
  emit('voice', { state: 'connecting' });
  const old = room; room = null;
  if (old) { try { await old.disconnect(); } catch {} old.removeAllListeners?.(); }
  try {
    room = new LK.Room({
      adaptiveStream: true, dynacast: true,
      audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      publishDefaults: { dtx: true, red: true },
    });
    wire(room);
    await room.connect(info.url, info.token, { autoSubscribe: true });
    V.ready = true;
    emit('voice', { state: 'ready' });
    if (!room.canPlaybackAudio) emit('voice:blocked', true);
    room.remoteParticipants.forEach(applyVolume);
  } catch (e) {
    console.error(e);
    V.ready = false;
    emit('voice', { state: 'error' });
    toast('Voice connection failed — retrying…');
    scheduleRetry(4000);
  } finally { connecting = false; }
}

let retryTimer = null, stopped = false;
function scheduleRetry(ms) {
  if (stopped) return;
  clearTimeout(retryTimer);
  retryTimer = setTimeout(retry, ms);
}
async function retry() {
  if (stopped) return;
  try { const r = await api('/api/lk'); info = r.livekit; } catch {}
  if (info) connect();
}
function resetLocal() {
  // after a drop nothing is published any more: tell the room our mic/camera are off
  const was = V.mic || V.cam || V.screen;
  if (V.mic) send({ t: 'mic', on: false });
  if (V.cam) send({ t: 'video', on: false });
  if (V.screen) send({ t: 'screen', on: false });
  V.mic = V.cam = V.screen = false;
  if (was) emit('voice:local');
}

function wire(r) {
  const E = LK.RoomEvent;
  r.on(E.TrackSubscribed, (track, pub, p) => {
    const uid = Number(p.identity);
    if (track.kind === 'audio') {
      const el = track.attach();
      el.dataset.uid = uid;
      el.style.display = 'none';
      document.body.append(el);
      applyVolume(p);
      emit('voice:audio-track', { uid, track });
    } else if (track.kind === 'video') {
      emit('voice:video', { uid, track, source: pub.source, on: true });
    }
  });
  r.on(E.TrackUnsubscribed, (track, pub, p) => {
    track.detach().forEach(el => el.remove());
    if (track.kind === 'video') emit('voice:video', { uid: Number(p.identity), track, source: pub.source, on: false });
  });
  r.on(E.LocalTrackPublished, (pub) => {
    if (pub.track?.kind === 'video') emit('voice:video', { uid: S.uid, track: pub.track, source: pub.source, on: true, local: true });
    if (pub.track?.kind === 'audio') emit('voice:audio-track', { uid: S.uid, track: pub.track, local: true });
  });
  r.on(E.LocalTrackUnpublished, (pub) => {
    if (pub.track?.kind === 'video' || pub.kind === 'video') emit('voice:video', { uid: S.uid, track: pub.track, source: pub.source, on: false, local: true });
    if (pub.source === LK.Track.Source.ScreenShare && V.screen) { V.screen = false; send({ t: 'screen', on: false }); emit('voice:local'); }
  });
  r.on(E.ActiveSpeakersChanged, (speakers) => {
    V.speaking = new Set(speakers.map(p => Number(p.identity)));
    emit('voice:speaking', V.speaking);
  });
  r.on(E.AudioPlaybackStatusChanged, () => emit('voice:blocked', !r.canPlaybackAudio));
  r.on(E.ParticipantConnected, applyVolume);
  r.on(E.ParticipantPermissionsChanged, (prev, p) => {
    if (p === r.localParticipant && !p.permissions?.canPublish) {
      V.mic = V.cam = V.screen = false;
      emit('voice:local');
    }
  });
  r.on(E.Disconnected, (reason) => {
    if (r !== room) return; // an old room we already replaced
    V.ready = false;
    resetLocal();
    const R = LK.DisconnectReason || {};
    if (reason === R.CLIENT_INITIATED) return;
    if (reason === R.PARTICIPANT_REMOVED || reason === R.DUPLICATE_IDENTITY) {
      // removed by an admin, or this account joined from another device: don't fight it
      emit('voice', { state: 'off' });
      if (reason === R.DUPLICATE_IDENTITY) toast('You joined the voice chat on another device');
      return;
    }
    emit('voice', { state: 'connecting' });
    scheduleRetry(1500);
  });
  startLevels();
}

// audio levels for speaking rings. One loop for the whole page (it used to start a new one on every
// reconnect), ~20 fps and only emitting when something changed, so idle rooms cost nothing.
let levelsOn = false, lastTick = 0, lastSum = -1;
function startLevels() {
  if (levelsOn) return;
  levelsOn = true;
  const tick = (t) => {
    requestAnimationFrame(tick);
    if (!room || t - lastTick < 50 || document.hidden) return;
    lastTick = t;
    let sum = 0;
    const seen = new Set();
    for (const p of [room.localParticipant, ...room.remoteParticipants.values()]) {
      const uid = Number(p.identity), l = p.isSpeaking ? p.audioLevel : 0;
      seen.add(uid); V.levels.set(uid, l); sum += l;
    }
    for (const uid of V.levels.keys()) if (!seen.has(uid)) V.levels.delete(uid);
    if (sum === 0 && lastSum === 0) return;
    lastSum = sum;
    emit('voice:levels', V.levels);
  };
  requestAnimationFrame(tick);
}

export async function unblockAudio() { try { await room?.startAudio(); emit('voice:blocked', false); } catch {} }

// After an admin changes our permissions, LiveKit needs a fresh token with canPublish.
export async function refreshPermissions() {
  if (!room) return;
  const canPub = room.localParticipant.permissions?.canPublish;
  if (canPub) return;
  // server-side UpdateParticipant usually updates permissions live; wait briefly for it
  await new Promise(r => setTimeout(r, 700));
}

export async function setMic(on) {
  if (!room || !V.ready) { toast('Voice is still connecting'); return false; }
  try {
    if (on) await refreshPermissions();
    await room.localParticipant.setMicrophoneEnabled(on);
    V.mic = on;
    send({ t: 'mic', on });
    return true;
  } catch (e) {
    console.error(e);
    toast(on ? 'Allow microphone access in Telegram settings to speak' : 'Could not mute');
    return false;
  }
}
async function setSource(kind, on) {
  const lp = room.localParticipant;
  const fn = kind === 'cam' ? lp.setCameraEnabled : lp.setScreenShareEnabled;
  if (fn) return fn.call(lp, on, kind === 'cam' ? { resolution: LK.VideoPresets?.h540?.resolution } : { audio: false });
  return lp.setTrackEnabled(kind === 'cam' ? LK.Track.Source.Camera : LK.Track.Source.ScreenShare, on);
}
export async function setCam(on) {
  if (!room || !V.ready) return toast('Voice is still connecting');
  try { await setSource('cam', on); V.cam = on; send({ t: 'video', on }); emit('voice:local'); }
  catch (e) { console.error(e); toast('Camera unavailable — allow camera access'); }
}
export const screenSupported = () => !!navigator.mediaDevices?.getDisplayMedia;
export async function setScreen(on) {
  if (!room || !V.ready) return toast('Voice is still connecting');
  if (!screenSupported()) return toast('Screen sharing works on desktop Telegram');
  try { await setSource('screen', on); V.screen = on; send({ t: 'screen', on }); emit('voice:local'); }
  catch (e) { console.error(e); V.screen = false; emit('voice:local'); }
}

// per-user volume (0..1) and mute-for-me
function applyVolume(p) {
  const uid = Number(p.identity);
  const v = mutedForMe.has(uid) ? 0 : (volumes.get(uid) ?? 1);
  try { p.setVolume(v); } catch {}
}
export const getVolume = (uid) => volumes.get(uid) ?? 1;
export const isMutedForMe = (uid) => mutedForMe.has(uid);
export function setVolume(uid, v) {
  volumes.set(uid, v);
  store.set('vols', Object.fromEntries(volumes));
  const p = room?.remoteParticipants.get(String(uid)); if (p) applyVolume(p);
}
export function toggleMuteForMe(uid) {
  mutedForMe.has(uid) ? mutedForMe.delete(uid) : mutedForMe.add(uid);
  store.set('mutedForMe', [...mutedForMe]);
  const p = room?.remoteParticipants.get(String(uid)); if (p) applyVolume(p);
  return mutedForMe.has(uid);
}

export async function leaveVoice() {
  stopped = true; clearTimeout(retryTimer);
  const r = room; room = null; V.ready = false;
  try { await r?.disconnect(); } catch {}
}

// for the recorder
export function audioTracks() {
  if (!room) return [];
  const out = [];
  room.remoteParticipants.forEach(p => p.audioTrackPublications.forEach(pub => { if (pub.track?.mediaStreamTrack) out.push(pub.track.mediaStreamTrack); }));
  room.localParticipant.audioTrackPublications.forEach(pub => { if (pub.track?.mediaStreamTrack) out.push(pub.track.mediaStreamTrack); });
  return out;
}
export function attachVideo(track) { const el = track.attach(); el.playsInline = true; el.muted = true; return el; }
