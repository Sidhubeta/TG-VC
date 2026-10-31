import { $, CID, S, actionRow, api, avatarEl, closeWS, confirmBox, connectWS, copyText, denied, emit, fmtDate, fmtDur, fullView, h, haptic, icon, off, on, reconnectWS, send, sheet, store, switchRow, tg, toast } from './core.js';
import { getMusicVolume, openNP, openPlayHistory, openSearch, pip, pipSupported, prompt_, refresh as refreshMusic, setMusicVolume, stopMusic, unlockAudio } from './player.js';
import { BOARD, joinSound, leaveSound, micSound, playBoard } from './sounds.js';
import {
  V, attachVideo, canPickOutput, currentDevice, flipCamera, getRoomVolume, getVolume, isMutedForMe, leaveVoice, listDevices,
  allowDeviceList, noiseStatus, screenSupported, setCam, setMic, setNoise, setRoomVolume, setScreen, setVolume, startVoice, toggleMuteForMe,
  restartVoice, unblockAudio, useDevice,
} from './voice.js';
import { recorder } from './recorder.js';
import { startEmoji } from './emoji.js';
startEmoji();

// ======================= boot =======================
function gate(title, text, action) {
  const g = $('#gate');
  g.hidden = false;
  g.querySelector('.box').replaceChildren(
    title === 'loading' ? h('div', { class: 'spinner' }) : h('div', { style: { fontSize: '44px' } }, '🎧'),
    h('h1', null, title === 'loading' ? 'Joining' : title), h('p', null, text), action || '');
}

async function boot() {
  try {
    tg?.ready(); tg?.expand(); goFullscreen();
    tg?.disableVerticalSwipes?.();
    try { tg?.setHeaderColor?.(tg.colorScheme === 'light' ? '#ffffff' : '#000000'); tg?.setBackgroundColor?.(tg.colorScheme === 'light' ? '#ffffff' : '#000000'); } catch {}
    if (tg?.colorScheme === 'light') document.documentElement.classList.add('light');
  } catch {}
  gate('loading', 'Connecting to the voice chat…');
  const qs = new URLSearchParams(location.search);
  let body;
  if (tg?.initData) body = { initData: tg.initData };
  else if (qs.get('dev')) body = { dev: true, uid: Number(qs.get('uid') || 1), name: qs.get('name') || undefined };
  else return gate('Open in Telegram', 'This voice chat only works inside Telegram. Ask in your group for the link (send /vc).');
  let r;
  try {
    const res = await fetch('/api/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    r = await res.json();
    if (!res.ok) { const er = new Error(r.error || 'Could not join'); er.status = res.status; throw er; }
  } catch (e) {
    const final = e.status === 403;
    return gate("Can't join", e.message, final
      ? h('button', { class: 'btn soft', onclick: () => tg?.close?.() }, 'Close')
      : h('button', { class: 'btn soft', onclick: () => location.reload() }, 'Try again'));
  }
  Object.assign(S, { session: r.session, uid: r.uid, chat: r.chat, admin: r.admin, group: r.group, bot: r.bot || '' });
  S.lk = r.livekit;
  connectWS({ asGroup: store.get('asGroup:' + r.chat, false) && r.admin });
  try { tg?.enableClosingConfirmation?.(); } catch {}
}
// Browsers only allow sound after a tap. Instead of a "Tap to listen" button, any tap anywhere starts it.
// Phones count only the end of a tap (touchend / click) as permission for sound, not the moment the finger
// lands; listening to pointerdown alone is why only the "sound off" button itself used to bring the music back.
let lastUnlock = 0;
const unlockAll = () => { if (Date.now() - lastUnlock < 250) return; lastUnlock = Date.now(); unlockAudio(); unblockAudio(); };
['touchend', 'click', 'keydown'].forEach(e => document.addEventListener(e, unlockAll, { capture: true, passive: true }));

// Closing the app = leaving the voice chat (the socket also drops, this just makes it instant)
let byeSent = false;
function bye() {
  if (byeSent || !S.session) return;
  byeSent = true;
  try { navigator.sendBeacon('/api/bye?s=' + encodeURIComponent(S.session) + '&c=' + CID); } catch {}
  leaveVoice();
}
window.addEventListener('pagehide', bye);
window.addEventListener('beforeunload', bye);
// Back from the browser's page cache (the page was kept, not reloaded): we had said goodbye, so join again.
window.addEventListener('pageshow', (e) => {
  if (!e.persisted || !byeSent) return;
  byeSent = false;
  reconnectWS();
  restartVoice(S.lk);
});

function goFullscreen() {
  try {
    if (tg?.isVersionAtLeast?.('8.0') && !tg.isFullscreen) tg.requestFullscreen();
  } catch {}
}

let started = false;
on('ws:init', (m) => {
  S.admin = m.admin; S.sudo = !!m.sudo; S.owner = !!m.owner; S.room = m.room; S.group = m.group;
  // appearing as the group: members know us by a stand-in id, and the voice uses it too
  S.pid = m.pid || null;
  // the voice identity changed (switched to appearing as the group, or back): join the voice again under it
  const lkChanged = !!m.livekit && m.lkid !== (S.lkid ?? S.uid);
  if (m.livekit) { S.lk = m.livekit; S.lkid = m.lkid; }
  S.parts = new Map(m.parts.map(p => [p.uid, p]));
  // The server decides what we may read back: since we joined (admins: 48 hours). A quick reconnect keeps it;
  // leaving and joining again starts over.
  S.msgs = [...m.chat].sort((a, b) => a.id - b.id);
  if (historySheet) { historySheet.close(); historySheet = null; }
  $('#gate').hidden = true;
  $('#room').hidden = false;
  if (!started) {
    started = true;
    startVoice(S.lk);
    bindDock();
  } else if (lkChanged) {
    restartVoice(S.lk);
  } else {
    // Reconnected after a longer drop: the room has us back with the mic off, but we may still be talking.
    // Tell it what we're really doing (before, we saw "You're live" and everyone else saw us muted).
    if (V.mic) send({ t: 'mic', on: true });
    if (V.cam) send({ t: 'video', on: true });
    if (V.screen) send({ t: 'screen', on: true });
  }
  renderHeader(); renderGrid(true); renderDock();
});
on('fatal', ({ title, text }) => { leaveVoice(); stopMusic(); $('#room').hidden = true; gate(title, text, h('button', { class: 'btn soft', onclick: () => tg?.close?.() }, 'Close')); });
on('ws:down', () => toast('Reconnecting…', 1500));
on('ws:kicked', (m) => { closeWS(); emit('fatal', { title: m.ban ? 'Banned' : 'Removed', text: m.reason || (m.ban ? 'An admin banned you from this voice chat.' : 'An admin removed you from this voice chat.') }); });
on('ws:locked', () => emit('fatal', { title: 'Locked', text: 'Admins locked this voice chat.' }));
on('ws:toast', (m) => toast(m.text));
// the room refused our mic (an admin muted us while we were away): it's off, whatever we thought
on('ws:mic.denied', () => {
  if (V.mic) setMic(false);
  toast(S.parts.get(S.uid)?.forceMuted ? 'An admin muted you — raise your hand to speak' : 'You can’t speak right now — raise your hand');
  renderDock();
});
on('ws:denied', (m) => denied(m.text)); // something you're not allowed to do: said in red

// ======================= header =======================
function renderHeader() {
  const s = S.room.settings || {};
  $('#r-title').textContent = s.title || S.group || 'Voice Chat';
  const n = S.parts.size;
  $('#r-count').textContent = `${n} ${n === 1 ? 'participant' : 'participants'}`;
  $('#r-rec').hidden = !S.room.rec?.on;
  $('#r-lock').hidden = !s.locked;
  const d = $('#r-desc'); d.hidden = !s.description; d.textContent = s.description || '';
}
on('ws:room', (m) => { S.room = m.room; renderHeader(); renderDock(); refreshMusic(); emit('room:changed'); });
$('#b-search').onclick = () => openSearch('home');
$('#b-admin').onclick = () => openMore();

// ======================= participants =======================
const tiles = new Map();
const inviteRow = h('button', { class: 'prow invite-row', onclick: () => openInvite() },
  h('div', { class: 'pav' }, icon('user-plus')), h('div', { class: 'pmeta' }, h('div', { class: 'pname' }, 'Invite members')));
function sortParts() {
  return [...S.parts.values()].sort((a, b) =>
    (b.uid === S.uid) - (a.uid === S.uid) || (!!b.hand - !!a.hand) || (b.mic - a.mic) || (b.canSpeak - a.canSpeak) || (a.joined - b.joined));
}
function statusOf(p) {
  if (netOf(p.uid) === 'poor' || netOf(p.uid) === 'lost') return ['weak internet — may lag', 'warn'];
  if (p.hand) return ['wants to speak', 'blue'];
  if (p.forceMuted) return ['muted by admin', 'red'];
  if (isMutedForMe(p.uid)) return ['muted for you', 'red'];
  if (p.bio) return [p.bio, ''];
  if (p.mic && p.canSpeak) return ['unmuted', 'blue'];
  return ['listening', ''];
}
function tileFor(p) {
  let t = tiles.get(p.uid);
  if (!t) {
    t = h('button', { class: 'prow', onclick: () => openParticipant(p.uid) });
    t._new = true;
    tiles.set(p.uid, t);
  }
  const live = p.mic && p.canSpeak;
  const speaking = V.speaking.has(p.uid) && p.mic;
  const net = netOf(p.uid);
  const weak = net === 'poor' || net === 'lost';
  t.className = 'prow' + (t._new ? ' new' : '') + (live ? ' live' : '') + (p.forceMuted ? ' forced' : '') + (speaking ? ' speaking' : '');
  t._new = false;
  const key = `${p.name}|${p.photo}`;
  if (t._key !== key) { t._key = key; t._av = avatarEl(p); }
  const [st, cls] = statusOf(p);
  // rebuilding a row costs layout + emoji scanning; only do it when something visible changed
  const bad = badgesOf(p.uid);
  const sig = [st, cls, live, p.hand, p.admin, p.mod, p.asGroup, p.flags?.length, S.admin, t._key, weak, bad.map(b => b.id).join()].join('|');
  if (t._sig === sig) return t;
  t._sig = sig;
  t.replaceChildren(
    h('div', { class: 'pav' }, t._av, p.hand ? h('span', { class: 'hand' }, '✋') : null),
    h('div', { class: 'pmeta' },
      h('div', { class: 'pname' }, h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, p.name), p.uid === S.uid ? h('span', { class: 'you' }, '(you)') : null,
        p.admin && !p.asGroup ? h('span', { class: 'tag' }, 'admin') : null,
        p.mod ? h('span', { class: 'tag mod' }, 'mod') : null,
        bad.length && !p.asGroup ? h('span', { class: 'pbadges', title: bad.map(b => b.name).join(', ') }, bad.slice(0, 2).map(b => b.e).join('')) : null,
        S.admin && p.flags?.length ? h('span', { class: 'tag warn', title: p.flags.join('\n') }, 'check') : null),
      h('div', { class: 'psub ' + cls }, st)),
    weak ? h('span', { class: 'pnet', title: 'Weak internet' }, icon('signal')) : null,
    h('span', { class: 'pmic' }, icon(live ? 'mic' : 'mic-off')));
  t.setAttribute('aria-label', `${p.name}, ${st}`);
  return t;
}
function renderGrid(full = false) {
  const g = $('#grid');
  const list = sortParts();
  const want = [inviteRow, ...list.map(tileFor)];
  if (full || g.children.length !== want.length || want.some((el, i) => g.children[i] !== el)) g.replaceChildren(...want);
  const lh = $('#lh-count'); if (lh) lh.textContent = `${list.length} ${list.length === 1 ? 'participant' : 'participants'}`;
}
on('ws:p.join', (m) => {
  S.parts.set(m.p.uid, m.p); renderGrid(); renderHeader();
  if (!m.quiet && !document.hidden && S.room.settings?.join_sound !== false && store.get('joinSound', true)) joinSound();
});
on('ws:p.leave', (m) => {
  const t = tiles.get(m.uid);
  S.parts.delete(m.uid);
  if (t) { t.classList.add('out'); setTimeout(() => { tiles.delete(m.uid); renderGrid(); }, 280); }
  renderHeader();
  removeVideo(m.uid);
  if (!m.quiet && !document.hidden && S.room.settings?.join_sound !== false && store.get('joinSound', true)) leaveSound();
});
on('ws:p.upd', (m) => {
  const prev = S.parts.get(m.p.uid);
  S.parts.set(m.p.uid, m.p);
  if (m.p.uid === S.uid) {
    if (prev?.canSpeak && !m.p.canSpeak && V.mic) { setMic(false); }
    if (!m.p.canSpeak && (V.cam || V.screen)) { if (V.cam) setCam(false); if (V.screen) setScreen(false); }
    renderDock();
  }
  renderGrid();
});
on('ws:hand', (m) => { if ((S.admin || isMod()) && m.uid !== S.uid) toast(`✋ ${m.name} wants to speak`); });
on('ws:alert', (m) => { if (S.admin || isMod()) { toast(`⚠️ ${m.name}: ${m.flags[0]}`, 4000); bumpAdminBadge(); } });
on('voice:speaking', () => { for (const p of S.parts.values()) tileFor(p); renderDock(); });
// ---- connection quality: everyone sees who has weak internet; you get a banner for your own ----
const net = new Map();
const netOf = (uid) => net.get(uid);
on('voice:quality', ({ uid, q }) => {
  const was = net.get(uid);
  net.set(uid, q);
  const p = S.parts.get(uid); if (p) tileFor(p);
  if (uid === S.uid) {
    const w = $('#net-warn'), bad = q === 'poor' || q === 'lost';
    w.hidden = !bad;
    if (bad) w.textContent = q === 'lost' ? '📶 Your internet dropped — reconnecting…' : '📶 Your internet is weak, so voice and music may lag';
    if (bad && was !== q) haptic('warning');
  }
});

