// Voice / video via LiveKit (SFU). Our WebSocket is the source of truth for the room; LiveKit only moves media.
import { S, api, emit, send, store, toast } from './core.js';

const LK_VERSION = '2.23.0';
let LK = null, room = null, info = null, connecting = false;
const volumes = new Map(Object.entries(store.get('vols', {})).map(([k, v]) => [Number(k), v]));
const mutedForMe = new Set(store.get('mutedForMe', []));
let roomVol = store.get('roomVol', 1);
export const V = {
  ready: false, mic: false, cam: false, screen: false, facing: 'user',
  noise: store.get('noise', true), speaking: new Set(),
};

async function loadSDK() {
  if (LK) return LK;
  for (const src of ['/static/vendor/livekit-client.esm.mjs', `https://cdn.jsdelivr.net/npm/livekit-client@${LK_VERSION}/dist/livekit-client.esm.mjs`]) {
    try { LK = await import(src); return LK; } catch (e) { console.warn('livekit load failed', src, e); }
  }
  return null;
}

export async function startVoice(lk) {
  info = lk;
  stopped = false; failN = 0; // (leaving stopped it for good: coming back from the page cache couldn't reconnect)
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
    const mic = store.get('dev:audioinput', ''), out = store.get('dev:audiooutput', '');
    room = new LK.Room({
      adaptiveStream: true, dynacast: true,
      audioCaptureDefaults: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1,
        ...(mic ? { deviceId: mic } : {}),
      },
      ...(out ? { audioOutput: { deviceId: out } } : {}),
      // DTX (stop sending in silence) clips the first syllable after a pause, which sounds like the voice
      // flickering; RED resends audio so lost packets don't crackle. 48 kbps keeps voices clear.
      publishDefaults: { dtx: false, red: true, audioPreset: { maxBitrate: 48000 } },
      videoCaptureDefaults: { resolution: LK.VideoPresets?.h540?.resolution },
    });
    wire(room);
    await room.connect(info.url, info.token, { autoSubscribe: true });
    V.ready = true;
    if (failN) toast('Voice connected');
    failN = 0;
    emit('voice', { state: 'ready' });
    emit('voice:session', 'joined');
    if (!room.canPlaybackAudio) emit('voice:blocked', true);
    room.remoteParticipants.forEach(applyVolume);
  } catch (e) {
    console.error(e);
    V.ready = false;
    emit('voice', { state: 'error' });
    // said once, not every 4 seconds while the voice server is down; the wait grows too
    if (!failN++) toast('Voice connection failed — retrying…');
    scheduleRetry(Math.min(30000, 4000 * 2 ** Math.min(failN - 1, 3)));
  } finally { connecting = false; }
}

let retryTimer = null, stopped = false, failN = 0;
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

// LiveKit identity -> uid. Someone appearing as the group joins the voice under a stand-in id (their tile says
// which, in "lk"); everyone else under their Telegram id.
const uidOf = (p) => {
  const id = Number(p.identity);
  for (const q of S.parts.values()) if (q.lk === id) return q.uid;
  return id;
};
function videoEvent(pub, p, on) {
  const local = p === room?.localParticipant;
  emit('voice:video', { uid: local ? S.uid : uidOf(p), track: pub.track, source: pub.source, on: on && !!pub.track, local });
}