// ======================= badges =======================
// Earned in this voice chat (Top DJ, Night owl, Regular…). Shown next to names and in profiles; a new one of your
// own pops up once.
let BADGES = {}, ALL_BADGES = [];
const badgesOf = (uid) => (S.parts.get(uid)?.asGroup ? [] : BADGES[String(uid)] || []);
async function loadBadges() {
  try {
    const r = await api('/api/badges');
    BADGES = r.badges; ALL_BADGES = r.all;
    renderGrid(true);
    const mine = badgesOf(S.uid), key = 'badges:' + S.chat, seen = store.get(key, null);
    const fresh = seen ? mine.filter(b => !seen.includes(b.id)) : [];
    store.set(key, mine.map(b => b.id));
    if (fresh.length) { toast(`🎉 New badge: ${fresh[0].e} ${fresh[0].name} — ${fresh[0].why}`, 5000); haptic('success'); }
  } catch {}
}
on('ws:init', loadBadges);
setInterval(() => { if (!document.hidden && S.session) loadBadges(); }, 5 * 60e3);
function badgeSheet(uid) {
  const own = badgesOf(uid).map(b => b.id), p = S.parts.get(uid);
  const body = h('div', null,
    h('p', { class: 'muted note' }, uid === S.uid ? 'Badges you can earn in this voice chat:' : `${p?.name || 'They'} ${own.length ? 'earned:' : 'haven’t earned a badge yet.'}`),
    h('div', { class: 'list' }, ...ALL_BADGES.filter(b => uid === S.uid || own.includes(b.id)).map(b =>
      h('div', { class: 'row badge-row' + (own.includes(b.id) ? ' got' : '') }, h('span', { class: 'bdg' }, b.e),
        h('span', { class: 'lab' }, b.name, h('small', null, b.why)), own.includes(b.id) ? icon('check') : null))));
  sheet({ title: 'Badges', body });
}

// moderators (made by admins): can mute, move and remove people who aren't admins or moderators
const isMod = () => !!S.parts.get(S.uid)?.mod;
// participant action sheet
function openParticipant(uid) {
  const p = S.parts.get(uid); if (!p) return;
  haptic('light');
  const me = uid === S.uid;
  const body = h('div');
  const s = sheet({ title: '', body });
  const render = () => {
    const p = S.parts.get(uid); if (!p) return s.close();
    const vol = h('input', { type: 'range', min: 0, max: 1, step: 0.05, value: getVolume(uid), 'aria-label': 'Volume', style: { color: 'var(--accent)', flex: 1 } });
    const paint = () => vol.style.setProperty('--v', (vol.value * 100) + '%'); paint();
    vol.oninput = () => { setVolume(uid, Number(vol.value)); paint(); };
    body.replaceChildren(
      h('div', { class: 'ph' }, h('div', { class: 'av' }, avatarEl(p)), h('h3', null, p.name),
        h('p', null, [p.username ? '@' + p.username : null, p.realName ? `real: ${p.realName}` : null, p.admin ? 'admin' : null, p.mod ? 'moderator' : null,
          S.room.settings?.music_dj?.[String(p.uid)] ? 'music DJ' : null].filter(Boolean).join('  ·  ')),
        p.bio ? h('div', { class: 'bio' }, p.bio) : null,
        badgesOf(uid).length || me ? h('button', { class: 'badge-chips', onclick: () => badgeSheet(uid) },
          ...badgesOf(uid).map(b => h('span', { class: 'chip' }, b.e, ' ', b.name)), me && !badgesOf(uid).length ? h('span', { class: 'chip ghost' }, '🏅 See badges you can earn') : null) : null,
        h('p', { style: { marginTop: '6px' } }, p.forceMuted ? 'muted by admin' : p.hand ? 'wants to speak' : (p.mic && p.canSpeak) ? 'unmuted' : 'listening')),
      (S.admin || isMod()) && p.flags?.length ? h('div', null, ...p.flags.map(f => h('div', { class: 'flag' }, '⚠️', h('span', null, f)))) : null,
      !me ? h('div', { class: 'list' },
        h('div', { class: 'row' }, icon('vol-low'), vol, icon('vol')),
        actionRow(isMutedForMe(uid) ? 'vol' : 'vol-x', isMutedForMe(uid) ? 'Unmute for me' : 'Mute for me', () => { const m = toggleMuteForMe(uid); toast(m ? `${p.name} is muted for you` : `${p.name} unmuted for you`); renderGrid(); render(); }, '', 'Only you stop hearing them')) : null,
      // admins, and moderators for people who aren't admins or moderators themselves
      (S.admin || (isMod() && !p.mod)) && !me && !p.admin ? h('div', { class: 'list' },
        p.forceMuted
          ? actionRow('mic', 'Allow to speak', () => { send({ t: 'admin.unmute', uid }); s.close(); })
          : actionRow('mic-off', 'Mute for everyone', () => { send({ t: 'admin.mute', uid }); s.close(); }, '', 'They can’t unmute until you allow it'),
        p.canSpeak && !p.forceMuted
          ? actionRow('headphones', 'Make listener', () => { send({ t: 'admin.role', uid, role: 'listener' }); s.close(); })
          : !p.forceMuted ? actionRow('hand', p.hand ? 'Accept hand — let them speak' : 'Make speaker', () => { send({ t: 'admin.role', uid, role: 'speaker' }); s.close(); }) : null,
        p.flags?.length ? actionRow('check', 'Clear warnings', () => { send({ t: 'admin.clearflags', uid }); s.close(); }) : null,
        actionRow('user-x', 'Remove from voice chat', async () => { if (await confirmBox(`Remove ${p.name}? They can rejoin.`, 'Remove', true)) { send({ t: 'admin.kick', uid }); s.close(); } }, 'red'),
        S.admin ? actionRow('ban', 'Ban', async () => { if (await confirmBox(`Ban ${p.name}? They won't be able to join again until you unban them.`, 'Ban', true)) { send({ t: 'admin.ban', uid }); s.close(); } }, 'red') : null) : null,
      S.admin && !me && !p.admin ? h('div', { class: 'list' },
        p.mod ? actionRow('shield', 'Remove as moderator', () => { send({ t: 'admin.unmod', uid }); toast(`${p.name} is no longer a moderator`); s.close(); })
          : actionRow('shield', 'Make moderator', () => { send({ t: 'admin.mod', uid }); toast(`${p.name} is now a moderator`); s.close(); }, '', 'Can mute, move and remove people')) : null,
      !me ? h('div', { class: 'list' }, actionRow('flag', 'Report to admins', async () => {
        const reason = await prompt_('Report ' + p.name, 'What happened? (optional)');
        send({ t: 'report', uid, reason }); s.close();
      }, 'red')) : null,
      me ? h('div', { class: 'list' }, actionRow('hand', p.hand ? 'Lower hand' : 'Raise hand', () => { send({ t: 'hand', up: !p.hand }); s.close(); })) : null);
  };
  render();
}

// ======================= video stage =======================
const vids = new Map();
on('voice:video', ({ uid, track, source, on: isOn, local }) => {
  const key = uid + ':' + source;
  if (!isOn) return removeVideo(uid, key);
  const old = vids.get(key);
  if (old?.track === track) return;
  if (old) removeVideo(uid, key);
  const p = S.parts.get(uid);
  const screen = source === 'screen_share';
  const el = attachVideo(track);
  const label = (p?.name || '') + (screen ? ' · screen' : '');
  const tile = h('div', { class: 'vtile' + (screen ? ' screen' : '') }, el,
    h('span', { class: 'lbl' }, label),
    h('button', { class: 'fs', 'aria-label': 'Full screen', onclick: () => fullView(el, { label }) }, icon('expand')),
    pipSupported(el) ? h('button', { class: 'fs pip', 'aria-label': 'Picture in picture', onclick: () => pip(el) }, icon('pip')) : null);
  vids.set(key, { tile, uid, track, el, local: local && !screen });
  if (local && !screen) el.classList.toggle('mirror', V.facing === 'user'); // only the selfie camera is mirrored
  $('#stage').append(tile);
});
on('voice:facing', () => { for (const v of vids.values()) if (v.local) v.el.classList.toggle('mirror', V.facing === 'user'); });
function removeVideo(uid, key) {
  for (const [k, v] of vids) if ((key ? k === key : v.uid === uid)) { v.tile.remove(); try { v.track?.detach?.(v.el); } catch {} vids.delete(k); }
}

// ======================= dock =======================
function me() { return S.parts.get(S.uid); }
function renderDock() {
  const p = me(); if (!p) return;
  const mic = $('#b-mic'), lbl = $('#mic-lbl');
  mic.className = 'mic';
  let ic = 'mic-off', text = 'Unmute';
  if (S.voiceState !== 'ready') { mic.classList.add('wait'); text = S.voiceState === 'off' ? 'Voice off' : 'Connecting…'; }
  else if (p.forceMuted) { mic.classList.add('forced'); text = 'Muted by admin'; }
  else if (!p.canSpeak) { mic.classList.add('hand'); if (p.hand) mic.classList.add('raised'); ic = 'hand'; text = p.hand ? 'Hand raised' : 'Raise hand'; }
  else if (V.mic) { mic.classList.add('on'); ic = 'mic'; text = 'You’re Live'; }
  const sig = mic.className + ic + text + V.cam;
  if (mic._sig === sig) return; // called on every speaker change: don't rebuild (it restarted the animation)
  mic._sig = sig;
  mic.replaceChildren(h('span', { class: 'b2' }), h('span', { class: 'b1' }), h('span', { class: 'blob' }), icon(ic));
  lbl.textContent = text;
  mic.setAttribute('aria-label', text);
  const cam = $('#b-cam');
  cam.classList.toggle('on', V.cam);
  cam.querySelector('.c').replaceChildren(icon(V.cam ? 'video' : 'video-off'));
}
on('voice', ({ state }) => { S.voiceState = state; renderDock(); });
on('voice:local', renderDock);

let micBusy = false;
function bindDock() {
  $('#b-mic').onclick = async () => {
    const p = me(); if (!p) return;
    if (S.voiceState !== 'ready') return toast(S.voiceState === 'off' ? 'Voice isn’t set up on the server yet' : 'Voice is still connecting');
    if (p.forceMuted) return denied('An admin muted you — only admins can let you speak');
    if (!p.canSpeak) { haptic('medium'); send({ t: 'hand', up: !p.hand }); if (!p.hand) toast('Hand raised — admins will let you speak'); return; }
    if (micBusy) return; // a second quick tap while the first one is still switching the mic
    haptic('medium');
    const want = !V.mic, btn = $('#b-mic').parentElement;
    micBusy = true; btn.classList.add('busy'); // feedback at once; turning the mic on can take a moment
    try { if (await setMic(want)) { micSound(want); } } finally { micBusy = false; btn.classList.remove('busy'); renderDock(); }
  };
  $('#b-cam').onclick = openCamera;
  $('#b-speaker').onclick = openAudio;
  $('#b-chat').onclick = () => openChat();
  $('#b-leave').onclick = async () => {
    if (!(await confirmBox('Leave the voice chat?', 'Leave', true))) return;
    if (V.mic) await setMic(false);
    await leaveVoice(); closeWS(); stopMusic();
    try { tg?.disableClosingConfirmation?.(); tg?.close(); } catch {}
    gate('You left', 'Open the link in your group again to rejoin.');
    $('#room').hidden = true;
  };
}

// ---- camera: pick front or back first, then switch or turn off ----
function openCamera() {
  const p = me();
  if (!p?.canSpeak) return denied('Only speakers can turn on the camera — raise your hand first');
  haptic('light');
  const body = V.cam
    ? h('div', { class: 'list' },
      actionRow('flip', V.facing === 'user' ? 'Switch to back camera' : 'Switch to front camera', () => { s.close(); flipCamera(); }),
      actionRow('video-off', 'Turn off camera', () => { s.close(); setCam(false); }, 'red'))
    : h('div', { class: 'list' },
      actionRow('video', 'Front camera', () => { s.close(); setCam(true, 'user'); }, '', 'Selfie camera'),
      actionRow('flip', 'Back camera', () => { s.close(); setCam(true, 'environment'); }));
  const s = sheet({ title: V.cam ? 'Camera' : 'Turn on camera', body });
}