function wire(r) {
  const E = LK.RoomEvent;
  r.on(E.TrackSubscribed, (track, pub, p) => {
    if (track.kind === 'audio') {
      const el = track.attach();
      el.dataset.uid = uidOf(p);
      el.style.display = 'none';
      document.body.append(el);
      applyVolume(p);
      emit('voice:audio-track', { uid: uidOf(p), track });
    } else if (track.kind === 'video' && !pub.isMuted) {
      videoEvent(pub, p, true);
    }
  });
  r.on(E.TrackUnsubscribed, (track, pub, p) => {
    track.detach().forEach(el => el.remove());
    if (track.kind === 'video') videoEvent(pub, p, false);
  });
  // A muted camera keeps its publication but sends no frames: hide the tile instead of showing a black box.
  r.on(E.TrackMuted, (pub, p) => { if (pub.kind === 'video') videoEvent(pub, p, false); });
  r.on(E.TrackUnmuted, (pub, p) => { if (pub.kind === 'video') videoEvent(pub, p, true); });
  r.on(E.TrackPublished, (pub, p) => { if (pub.kind === 'audio') applyVolume(p); });
  r.on(E.LocalTrackPublished, (pub) => {
    const p = r.localParticipant;
    if (pub.kind === 'video' && !pub.isMuted) videoEvent(pub, p, true);
    if (pub.kind === 'audio') emit('voice:audio-track', { uid: S.uid, track: pub.track, local: true });
  });
  r.on(E.LocalTrackUnpublished, (pub) => {
    if (pub.kind === 'video') videoEvent(pub, r.localParticipant, false);
    if (pub.source === LK.Track.Source.ScreenShare && V.screen) { V.screen = false; send({ t: 'screen', on: false }); emit('voice:local'); }
  });
  r.on(E.ActiveSpeakersChanged, (speakers) => {
    V.speaking = new Set(speakers.map(uidOf));
    emit('voice:speaking', V.speaking);
  });
  r.on(E.AudioPlaybackStatusChanged, () => emit('voice:blocked', !r.canPlaybackAudio));
  r.on(E.ParticipantConnected, applyVolume);
  r.on(E.MediaDevicesChanged, () => emit('voice:devices'));
  // excellent / good / poor / lost, for every participant (LiveKit measures it for everyone)
  r.on(E.ConnectionQualityChanged, (q, p) => emit('voice:quality', { uid: p === r.localParticipant ? S.uid : uidOf(p), q }));
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
}

export async function unblockAudio() {
  try { if (room && !room.canPlaybackAudio) await room.startAudio(); } catch {}
  if (!room || room.canPlaybackAudio) emit('voice:blocked', false);
}
export const audioBlocked = () => !!room && !room.canPlaybackAudio;

// After an admin changes our permissions, LiveKit needs a fresh token with canPublish.
export async function refreshPermissions() {
  if (!room) return;
  const canPub = room.localParticipant.permissions?.canPublish;
  if (canPub) return;
  // server-side UpdateParticipant usually updates permissions live; wait briefly for it
  await new Promise(r => setTimeout(r, 700));
}

// ---------------- noise isolation (RNNoise, runs on the phone) ----------------
// Removes fans, traffic, keyboard and room noise from your mic before it is sent. If the phone can't run it
// (no AudioWorklet / WebAssembly), the browser's own noise suppression is still on.
let ns = null, nsC = null, nsState = 'idle'; // idle | active | failed | off
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
function nsCtx() {
  // Created right inside the mic tap: an audio context made later (after awaits) can stay "suspended",
  // which silently stopped noise isolation from working.
  if (!nsC) nsC = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000, latencyHint: 'interactive' });
  if (nsC.state !== 'running') nsC.resume().catch(() => {});
  return nsC;
}
async function nsSetup() {
  const ctx = nsCtx();
  if (!ns) {
    const base = '/static/vendor/nsuppress/';
    const mod = await import(base + 'index.js');
    const [wasm] = await Promise.all([
      mod.loadRnnoise({ url: base + 'rnnoise.wasm', simdUrl: base + 'rnnoise_simd.wasm' }),
      ctx.audioWorklet.addModule(base + 'rnnoiseWorklet.js'),
      ctx.audioWorklet.addModule('/static/js/gate-worklet.js'),
    ]);
    ns = { ctx, mod, wasm };
  }
  if (ctx.state !== 'running') await Promise.race([ctx.resume(), sleep(1500)]);
  if (ctx.state !== 'running') throw new Error('audio context not running');
  return ns;
}
function rnnoiseProcessor() {
  let src = null, node = null, gate = null;
  const proc = {
    name: 'rnnoise',
    async init({ track, audioContext }) {
      src = audioContext.createMediaStreamSource(new MediaStream([track]));
      node = new ns.mod.RnnoiseWorkletNode(audioContext, { maxChannels: 1, wasmBinary: ns.wasm });
      // RNNoise cleans speech; the soft gate turns down the hiss it leaves while you're not talking
      gate = new AudioWorkletNode(audioContext, 'vc-soft-gate');
      const dest = audioContext.createMediaStreamDestination();
      src.connect(node).connect(gate).connect(dest);
      proc.processedTrack = dest.stream.getAudioTracks()[0];
    },
    async restart(opts) { await proc.destroy(); await proc.init(opts); },
    async destroy() {
      try { src?.disconnect(); node?.disconnect(); gate?.disconnect(); node?.destroy(); } catch {}
      src = node = gate = null;
    },
  };
  return proc;
}
function setNsState(st) { if (nsState !== st) { nsState = st; emit('voice:noise', st); } }
async function applyNoise() {
  const track = room?.localParticipant.getTrackPublication(LK.Track.Source.Microphone)?.audioTrack;
  if (!track) return;
  const active = !!track.getProcessor?.();
  try {
    if (V.noise && !active) {
      const { ctx } = await nsSetup();
      await track.setAudioContext(ctx);
      await track.setProcessor(rnnoiseProcessor());
      setNsState('active');
    } else if (!V.noise && active) {
      await track.stopProcessor();
      setNsState('off');
    } else if (V.noise && active) setNsState('active');
  } catch (e) {
    console.warn('noise isolation unavailable', e);
    try { if (track.getProcessor?.()) await track.stopProcessor(); } catch {} // never leave the mic silent
    if (nsState !== 'failed') toast('Noise isolation isn’t supported on this phone — using standard noise reduction');
    setNsState('failed');
  }
}
export function noiseStatus() {
  if (!V.noise) return 'Off';
  if (nsState === 'failed') return 'Not supported on this phone — using standard noise reduction';
  if (nsState === 'active' && V.mic) return 'On — removing background noise from your mic';
  return 'Removes background noise when your mic is on';
}
export async function setNoise(on) {
  V.noise = on;
  store.set('noise', on);
  if (on && nsState === 'failed') nsState = 'idle';
  if (on) nsCtx();
  await applyNoise();
  emit('voice:noise', nsState);
}
// Telegram in the background: phones give a hidden app little CPU, and the noise isolation (which runs every
// 2.7 ms) then crackles. While hidden, the mic uses the phone's own noise reduction; it switches back on return.
let bgOff = false;
document.addEventListener('visibilitychange', async () => {
  const track = room?.localParticipant.getTrackPublication(LK?.Track.Source.Microphone)?.audioTrack;
  if (document.hidden) {
    if (track?.getProcessor?.()) { bgOff = true; try { await track.stopProcessor(); } catch {} }
  } else {
    if (nsC && nsC.state !== 'running') nsC.resume().catch(() => {});
    if (bgOff) { bgOff = false; await applyNoise(); }
  }
});
document.addEventListener('pointerdown', () => { if (nsC && nsC.state !== 'running' && nsState === 'active') nsC.resume().catch(() => {}); }, { capture: true, passive: true });