// ---- audio: where the sound plays (speaker / ear speaker / Bluetooth / earphones), volumes, noise isolation ----
// On Android, Chrome lists the phone's call routes as microphones ("Speakerphone", "Headset earpiece",
// "Bluetooth headset", "Wired headset"…). Picking one switches the whole call to that route, like the
// speaker button in a normal call. iPhones don't expose this to apps in Telegram: there, Control Center does it.
const ROUTES = [
  [/speakerphone|speaker phone|loudspeaker/i, 'Phone speaker', 'Handsfree, loud', 'speaker'],
  [/earpiece|receiver|ear speaker/i, 'Ear speaker', 'Hold the phone to your ear', 'phone'],
  [/bluetooth|airpods|buds|\bbt\b/i, 'Bluetooth', null, 'bt'],
  [/usb/i, 'USB headset', null, 'headphones'],
  [/wired|headphone|headset|earphone/i, 'Earphones', 'Wired earphones / headset', 'headphones'],
];
function routeOf(d) {
  if (d.deviceId === 'default') return ['Automatic', 'Phone picks the best one', 'sparkle'];
  if (d.deviceId === 'communications') return null;
  for (const [re, name, sub, ic] of ROUTES) if (re.test(d.label)) return [name, sub, ic];
  return null;
}
const isApplePhone = /iPhone|iPad|iPod/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && 'ontouchend' in document);
function volRow(ic, label, value, set) {
  const r = h('input', { type: 'range', min: 0, max: 1, step: 0.02, value, 'aria-label': label, style: { color: 'var(--accent)', flex: 1 } });
  const paint = () => r.style.setProperty('--v', (r.value * 100) + '%');
  r.oninput = () => { set(Number(r.value)); paint(); };
  paint();
  return h('div', { class: 'row' }, icon(ic), h('span', { style: { width: '58px', fontSize: '14px' } }, label), r);
}
function openAudio() {
  haptic('light');
  const body = h('div');
  const choice = (kind, d, [name, sub, ic], curId) => h('button', {
    class: 'row route' + (d.deviceId === curId ? ' on' : ''),
    onclick: async () => { haptic('select'); if (await useDevice(kind, d.deviceId)) { toast(`Sound: ${name}`); render(); } },
  }, icon(ic), h('span', { class: 'lab' }, name, sub ? h('small', null, sub) : null), d.deviceId === curId ? icon('check') : null);
  const render = async () => {
    const [ins, outs] = await Promise.all([listDevices('audioinput'), canPickOutput() ? listDevices('audiooutput') : []]);
    const named = ins.some(d => d.label);
    const routes = ins.map(d => [d, routeOf(d)]).filter(([, r]) => r);
    const hasRoutes = routes.some(([d]) => d.deviceId !== 'default');
    const curIn = currentDevice('audioinput') || 'default', curOut = currentDevice('audiooutput') || 'default';
    let speaker;
    if (hasRoutes) {
      speaker = h('div', null, h('div', { class: 'list' }, ...routes.map(([d, r]) => choice('audioinput', d, r, curIn))),
        !V.mic ? h('p', { class: 'muted note' }, 'Ear speaker and Bluetooth calling switch fully once your mic is on.') : null);
    } else if (outs.length > 1) {
      speaker = h('div', { class: 'list' }, ...outs.map((d, i) => choice('audiooutput', d, [d.deviceId === 'default' ? 'Automatic' : d.label || `Speaker ${i + 1}`, null, /bluetooth|airpods|buds/i.test(d.label) ? 'bt' : 'speaker'], curOut)));
    } else if (!named && !isApplePhone) {
      speaker = h('div', null, h('p', { class: 'muted note' }, 'Allow the microphone once so your phone can list its speakers, earpiece and Bluetooth devices.'),
        h('button', { class: 'btn sm block', onclick: async () => { await allowDeviceList(); render(); } }, 'Show sound devices'));
    } else {
      speaker = h('p', { class: 'muted note' }, isApplePhone
        ? 'On iPhone, choose the iPhone speaker, AirPods or Bluetooth from Control Center (long-press the music card, then the AirPlay icon).'
        : 'Your phone picks the speaker itself: connect Bluetooth or earphones and the sound moves there.');
    }
    const mics = ins.filter(d => !routeOf(d) && d.deviceId !== 'communications');
    body.replaceChildren(
      h('div', { class: 'grp-h' }, 'Play sound through'),
      speaker,
      mics.length > 1 ? h('div', { class: 'grp-h' }, 'Microphone') : null,
      mics.length > 1 ? h('div', { class: 'list' }, ...mics.map((d, i) => choice('audioinput', d, [d.label || `Microphone ${i + 1}`, null, 'mic'], curIn))) : null,
      h('div', { class: 'grp-h' }, 'Volume'),
      h('div', { class: 'list' },
        volRow('group', 'Voices', getRoomVolume(), setRoomVolume),
        volRow('music', 'Music', getMusicVolume(), (v) => { unlockAudio(); setMusicVolume(v); })),
      h('p', { class: 'muted note' }, 'During a call the volume buttons change the call volume, which Android never lets go fully silent. Use these sliders to make voices or music quieter.'),
      h('div', { class: 'list' },
        switchRow('Noise isolation', noiseStatus(), V.noise, (v) => { setNoise(v).then(render); }),
        switchRow('Join & leave sounds', null, store.get('joinSound', true), v => store.set('joinSound', v))));
  };
  const onDev = () => render();
  on('voice:devices', onDev);
  on('voice:noise', onDev);
  sheet({ title: 'Audio', body, onClose: () => { off('voice:devices', onDev); off('voice:noise', onDev); } });
  render();
}

// ======================= more =======================
function openMore() {
  const p = me();
  const body = h('div',
    S.admin ? h('div', { class: 'list' }, actionRow('shield', 'Manage voice chat', () => { s.close(); openAdmin(); }, 'acc',
      adminAlerts ? `${adminAlerts} new alert${adminAlerts > 1 ? 's' : ''}` : 'Settings, logs, safety, recording')) : null,
    // a sudo user who isn't an admin of this group: just the Health page
    S.sudo && !S.admin ? h('div', { class: 'list' }, actionRow('shield', 'Health', () => {
      s.close();
      const body = h('div');
      let stop = null;
      sheet({ title: 'Health', body, tall: true, onClose: () => stop?.() });
      stop = adminHealth(body);
    }, 'acc', 'Is everything working?')) : null,
    h('div', { class: 'list' },
      p && !p.canSpeak ? actionRow('hand', p.hand ? 'Lower hand' : 'Raise hand', () => { send({ t: 'hand', up: !p.hand }); s.close(); }) : null,
      actionRow('user-plus', 'Invite members', () => { s.close(); openInvite(); }),
      actionRow('music', 'Music player', () => { s.close(); openNP(); }),
      actionRow('search', 'Browse music', () => { s.close(); openSearch('home'); }),
      actionRow('history', 'Play history', () => { s.close(); openPlayHistory(); }, '', 'What played and who added it'),
      actionRow('history', 'Recent messages', () => { s.close(); openHistory(); }, '', S.admin ? 'Last 48 hours' : 'Since you joined'),
      S.room.settings?.soundboard ? actionRow('sound', 'Soundboard', () => { s.close(); openBoard(); }) : null,
      screenSupported() && p?.canSpeak ? actionRow('screen', V.screen ? 'Stop sharing screen' : 'Share screen', () => { s.close(); setScreen(!V.screen); }) : null),
    h('div', { class: 'list' },
      S.admin ? switchRow(`Appear as ${S.group}`, 'Others see the group name instead of you', store.get('asGroup:' + S.chat, false), v => {
        store.set('asGroup:' + S.chat, v); toast('Rejoining…'); reconnectWS({ asGroup: v }); s.close();
      }) : null));
  const s = sheet({ title: 'More', body });
}
function openBoard() {
  const body = h('div', h('div', { class: 'sb' }, ...BOARD.map(([id, e, label]) =>
    h('button', { onclick: () => { haptic('light'); send({ t: 'sound', id }); } }, h('span', null, e), label))),
    h('p', { class: 'muted', style: { fontSize: '13px' } }, 'Everyone in the room hears these.'));
  sheet({ title: 'Soundboard', body });
}
on('ws:sound', (m) => {
  const b = BOARD.find(x => x[0] === m.id); if (!b) return;
  playBoard(m.id);
  const pop = h('div', { class: 'sound-pop' }, b[1], h('small', null, m.name));
  document.body.append(pop); setTimeout(() => pop.remove(), 1500);
});

// ======================= invite =======================
async function openInvite() {
  haptic('light');
  const search = h('input', { type: 'search', placeholder: 'Search members', 'aria-label': 'Search members' });
  const note = h('p', { class: 'muted note' });
  const list = h('div', { class: 'list' });
  const foot = h('div', { class: 'inv-foot' });
  const body = h('div', null, h('div', { class: 'searchbar', style: { margin: '4px 0 6px' } }, icon('search'), search), note, list, foot);
  const sh = sheet({ title: 'Invite to voice chat', body, tall: true, onClose: () => io?.disconnect() });
  const sent = new Set();
  // the list comes in pages: more people load by themselves as you scroll down
  let q = '', next = 0, more = true, busy = false, gen = 0, io = null, shown = 0;
  const row = (p) => {
    const btn = h('button', { class: 'btn sm' + (sent.has(p.uid) ? ' soft' : ''), disabled: sent.has(p.uid) }, sent.has(p.uid) ? 'Invited' : 'Invite');
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '…';
      try {
        await api('/api/invite', { body: { uid: p.uid } });
        sent.add(p.uid); haptic('success');
        toast(`${p.name} was invited in the group`, 3000);
        btn.textContent = 'Invited'; btn.classList.add('soft');
      } catch (e) { btn.disabled = false; btn.textContent = 'Invite'; toast(e.message, 3600, 'err'); }
    };
    return h('div', { class: 'row' }, h('div', { class: 'ia' }, avatarEl(p)),
      h('span', { class: 'lab' }, p.name, h('small', null, [p.admin ? 'admin' : null, p.username ? '@' + p.username : null].filter(Boolean).join(' · '))), btn);
  };
  const load = async () => {
    if (busy || !more) return;
    busy = true;
    const my = gen;
    foot.replaceChildren(h('div', { class: 'spinner', style: { margin: '14px auto' } }));
    try {
      const r = await api(`/api/invite?q=${encodeURIComponent(q)}&offset=${next}&limit=40`);
      if (my !== gen) return; // a newer search started meanwhile
      list.append(...r.people.map(row));
      shown += r.people.length;
      more = r.more; next = r.next;
      note.textContent = r.only_admins ? 'This group hides its member list, so you can invite its admins.'
        : r.source === 'full' ? (r.total != null ? `${r.total} members` : '')
        : 'Admins and the members the bot has seen in the group.';
      if (r.hint) note.append(h('span', { class: 'inv-hint' }, r.privacy
        ? ' To list more people: in @BotFather send /setprivacy → choose the bot → Disable. To list everyone, set TG_API_ID and TG_API_HASH (my.telegram.org).'
        : ' To list everyone in the group, set TG_API_ID and TG_API_HASH (from my.telegram.org) on the server.'));
      foot.replaceChildren(!shown ? h('div', { class: 'empty-note' }, q ? 'Nobody found.' : 'Nobody to invite right now.') : '');
      if (more && sh.body.scrollHeight <= sh.body.clientHeight + 40) { busy = false; return load(); } // fill the screen
    } catch (e) {
      if (my === gen) foot.replaceChildren(h('div', { class: 'empty-note' }, e.message, ' ', h('button', { class: 'btn sm soft', onclick: () => { busy = false; load(); } }, 'Retry')));
    } finally { if (my === gen) busy = false; }
  };
  const restart = () => { gen++; busy = false; more = true; next = 0; shown = 0; list.replaceChildren(); load(); };
  let tmr = null;
  search.oninput = () => { clearTimeout(tmr); tmr = setTimeout(() => { q = search.value.trim().replace(/^@/, ''); restart(); }, 300); };
  io = new IntersectionObserver((es) => { if (es.some(e => e.isIntersecting)) load(); }, { root: sh.body, rootMargin: '300px' });
  io.observe(foot);
  restart();
}

// ======================= chat (like Telegram's voice chat) =======================
// Tap Message: a message box slides up over the room (no separate screen). Messages pop up for a few seconds
// and disappear. Tap a message to reply. Everyone can read back messages since they joined; admins, 48 hours.
const REACTS = ['👍', '❤️', '😂', '🔥', '😮', '😢', '👏', '🎉'];
const FLY = ['👍', '❤️', '🔥', '😂', '😍', '🤩', '👏', '🎉', '😮', '😢', '🙏', '💯', '🥳', '😎', '🤣', '💔'];
const nameColor = (uid) => `hsl(${(Math.abs(uid) * 47) % 360} 70% 62%)`;
let historySheet = null, replyTo = null, composerBound = false, swallowClick = false;

function openChat(reply = null) {
  const box = $('#composer'), input = $('#cmp-input');
  if (!composerBound) bindComposer();
  if (!box.hidden && !reply) return closeComposer();
  box.hidden = false;
  document.body.classList.add('composing');
  $('#cmp-history').hidden = false;
  setReply(reply);
  input.focus();
  haptic('light');
}
function closeComposer() {
  const box = $('#composer');
  box.hidden = true;
  $('#cmp-react').hidden = true;
  $('#cmp-emoji').classList.remove('on');
  document.body.classList.remove('composing');
  setReply(null);
  $('#cmp-input').blur();
}
function bindComposer() {
  composerBound = true;
  const box = $('#composer'), input = $('#cmp-input'), picker = $('#cmp-react');
  const go = () => {
    const text = input.value.trim(); if (!text) return;
    send({ t: 'chat', text, reply: replyTo?.id || null });
    input.value = ''; setReply(null); haptic('light');
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  $('#cmp-send').onclick = () => { go(); input.focus(); };
  $('#cmp-history').onclick = () => { closeComposer(); openHistory(); };
  picker.replaceChildren(...FLY.map(e => h('button', { 'aria-label': 'React ' + e, onclick: () => { haptic('light'); send({ t: 'fly', emoji: e }); } }, e)));
  $('#cmp-emoji').onclick = () => { picker.hidden = !picker.hidden; $('#cmp-emoji').classList.toggle('on', !picker.hidden); };
  // tap anywhere else (not the box, a popup or the Message button) closes it, like Telegram
  document.addEventListener('pointerdown', (e) => {
    if (box.hidden || box.contains(e.target) || e.target.closest('#b-chat, .cpop')) return;
    if (input.value.trim()) return;
    closeComposer();
    swallowClick = true; // this tap only closes the box; it shouldn't also open whatever is underneath
    setTimeout(() => { swallowClick = false; }, 500);
  }, true);
  document.addEventListener('click', (e) => { if (swallowClick) { swallowClick = false; e.stopPropagation(); e.preventDefault(); } }, true);
}
function setReply(m) {
  replyTo = m;
  const bar = $('#cmp-reply');
  bar.hidden = !m;
  if (m) bar.replaceChildren(h('div', { class: 'rb' }, h('b', null, 'Reply to ' + m.name), h('span', null, m.text)),
    h('button', { class: 'close-x', 'aria-label': 'Cancel reply', onclick: () => setReply(null) }, icon('close')));
}

function showPop(m) {
  if (document.hidden) return;
  const box = $('#chat-pop');
  const el = h('button', { class: 'cpop', onclick: () => { el.remove(); openChat(m); } },
    avatarEl({ uid: m.uid, name: m.name, photo: m.photo }, 'ma'),
    h('span', { class: 'cbub' }, h('b', { style: { color: nameColor(m.uid) } }, m.name),
      m.reply ? h('i', { class: 'crq' }, '↩ ' + m.reply.name) : null, h('span', null, m.text)));
  box.append(el);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 400); }, 12000);
}
on('ws:fly', (m) => {
  const layer = $('#fly-layer');
  if (layer.children.length > 20 || document.hidden) return;
  const el = h('div', { class: 'fly', style: { left: `${10 + Math.random() * 70}%`, '--dx': `${Math.round(Math.random() * 60 - 30)}px` } },
    h('span', null, m.emoji), h('small', null, m.name));
  layer.append(el);
  setTimeout(() => el.remove(), 3200);
});

// ---- recent messages (since you joined; admins: last 48 hours) ----
function msgEl(m) {
  const mine = m.uid === S.uid;
  return h('div', { class: 'msg' + (mine ? ' mine' : ''), 'data-id': m.id, onclick: () => msgMenu(m) },
    mine ? null : avatarEl({ uid: m.uid, name: m.name, photo: m.photo }, 'ma'),
    h('div', { class: 'bub' },
      mine ? null : h('div', { class: 'who', style: { color: nameColor(m.uid) } }, m.name),
      m.reply ? h('div', { class: 'rq' }, h('b', null, m.reply.name), h('span', null, m.reply.text)) : null,
      h('span', { class: 'txt' }, m.text),
      h('span', { class: 'tm' }, fmtDate(m.ts)),
      reactsEl(m)));
}
function reactsEl(m) {
  const e = Object.entries(m.reactions || {});
  if (!e.length) return null;
  return h('div', { class: 'reacts' }, ...e.map(([em, us]) => h('button', { class: us.includes(S.uid) || (S.pid && us.includes(S.pid)) ? 'mine' : '', onclick: (ev) => { ev.stopPropagation(); send({ t: 'react', id: m.id, emoji: em }); } }, em, String(us.length))));
}
function msgMenu(m) {
  haptic('light');
  const body = h('div',
    h('div', { class: 'react-pick' }, ...REACTS.map(e => h('button', { onclick: () => { send({ t: 'react', id: m.id, emoji: e }); s.close(); } }, e))),
    h('div', { class: 'list' },
      actionRow('reply', 'Reply', () => { s.close(); historySheet?.close(); openChat(m); }),
      actionRow('copy', 'Copy text', () => { s.close(); copyText(m.text); }),
      S.admin || isMod() || m.uid === S.uid ? actionRow('trash', 'Delete', () => { send({ t: 'chat.del', id: m.id }); s.close(); }, 'red') : null));
  const s = sheet({ title: m.name, body });
}
function openHistory() {
  historySheet?.close();
  const list = h('div', { class: 'chat-list' }, ...S.msgs.map(msgEl));
  if (!S.msgs.length) list.append(h('div', { class: 'empty-note' }, S.admin ? 'No messages in the last 48 hours.' : 'No messages since you joined.'));
  const note = S.admin ? 'As an admin you see the last 48 hours. Tap a message to reply.'
    : 'Messages since you joined. When you leave the voice chat they’re gone for you. Tap a message to reply.';
  const body = h('div', null, h('p', { class: 'muted note' }, note), list);
  historySheet = sheet({ title: 'Recent messages', body, tall: true, onClose: () => { historySheet = null; } });
  setTimeout(() => (historySheet && (historySheet.body.scrollTop = historySheet.body.scrollHeight)), 50);
}
on('ws:chat', ({ m }) => {
  S.msgs.push(m); if (S.msgs.length > 300) S.msgs.shift();
  if (historySheet) {
    const body = historySheet.body;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    body.querySelector('.empty-note')?.remove();
    body.querySelector('.chat-list').append(msgEl(m));
    if (atBottom || m.uid === S.uid) body.scrollTo({ top: body.scrollHeight, behavior: 'smooth' });
  }
  showPop(m);
});
on('ws:react', ({ id, reactions }) => {
  const m = S.msgs.find(x => x.id === id); if (!m) return; m.reactions = reactions;
  const el = historySheet?.body.querySelector(`.msg[data-id="${id}"]`);
  if (el) el.replaceWith(msgEl(m));
});
on('ws:chat.del', ({ id }) => {
  S.msgs = S.msgs.filter(x => x.id !== id);
  historySheet?.body.querySelector(`.msg[data-id="${id}"]`)?.remove();
});