// iPhone: tell iOS up front whether we record. Otherwise it flips the audio session between "playback" and
// "voice call" as mics start and stop, and the volume jumps up and down (and music gets ducked).
function iosSession(recording) {
  try {
    if (navigator.audioSession && navigator.audioSession.type !== (recording ? 'play-and-record' : 'playback')) {
      navigator.audioSession.type = recording ? 'play-and-record' : 'playback';
      emit('voice:session', recording ? 'record' : 'play');
    }
  } catch {}
}
iosSession(false);

export async function setMic(on) {
  if (!room || !V.ready) { toast('Voice is still connecting'); return false; }
  if (on) iosSession(true);
  if (on && V.noise && nsState !== 'failed') { try { nsCtx(); } catch {} }
  try {
    if (on) await refreshPermissions();
    await room.localParticipant.setMicrophoneEnabled(on);
    if (on) await applyNoise();
    V.mic = on;
    send({ t: 'mic', on });
    if (!on) iosSession(false);
    // the phone switches its audio between "media" and "call" mode as the mic starts/stops (Android too):
    // a playing song can go silent right then, so the music player gets a nudge
    emit('voice:session', on ? 'record' : 'play');
    return true;
  } catch (e) {
    console.error(e);
    if (!on) {
      // Turning the mic off can't really fail for us: when an admin took the right to speak away, LiveKit has
      // already stopped it and throws here. It's off either way ("Could not mute" was wrong).
      V.mic = false;
      send({ t: 'mic', on: false });
      iosSession(false);
      emit('voice:session', 'play');
      return true;
    }
    toast('Allow microphone access in Telegram settings to speak');
    return false;
  }
}