// ======================= admin =======================
let adminAlerts = 0;
function bumpAdminBadge() { adminAlerts++; const b = $('#admin-badge'); b.hidden = false; b.textContent = adminAlerts; }
// (the badge sits on the More button; it clears when the admin opens Manage)
function openAdmin(tab = 'room') {
  adminAlerts = 0; $('#admin-badge').hidden = true;
  const tabs = h('div', { class: 'seg', style: { marginBottom: '6px' } });
  const content = h('div');
  const body = h('div', null, tabs, content);
  let cleanup = null;
  const s = sheet({ title: 'Manage', body, tall: true, onClose: () => cleanup?.() });
  // Health only for the group's owner and the bot's sudo users, not every admin
  const TABS = [['room', 'Room'], ['music', 'Music'], ['team', 'Team'], ['logs', 'Logs'], ['safety', 'Safety'], ...(S.sudo ? [['health', 'Health']] : [])];
  const go = (k) => {
    tab = k; cleanup?.(); cleanup = null;
    tabs.replaceChildren(...TABS.map(([id, l]) => h('button', { class: id === k ? 'on' : '', onclick: () => go(id) }, l)));
    const r = ({ room: adminRoom, music: adminMusic, team: (c) => adminTeam(c, go), logs: adminLogs, safety: adminSafety, health: adminHealth })[k](content, s);
    cleanup = typeof r === 'function' ? r : null;
  };
  go(tab);
}
function adminRoom(c) {
  const st = S.room.settings;
  const set = (k, v) => send({ t: 'admin.settings', settings: { [k]: v } });
  const title = h('input', { class: 'field', value: st.title || '', maxlength: 80, placeholder: 'Title' });
  const desc = h('textarea', { class: 'field', maxlength: 300, placeholder: 'Description (optional)' }, st.description || '');
  const recBox = h('div');
  c.replaceChildren(
    h('div', { class: 'grp-h' }, 'Title and description'),
    h('div', { style: { display: 'grid', gap: '8px', margin: '8px 0 16px' } }, title, desc,
      h('button', { class: 'btn sm', onclick: () => { send({ t: 'admin.settings', settings: { title: title.value, description: desc.value } }); toast('Saved'); } }, 'Save')),
    h('div', { class: 'grp-h' }, 'Permissions'),
    h('div', { class: 'list' },
      switchRow('New participants can speak', 'Off: they join as listeners and raise a hand', st.new_can_speak, v => set('new_can_speak', v)),
      switchRow('Lock voice chat', 'No new people can join', st.locked, v => set('locked', v)),
      switchRow('Soundboard', null, st.soundboard, v => set('soundboard', v)),
      switchRow('Join & leave sounds', 'For everyone', st.join_sound, v => set('join_sound', v))),
    h('div', { class: 'list' },
      actionRow('mic-off', 'Mute everyone', () => { send({ t: 'admin.muteall' }); haptic('warning'); }, '', 'Everyone except admins'),
      actionRow('mic', 'Let everyone speak again', () => send({ t: 'admin.unmuteall' }))),
    h('div', { class: 'grp-h' }, 'Private link'),
    h('div', { class: 'list' },
      actionRow('link', 'Copy invite link', () => { pendingLink = (url) => copyText(url); send({ t: 'admin.link' }); }, '', 'Only members of this group can use it'),
      actionRow('lock', 'Reset link', async () => { if (await confirmBox('The old link will stop working. Members need the new one (/vc in the group).', 'Reset link', true)) { pendingLink = (url) => { copyText(url); toast('New link copied'); }; send({ t: 'admin.resetlink' }); } }, 'red')),
    h('div', { class: 'grp-h' }, 'Recording'),
    recBox,
    h('div', { class: 'grp-h' }, 'Schedule'),
    scheduleBox());
  // the recording section follows the room state (start / rename / stop) without redrawing the whole tab
  const drawRec = () => {
    const rec = S.room.rec || {};
    const mine = rec.on && (rec.by === S.uid || (S.pid && rec.by === S.pid));
    const name = h('input', { class: 'field', maxlength: 60, placeholder: 'Recording name (optional)', value: rec.name || '' });
    name.addEventListener('change', () => send({ t: 'rec.name', name: name.value }));
    recBox.replaceChildren(
      mine ? h('div', { style: { margin: '8px 0 0' } }, name) : null,
      h('div', { class: 'list' },
        rec.on
          ? actionRow('rec', mine ? 'Stop recording and send' : 'Another admin is recording', () => { if (mine) { send({ t: 'rec.name', name: name.value }); recorder.stop(name.value); } }, 'red', mine ? 'The bot sends the file to your private chat' : null)
          : actionRow('rec', 'Start recording', startRecording, '', 'Records voices and music. The bot sends the file only to you, in private')));
  };
  drawRec();
  on('room:changed', drawRec);
  return () => off('room:changed', drawRec);
}
async function startRecording() {
  // recordings go to your private chat with the bot, so check the bot can message you before recording
  let ok = false;
  try { ok = (await api('/api/dmcheck')).ok; } catch {}
  if (!ok) {
    const bot = S.bot;
    const body = h('div', null,
      h('p', { style: { margin: '0 0 16px', color: 'var(--fg2)' } }, 'The bot sends recordings to your private chat. Open the bot and press Start once, then come back and start recording.'),
      bot ? h('button', { class: 'btn block', onclick: () => { try { tg?.openTelegramLink ? tg.openTelegramLink(`https://t.me/${bot}?start=rec`) : window.open(`https://t.me/${bot}?start=rec`); } catch {} s.close(); } }, `Open @${bot}`) : null);
    const s = sheet({ title: 'Start the bot first', body });
    return;
  }
  recorder.start();
}
let pendingLink = null;
on('ws:link', (m) => { pendingLink?.(m.url); pendingLink = null; });

function scheduleBox() {
  const box = h('div');
  const when = h('input', { class: 'field', type: 'datetime-local', 'aria-label': 'Start time' });
  const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0);
  when.value = new Date(d - d.getTimezoneOffset() * 60e3).toISOString().slice(0, 16);
  const ttl = h('input', { class: 'field', placeholder: 'What’s it about? (optional)', maxlength: 100 });
  const list = h('div');
  const load = async () => {
    const r = await api('/api/reports');
    list.replaceChildren(r.schedules.length ? h('div', { class: 'list' }, ...r.schedules.map(x => h('div', { class: 'row' }, icon('cal'),
      h('span', { class: 'lab' }, fmtDate(x.at), x.title ? h('small', null, x.title) : null),
      h('button', { class: 'close-x', 'aria-label': 'Delete', onclick: () => { send({ t: 'admin.unschedule', id: x.id }); setTimeout(load, 300); } }, icon('trash'))))) : '');
  };
  box.append(h('div', { style: { display: 'grid', gap: '8px', margin: '8px 0' } }, when, ttl,
    h('button', { class: 'btn sm', onclick: () => { const at = new Date(when.value).getTime() / 1000; if (!when.value || !Number.isFinite(at)) return toast('Pick a date and time first'); send({ t: 'admin.schedule', at, title: ttl.value }); setTimeout(load, 400); } }, 'Schedule reminder')), list);
  load().catch(() => {});
  return box;
}

// ---- music permissions ("music auth") ----
const MUSIC_PERMS = [
  ['add', 'Add songs'], ['next', 'Play a song next (jump the queue)'], ['vote', 'Vote to skip'], ['skip_own', 'Skip songs they added'],
  ['playback', 'Play, pause, seek, previous and skip any song'], ['queue', 'Change the queue', 'Remove, move, play now, shuffle, clear'],
  ['modes', 'Repeat and autoplay'], ['fx_own', 'Sound effects on their own songs'], ['fx', 'Sound effects on any song'],
  ['video', 'Turn on video for everyone'], ['stop', 'Stop the music'],
];
function adminMusic(c) {
  const draw = () => {
    const st = S.room.settings || {}, ev = st.music_everyone || {}, djs = st.music_dj || {};
    c.replaceChildren(
      h('p', { class: 'muted note' }, 'Admins can always do everything. Choose what everyone else can do, and give chosen people their own rights as music DJs.'),
      h('div', { class: 'grp-h' }, 'Everyone can'),
      h('div', { class: 'list' }, ...MUSIC_PERMS.map(([k, label, sub]) => switchRow(label, sub || null, !!ev[k], v => send({ t: 'admin.musicperms', everyone: { [k]: v } })))),
      h('div', { class: 'grp-h' }, 'Music DJs'),
      h('div', { class: 'list' },
        ...Object.entries(djs).map(([uid, d]) => actionRow('headphones', d.name, () => djSheet(Number(uid), d.name, d.perms), '',
          `${Object.values(d.perms || {}).filter(Boolean).length} of ${MUSIC_PERMS.length} rights`)),
        actionRow('plus', 'Add a music DJ', addDj, 'acc', 'Pick someone in the voice chat')));
  };
  draw();
  on('room:changed', draw);
  return () => off('room:changed', draw);
}
// ---- health: is everything working? ----
const PROBLEM = { 'music-error': 'Song couldn’t load', 'music-stall': 'Song got stuck', 'music-blocked': 'Phone blocked the sound', 'music-dead': 'Song link failed (retried)', 'music-webaudio': 'Sound without a tap (Web Audio)', 'music-wa-fail': 'Web Audio piece failed', 'music-gaveup': 'Phone gave up on a song', 'music-bg': 'Back from the background', 'music-radio': 'Background stream failed', 'music-fx-asleep': 'Effect paused (Web Audio asleep)',
  js: 'App error', promise: 'App error' };
function adminHealth(c) {
  let alive = true;
  const load = async () => {
    c.replaceChildren(h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } })));
    let r;
    try { r = await api('/api/health'); } catch (e) { c.replaceChildren(h('div', { class: 'empty-note' }, e.message)); return; }
    if (!alive) return;
    const dot = (ok) => h('span', { class: 'hdot ' + (ok === true ? 'ok' : ok === false ? 'bad' : 'idle') });
    const pr = r.problems;
    c.replaceChildren(
      h('div', { class: 'list' }, ...r.items.map(it => h('div', { class: 'row health' }, dot(it.ok),
        h('span', { class: 'lab' }, it.name, h('small', null, it.detail)),
        it.test ? h('button', { class: 'btn sm soft', onclick: async (e) => {
          const b = e.currentTarget; b.disabled = true; b.textContent = '…';
          try { const t = await api('/api/health', { body: { test: it.test } }); toast(t.detail, 4000, t.ok ? '' : 'err'); } catch (er) { toast(er.message, 3000, 'err'); }
          load();
        } }, 'Test') : null))),
      h('div', { class: 'grp-h' }, 'Problems on people’s phones (24 h)'),
      Object.keys(pr.counts).length
        ? h('div', null,
          h('p', { class: 'muted note' }, Object.entries(pr.counts).map(([k, n]) => `${PROBLEM[k] || k}: ${n}`).join(' · ')),
          h('div', { class: 'list' }, ...pr.latest.map(d => h('div', { class: 'row' },
            h('span', { class: 'lab' }, `${PROBLEM[d.kind] || d.kind} — ${d.name}`, h('small', null, `${fmtDate(d.ts)} · ${d.device} · ${d.detail}`))))))
        : h('p', { class: 'muted note' }, 'None reported. 🎉'),
      h('button', { class: 'btn sm soft', style: { marginTop: '10px' }, onclick: load }, 'Refresh'),
      S.owner ? sudoBox() : null);
  };
  load();
  return () => { alive = false; };
}
// ---- sudo users: the bot's owner picks who else sees the Health page (also /addsudo in Telegram) ----
function sudoBox() {
  const list = h('div', { class: 'list' });
  const input = h('input', { class: 'field', placeholder: '@username or Telegram id', autocomplete: 'off', style: { flex: 1 } });
  const draw = (users) => list.replaceChildren(...(users.length ? users.map(u => h('div', { class: 'row' }, icon('shield'),
    h('span', { class: 'lab' }, u.name || 'User', h('small', null, (u.username ? '@' + u.username + ' · ' : '') + u.uid)),
    h('button', { class: 'close-x', 'aria-label': 'Remove', onclick: async () => {
      if (!(await confirmBox(`Remove ${u.name || u.uid} from sudo users?`, 'Remove', true))) return;
      try { draw((await api('/api/sudo', { body: { remove: u.uid } })).users); toast('Removed'); } catch (e) { toast(e.message, 3000, 'err'); }
    } }, icon('trash')))) : [h('p', { class: 'muted note' }, 'Nobody yet.')]));
  const add = async () => {
    const who = input.value.trim(); if (!who) return;
    try { draw((await api('/api/sudo', { body: { who } })).users); input.value = ''; toast('Added'); haptic('success'); }
    catch (e) { toast(e.message, 4000, 'err'); }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  api('/api/sudo').then(r => draw(r.users), () => {});
  return h('div', null,
    h('div', { class: 'grp-h' }, 'Sudo users'),
    h('p', { class: 'muted note' }, 'They see this page too. Add as many as you like: here, or with /addsudo in Telegram (a user id, an @username, or as a reply to their message).'),
    list,
    h('div', { style: { display: 'flex', gap: '8px', marginTop: '8px' } }, input, h('button', { class: 'btn sm', onclick: add }, 'Add')));
}

// ---- team: moderators (people) and music DJs (music) ----
function adminTeam(c, go) {
  const draw = () => {
    const st = S.room.settings || {}, mods = st.mods || {}, djs = st.music_dj || {};
    c.replaceChildren(
      h('p', { class: 'muted note' }, 'Admins can do everything. Moderators look after people: mute or unmute, make listener or speaker, remove from the voice chat, delete messages (not ban, settings or recording). Music DJs get the music rights you choose.'),
      h('div', { class: 'grp-h' }, 'Moderators'),
      h('div', { class: 'list' },
        ...Object.entries(mods).map(([uid, m]) => actionRow('shield', m.name, async () => {
          if (await confirmBox(`Remove ${m.name} as moderator?`, 'Remove')) { send({ t: 'admin.unmod', uid: Number(uid) }); toast(`${m.name} is no longer a moderator`); }
        }, '', 'Tap to remove')),
        actionRow('plus', 'Add a moderator', () => pickPerson('Add a moderator', (p) => !p.admin && !mods[String(p.uid)], (p) => { send({ t: 'admin.mod', uid: p.uid }); toast(`${p.name} is now a moderator`); }), 'acc', 'Pick someone in the voice chat')),
      h('div', { class: 'grp-h' }, 'Music DJs'),
      h('div', { class: 'list' },
        ...Object.values(djs).map(d => actionRow('headphones', d.name, () => go('music'), '', `${Object.values(d.perms || {}).filter(Boolean).length} music rights`)),
        actionRow('headphones', 'Manage music DJs', () => go('music'), 'acc')));
  };
  draw();
  on('room:changed', draw);
  return () => off('room:changed', draw);
}
function pickPerson(title, filter, done) {
  const people = [...S.parts.values()].filter(p => p.uid !== S.uid && filter(p));
  const body = people.length
    ? h('div', { class: 'list' }, ...people.map(p => h('button', { class: 'row', onclick: () => { s.close(); done(p); } },
      h('div', { class: 'ia' }, avatarEl(p)), h('span', { class: 'lab' }, p.name, p.username ? h('small', null, '@' + p.username) : null))))
    : h('div', { class: 'empty-note' }, 'Nobody else to pick in the voice chat right now.');
  const s = sheet({ title, body });
}
function addDj() {
  const djs = S.room.settings?.music_dj || {};
  const people = [...S.parts.values()].filter(p => !p.admin && !djs[String(p.uid)]);
  const body = people.length
    ? h('div', { class: 'list' }, ...people.map(p => h('button', { class: 'row', onclick: () => {
      s.close(); djSheet(p.uid, p.name, Object.fromEntries(MUSIC_PERMS.map(([k]) => [k, true])), true);
    } }, h('div', { class: 'ia' }, avatarEl(p)), h('span', { class: 'lab' }, p.name, p.username ? h('small', null, '@' + p.username) : null))))
    : h('div', { class: 'empty-note' }, 'Everyone in the voice chat is already an admin or a DJ.');
  const s = sheet({ title: 'Add a music DJ', body });
}
function djSheet(uid, name, perms, isNew = false) {
  const cur = { ...perms };
  const save = () => send({ t: 'admin.dj', uid, name, perms: cur });
  if (isNew) { save(); toast(`${name} is now a music DJ`); }
  const body = h('div', null,
    h('p', { class: 'muted note' }, `What ${name} can do with the music:`),
    h('div', { class: 'list' }, ...MUSIC_PERMS.map(([k, label, sub]) => switchRow(label, sub || null, !!cur[k], v => { cur[k] = v; save(); }))),
    h('div', { class: 'list' }, actionRow('user-x', 'Remove as DJ', () => { send({ t: 'admin.undj', uid }); toast(`${name} is no longer a DJ`); s.close(); }, 'red')));
  const s = sheet({ title: '🎧 ' + name, body, tall: true });
}

// ---- logs ----
function adminLogs(c) {
  const RANGES = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All time'], ['custom', 'Custom']];
  let range = 'today', sort = 'time', view = 'people', q = '';
  const search = h('input', { type: 'search', placeholder: 'Name, @username or user ID', 'aria-label': 'Search logs' });
  const from = h('input', { type: 'date', 'aria-label': 'From' }), to = h('input', { type: 'date', 'aria-label': 'To' });
  const dates = h('div', { class: 'dates', hidden: true }, from, to);
  const chips = h('div', { class: 'chips' });
  const views = h('div', { class: 'seg' });
  const out = h('div');
  const bounds = () => {
    const now = Date.now() / 1000; const start = new Date(); start.setHours(0, 0, 0, 0);
    if (range === 'today') return [start / 1000, 0];
    if (range === '7d') return [now - 7 * 86400, 0];
    if (range === '30d') return [now - 30 * 86400, 0];
    if (range === 'custom') return [from.value ? new Date(from.value + 'T00:00').getTime() / 1000 : 0, to.value ? new Date(to.value + 'T23:59:59').getTime() / 1000 : 0];
    return [0, 0];
  };
  const qs = () => { const [f, t] = bounds(); return `from=${f}&to=${t}&q=${encodeURIComponent(q)}`; };
  const renderTop = () => {
    chips.replaceChildren(...RANGES.map(([k, l]) => h('button', { class: 'chip' + (range === k ? ' on' : ''), onclick: () => { range = k; dates.hidden = k !== 'custom'; renderTop(); load(); } }, l)));
    views.replaceChildren(...[['people', 'People'], ['sessions', 'Join log']].map(([k, l]) => h('button', { class: view === k ? 'on' : '', onclick: () => { view = k; renderTop(); load(); } }, l)));
  };
  let seq = 0;
  async function load() {
    const my = ++seq;
    out.replaceChildren(h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } })));
    try {
      if (view === 'people') {
        const r = await api(`/api/logs/users?${qs()}&sort=${sort}`);
        if (my !== seq) return;
        const sorts = h('div', { class: 'chips' }, ...[['time', 'Most time'], ['joins', 'Most joins'], ['mic', 'Most mic'], ['recent', 'Recent']].map(([k, l]) => h('button', { class: 'chip' + (sort === k ? ' on' : ''), onclick: () => { sort = k; load(); } }, l)));
        out.replaceChildren(sorts, r.users.length ? h('div', { class: 'list' }, ...r.users.map((u, i) => h('button', { class: 'row urow', onclick: () => userDetail(u) },
          h('span', { style: { width: '22px', color: 'var(--fg2)', fontSize: '13px', fontVariantNumeric: 'tabular-nums' } }, String(i + 1)),
          h('span', { class: 'lab' }, h('b', null, u.name), u.online ? h('span', { class: 'online-dot', title: 'In the room now' }) : null,
            h('small', null, `${u.username ? '@' + u.username + ' · ' : ''}${u.uid}`)),
          h('span', { class: 'rt' }, fmtDur(u.total), h('small', null, `${u.joins}× · mic ${fmtDur(u.mic)}`))))) : h('div', { class: 'empty-note' }, 'No one joined in this period.'));
      } else {
        const r = await api(`/api/logs?${qs()}&limit=150`);
        if (my !== seq) return;
        out.replaceChildren(r.sessions.length ? sessionsList(r.sessions, true) : h('div', { class: 'empty-note' }, 'No joins in this period.'));
      }
    } catch (e) { out.replaceChildren(h('div', { class: 'empty-note' }, e.message)); }
  }
  let tmr;
  search.oninput = () => { q = search.value.trim(); clearTimeout(tmr); tmr = setTimeout(load, 350); };
  from.onchange = to.onchange = load;
  renderTop();
  c.replaceChildren(h('div', { class: 'searchbar', style: { marginTop: '8px' } }, icon('search'), search), chips, dates, views, out);
  load();
}
function sessionsList(rows, showName) {
  return h('div', { class: 'list' }, ...rows.map(s => {
    const dur = (s.left || Date.now() / 1000) - s.joined;
    return h('div', { class: 'row urow' },
      h('span', { class: 'lab' }, showName ? h('b', null, s.name) : null, h('small', null,
        `Joined ${fmtDate(s.joined)} · ${s.left ? 'left ' + new Date(s.left * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : 'still here'}`)),
      h('span', { class: 'rt' }, fmtDur(dur), h('small', null, `mic ${fmtDur(s.mic_secs)} · off ${fmtDur(Math.max(0, dur - s.mic_secs))}`)));
  }));
}
async function userDetail(u) {
  const body = h('div');
  const s = sheet({ title: u.name, body, tall: true });
  const micPct = u.total ? Math.round(u.mic / u.total * 100) : 0;
  body.append(
    h('p', { class: 'muted', style: { margin: '0 0 6px' } }, `${u.username ? '@' + u.username + ' · ' : ''}ID ${u.uid}`, u.online ? ' · in the room now' : ''),
    h('div', { class: 'stat-grid' },
      h('div', { class: 'stat' }, h('b', null, String(u.joins)), h('span', null, 'Times joined')),
      h('div', { class: 'stat' }, h('b', null, fmtDur(u.total)), h('span', null, 'Total time')),
      h('div', { class: 'stat' }, h('b', null, fmtDur(u.mic)), h('span', null, 'Mic on'), h('div', { class: 'micbar' }, h('i', { style: { width: micPct + '%' } }))),
      h('div', { class: 'stat' }, h('b', null, fmtDur(u.mic_off)), h('span', null, 'Mic off'))),
    h('p', { class: 'muted', style: { fontSize: '13px' } }, `First seen ${fmtDate(u.first)} · last joined ${fmtDate(u.last)}`),
    h('div', { class: 'grp-h' }, 'Every visit'));
  const holder = h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } }));
  body.append(holder);
  try { const r = await api(`/api/logs?q=${u.uid}&limit=200`); holder.replaceWith(sessionsList(r.sessions, false)); }
  catch (e) { holder.textContent = e.message; }
  if (!S.parts.get(u.uid)?.admin) body.append(h('div', { class: 'list' }, actionRow('ban', 'Ban', async () => {
    if (await confirmBox(`Ban ${u.name}?`, 'Ban', true)) { send({ t: 'admin.ban', uid: u.uid, name: u.name }); s.close(); }
  }, 'red')));
}