// ---------------- camera + screen ----------------
const camRes = () => LK.VideoPresets?.h540?.resolution;
export async function setCam(on, facing = V.facing) {
  if (!room || !V.ready) return toast('Voice is still connecting');
  const lp = room.localParticipant;
  try {
    if (on) {
      V.facing = facing;
      await lp.setCameraEnabled(true, { facingMode: facing, resolution: camRes() });
    } else {
      // Unpublish instead of mute: the camera light goes off, everyone's tile disappears at once, and the
      // next start can use the other camera.
      const pub = lp.getTrackPublication(LK.Track.Source.Camera);
      if (pub?.track) await lp.unpublishTrack(pub.track, true);
    }
    V.cam = on;
    send({ t: 'video', on });
    emit('voice:local');
  } catch (e) {
    console.error(e);
    toast('Camera unavailable — allow camera access');
  }
}
export async function flipCamera() {
  const track = room?.localParticipant.getTrackPublication(LK.Track.Source.Camera)?.videoTrack;
  if (!track) return;
  const facing = V.facing === 'user' ? 'environment' : 'user';
  try {
    await track.restartTrack({ facingMode: facing, resolution: camRes() });
    V.facing = facing;
    emit('voice:local');
    emit('voice:facing', facing);
  } catch (e) { console.error(e); toast('Couldn’t switch camera'); }
}
export const screenSupported = () => !!navigator.mediaDevices?.getDisplayMedia;
export async function setScreen(on) {
  if (!room || !V.ready) return toast('Voice is still connecting');
  if (!screenSupported()) return toast('Screen sharing works on desktop Telegram');
  try { await room.localParticipant.setScreenShareEnabled(on, { audio: false }); V.screen = on; send({ t: 'screen', on }); emit('voice:local'); }
  catch (e) { console.error(e); V.screen = false; emit('voice:local'); }
}

// ---------------- volume + mute for me ----------------
function applyVolume(p) {
  const uid = uidOf(p);
  const muted = mutedForMe.has(uid);
  try { p.setVolume(muted ? 0 : Math.min(1, (volumes.get(uid) ?? 1) * roomVol)); } catch {}
  // Volume alone isn't enough: iPhones ignore audio element volume. Stop receiving their audio instead.
  p.audioTrackPublications?.forEach(pub => { try { if (pub.isDesired !== false) pub.setEnabled(!muted); } catch {} });
}
export const getVolume = (uid) => volumes.get(uid) ?? 1;
export const isMutedForMe = (uid) => mutedForMe.has(uid);
// a tile's uid -> its LiveKit participant (someone appearing as the group is there under their stand-in id)
const lkPart = (uid) => room?.remoteParticipants.get(String(S.parts.get(uid)?.lk ?? uid));
export function setVolume(uid, v) {
  volumes.set(uid, v);
  store.set('vols', Object.fromEntries(volumes));
  const p = lkPart(uid); if (p) applyVolume(p);
}
export function toggleMuteForMe(uid) {
  mutedForMe.has(uid) ? mutedForMe.delete(uid) : mutedForMe.add(uid);
  store.set('mutedForMe', [...mutedForMe]);
  const p = lkPart(uid); if (p) applyVolume(p);
  return mutedForMe.has(uid);
}
export const getRoomVolume = () => roomVol;
export function setRoomVolume(v) {
  roomVol = v;
  store.set('roomVol', v);
  room?.remoteParticipants.forEach(applyVolume);
}

// ---------------- audio devices (speaker / Bluetooth / mic) ----------------
export async function listDevices(kind) {
  try {
    return (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === kind && d.deviceId);
  } catch { return []; }
}
// device names are hidden until the microphone was allowed once
export async function allowDeviceList() {
  try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach(t => t.stop()); }
  catch { toast('Allow the microphone to see your sound devices'); }
}
export const canPickOutput = () => typeof HTMLMediaElement !== 'undefined' && 'setSinkId' in HTMLMediaElement.prototype;
export const currentDevice = (kind) => store.get('dev:' + kind, '');
export async function useDevice(kind, deviceId) {
  store.set('dev:' + kind, deviceId);
  try { if (room) await room.switchActiveDevice(kind, deviceId); }
  catch (e) { console.warn(e); toast('Couldn’t switch to that device'); return false; }
  if (kind === 'audiooutput') emit('voice:sink', deviceId);
  if (kind === 'audioinput' && V.mic) await applyNoise();
  return true;
}

export async function leaveVoice() {
  stopped = true; clearTimeout(retryTimer);
  const r = room; room = null; V.ready = false;
  try { await r?.disconnect(); } catch {}
}
// join again under another identity (switched to appearing as the group, or back)
export async function restartVoice(lk) {
  const was = V.mic;
  await leaveVoice();
  V.mic = V.cam = V.screen = false;
  if (was) send({ t: 'mic', on: false });
  await startVoice(lk);
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