// ---- safety: reports, flagged, bans ----
async function adminSafety(c) {
  c.replaceChildren(h('div', { class: 'empty-note' }, h('div', { class: 'spinner', style: { margin: '0 auto' } })));
  const flagged = [...S.parts.values()].filter(p => p.flags?.length);
  let r;
  try { r = await api('/api/reports'); } catch (e) { return c.replaceChildren(h('div', { class: 'empty-note' }, e.message)); }
  c.replaceChildren(
    h('div', { class: 'grp-h' }, 'Needs a look (in the room now)'),
    flagged.length ? h('div', { class: 'list' }, ...flagged.map(p => h('button', { class: 'row', onclick: () => openParticipant(p.uid) },
      h('span', { style: { fontSize: '18px' } }, '⚠️'), h('span', { class: 'lab' }, p.name, h('small', null, p.flags.join(' · ')))))) : h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Nobody suspicious right now. People who join in a flood, rejoin over and over, or get reported show up here.'),
    h('div', { class: 'grp-h' }, 'Reports'),
    r.reports.length ? h('div', { class: 'list' }, ...r.reports.map(x => h('div', { class: 'rep' },
      h('div', { class: 'rt' }, icon('flag'), h('div', null, h('b', null, `${x.name}`), h('small', null, `Reported by ${x.by_name} · ${fmtDate(x.ts)}`), h('small', null, x.reason || 'No reason given'))),
      h('div', { class: 'ra' },
        h('button', { class: 'btn sm soft', onclick: async (e) => { await api('/api/reports', { body: { id: x.id } }); e.target.closest('.rep').remove(); } }, 'Dismiss'),
        h('button', { class: 'btn sm danger', onclick: async () => { if (await confirmBox(`Ban ${x.name}?`, 'Ban', true)) { send({ t: 'admin.ban', uid: x.uid, name: x.name }); await api('/api/reports', { body: { id: x.id } }); adminSafety(c); } } }, 'Ban'))))) : h('p', { class: 'muted', style: { fontSize: '14px' } }, 'No open reports.'),
    h('div', { class: 'grp-h' }, 'Banned'),
    r.bans.length ? h('div', { class: 'list' }, ...r.bans.map(b => h('div', { class: 'row' }, icon('ban'),
      h('span', { class: 'lab' }, b.name || String(b.uid), h('small', null, `ID ${b.uid} · ${fmtDate(b.ts)}`)),
      h('button', { class: 'btn sm soft', onclick: () => { send({ t: 'admin.unban', uid: b.uid }); setTimeout(() => adminSafety(c), 300); } }, 'Unban')))) : h('p', { class: 'muted', style: { fontSize: '14px' } }, 'No one is banned.'));
}

boot();
