import { $, S, actionRow, api, avatarEl, closeWS, confirmBox, connectWS, copyText, emit, fmtDate, fmtDur, fmtTime, grad, h, haptic, icon, initials, on, pushBack, reconnectWS, send, sheet, store, switchRow, tg, toast } from './core.js';
import { openNP, openSearch, pip, pipSupported, prompt_, refresh as refreshMusic, stopMusic, unlockAudio } from './player.js';
import { BOARD, joinSound, leaveSound, micSound, playBoard } from './sounds.js';
import { V, attachVideo, getVolume, isMutedForMe, leaveVoice, screenSupported, setCam, setMic, setScreen, setVolume, startVoice, toggleMuteForMe, unblockAudio } from './voice.js';
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
  Object.assign(S, { session: r.session, uid: r.uid, chat: r.chat, admin: r.admin, group: r.group });
  S.lk = r.livekit;
  connectWS({ asGroup: store.get('asGroup:' + r.chat, false) && r.admin });
  try { tg?.enableClosingConfirmation?.(); } catch {}
  document.addEventListener('pointerdown', firstTap, { once: true, capture: true });
}
function firstTap() { unlockAudio(); unblockAudio(); }

// Closing the app = leaving the voice chat (the socket also drops, this just makes it instant)
let byeSent = false;
function bye() {
  if (byeSent || !S.session) return;
  byeSent = true;
  try { navigator.sendBeacon('/api/bye?s=' + encodeURIComponent(S.session)); } catch {}
  leaveVoice();
}
window.addEventListener('pagehide', bye);
window.addEventListener('beforeunload', bye);

function goFullscreen() {
  try {
    if (tg?.isVersionAtLeast?.('8.0') && !tg.isFullscreen) tg.requestFullscreen();
  } catch {}
}

let started = false;
on('ws:init', (m) => {
  S.admin = m.admin; S.room = m.room; S.group = m.group;
  S.parts = new Map(m.parts.map(p => [p.uid, p]));
  S.chat = m.chat;
  $('#gate').hidden = true;
  $('#room').hidden = false;
  if (!started) {
    started = true;
    startVoice(S.lk);
    bindDock();
  }
  renderHeader(); renderGrid(true); renderDock();
});
on('fatal', ({ title, text }) => { leaveVoice(); stopMusic(); $('#room').hidden = true; gate(title, text, h('button', { class: 'btn soft', onclick: () => tg?.close?.() }, 'Close')); });
on('ws:down', () => toast('Reconnecting…', 1500));
on('ws:kicked', (m) => { closeWS(); emit('fatal', { title: m.ban ? 'Banned' : 'Removed', text: m.reason || (m.ban ? 'An admin banned you from this voice chat.' : 'An admin removed you from this voice chat.') }); });
on('ws:locked', () => emit('fatal', { title: 'Locked', text: 'Admins locked this voice chat.' }));
on('ws:toast', (m) => toast(m.text));

// ======================= header =======================
function renderHeader() {
  const s = S.room.settings || {};
  $('#r-title').textContent = s.title || S.group || 'Voice Chat';
  const n = S.parts.size;
  $('#r-count').textContent = `${n} ${n === 1 ? 'participant' : 'participants'}`;
  $('#r-rec').hidden = !S.room.rec?.on;
  $('#r-lock').hidden = !s.locked;
  const d = $('#r-desc'); d.hidden = !s.description; d.textContent = s.description || '';
  $('#b-admin').querySelector('use').setAttribute('href', S.admin ? '#i-shield' : '#i-more');
}
on('ws:room', (m) => { S.room = m.room; renderHeader(); renderDock(); refreshMusic(); });
$('#b-search').onclick = () => openSearch('home');
$('#b-admin').onclick = () => (S.admin ? openAdmin() : openMore());

// ======================= participants =======================
const tiles = new Map();
function sortParts() {
  return [...S.parts.values()].sort((a, b) =>
    (b.uid === S.uid) - (a.uid === S.uid) || (!!b.hand - !!a.hand) || (b.mic - a.mic) || (b.canSpeak - a.canSpeak) || (a.joined - b.joined));
}
function statusOf(p) {
  if (V.speaking.has(p.uid) && p.mic) return ['speaking', 'speaking'];
  if (p.hand) return ['wants to speak', 'blue'];
  if (p.forceMuted) return ['muted by admin', 'red'];
  if (isMutedForMe(p.uid)) return ['muted for you', 'red'];
  if (p.bio) return [p.bio, ''];
  if (p.mic && p.canSpeak) return ['unmuted', 'blue'];
  return ['listening', ''];
}
const statusText = (p) => statusOf(p)[0];
function tileFor(p) {
  let t = tiles.get(p.uid);
  if (!t) {
    t = h('button', { class: 'prow', onclick: () => openParticipant(p.uid) });
    t._photo = null;
    tiles.set(p.uid, t);
  }
  const live = p.mic && p.canSpeak;
  const speaking = V.speaking.has(p.uid) && p.mic;
  t.className = 'prow' + (live ? ' live' : '') + (p.forceMuted ? ' forced' : '') + (speaking ? ' speaking' : '');
  const key = `${p.name}|${p.photo}`;
  if (t._key !== key) { t._key = key; t._av = avatarEl(p); }
  const [st, cls] = statusOf(p);
  t.replaceChildren(
    h('div', { class: 'pav' }, h('span', { class: 'wave' }), h('span', { class: 'wave w2' }), t._av, p.hand ? h('span', { class: 'hand' }, '✋') : null),
    h('div', { class: 'pmeta' },
      h('div', { class: 'pname' }, h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, p.name), p.uid === S.uid ? h('span', { class: 'you' }, '(you)') : null,
        p.admin && !p.asGroup ? h('span', { class: 'tag' }, 'admin') : null,
        S.admin && p.flags?.length ? h('span', { class: 'tag warn', title: p.flags.join('\n') }, 'check') : null),
      h('div', { class: 'psub ' + cls }, st)),
    h('span', { class: 'pmic' }, icon(live ? 'mic' : 'mic-off')));
  t.setAttribute('aria-label', `${p.name}, ${st}`);
  return t;
}
function renderGrid(full = false) {
  const g = $('#grid');
  const list = sortParts();
  const want = list.map(tileFor);
  if (full || g.children.length !== want.length || want.some((el, i) => g.children[i] !== el)) g.replaceChildren(...want);
  const lh = $('#lh-count'); if (lh) lh.textContent = `${list.length} ${list.length === 1 ? 'participant' : 'participants'}`;
}
on('ws:p.join', (m) => {
  S.parts.set(m.p.uid, m.p); renderGrid(); renderHeader();
  if (S.room.settings?.join_sound !== false && store.get('joinSound', true)) joinSound();
});
on('ws:p.leave', (m) => {
  const t = tiles.get(m.uid);
  S.parts.delete(m.uid);
  if (t) { t.classList.add('out'); setTimeout(() => { tiles.delete(m.uid); renderGrid(); }, 280); }
  renderHeader();
  removeVideo(m.uid);
  if (S.room.settings?.join_sound !== false && store.get('joinSound', true)) leaveSound();
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
on('ws:hand', (m) => { if (S.admin && m.uid !== S.uid) toast(`✋ ${m.name} wants to speak`); });
on('ws:alert', (m) => { if (S.admin) { toast(`⚠️ ${m.name}: ${m.flags[0]}`, 4000); bumpAdminBadge(); } });
on('voice:speaking', () => { for (const p of S.parts.values()) tileFor(p); renderDock(); });
on('voice:levels', (lv) => {
  for (const [uid, l] of lv) {
    const t = tiles.get(uid); if (t) t.style.setProperty('--lvl', Math.min(1, l * 2.2).toFixed(3));
  }
  const me = lv.get(S.uid) || 0;
  $('#b-mic').style.setProperty('--lvl', Math.min(1, me * 2.4).toFixed(3));
});

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
        h('p', null, [p.username ? '@' + p.username : null, p.realName ? `real: ${p.realName}` : null, p.admin ? 'admin' : null].filter(Boolean).join('  ·  ')),
        p.bio ? h('div', { class: 'bio' }, p.bio) : null,
        h('p', { style: { marginTop: '6px' } }, p.forceMuted ? 'muted by admin' : p.hand ? 'wants to speak' : (p.mic && p.canSpeak) ? 'unmuted' : 'listening')),
      S.admin && p.flags?.length ? h('div', null, ...p.flags.map(f => h('div', { class: 'flag' }, '⚠️', h('span', null, f)))) : null,
      !me ? h('div', { class: 'list' },
        h('div', { class: 'row' }, icon('vol-low'), vol, icon('vol')),
        actionRow(isMutedForMe(uid) ? 'vol' : 'vol-x', isMutedForMe(uid) ? 'Unmute for me' : 'Mute for me', () => { const m = toggleMuteForMe(uid); toast(m ? `${p.name} is muted for you` : `${p.name} unmuted for you`); renderGrid(); render(); }, '', 'Only you stop hearing them')) : null,
      S.admin && !me && !p.admin ? h('div', { class: 'list' },
        p.forceMuted
          ? actionRow('mic', 'Allow to speak', () => { send({ t: 'admin.unmute', uid }); s.close(); })
          : actionRow('mic-off', 'Mute for everyone', () => { send({ t: 'admin.mute', uid }); s.close(); }, '', 'They can’t unmute until you allow it'),
        p.canSpeak && !p.forceMuted
          ? actionRow('headphones', 'Make listener', () => { send({ t: 'admin.role', uid, role: 'listener' }); s.close(); })
          : !p.forceMuted ? actionRow('hand', p.hand ? 'Accept hand — let them speak' : 'Make speaker', () => { send({ t: 'admin.role', uid, role: 'speaker' }); s.close(); }) : null,
        p.flags?.length ? actionRow('check', 'Clear warnings', () => { send({ t: 'admin.clearflags', uid }); s.close(); }) : null,
        actionRow('user-x', 'Remove from voice chat', async () => { if (await confirmBox(`Remove ${p.name}? They can rejoin.`, 'Remove', true)) { send({ t: 'admin.kick', uid }); s.close(); } }, 'red'),
        actionRow('ban', 'Ban', async () => { if (await confirmBox(`Ban ${p.name}? They won't be able to join again until you unban them.`, 'Ban', true)) { send({ t: 'admin.ban', uid }); s.close(); } }, 'red')) : null,
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
on('voice:video', ({ uid, track, source, on: isOn }) => {
  const key = uid + ':' + source;
  if (!isOn) return removeVideo(uid, key);
  if (vids.has(key)) return;
  const p = S.parts.get(uid);
  const el = attachVideo(track);
  const tile = h('div', { class: 'vtile' + (source === 'screen_share' ? ' screen' : '') }, el,
    h('span', { class: 'lbl' }, (p?.name || '') + (source === 'screen_share' ? ' · screen' : '')),
    h('button', { class: 'fs', 'aria-label': 'Full screen', onclick: () => { try { (el.requestFullscreen || el.webkitEnterFullscreen).call(el); } catch {} } }, icon('expand')),
    pipSupported(el) ? h('button', { class: 'fs pip', 'aria-label': 'Picture in picture', onclick: () => pip(el) }, icon('pip')) : null);
  if (uid === S.uid && source !== 'screen_share') el.style.transform = 'scaleX(-1)';
  vids.set(key, { tile, uid });
  $('#stage').append(tile);
});
function removeVideo(uid, key) {
  for (const [k, v] of vids) if ((key ? k === key : v.uid === uid)) { v.tile.remove(); vids.delete(k); }
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
  mic.replaceChildren(h('span', { class: 'b2' }), h('span', { class: 'b1' }), h('span', { class: 'blob' }), icon(ic));
  lbl.textContent = text;
  mic.setAttribute('aria-label', text);
  const cam = $('#b-cam');
  cam.classList.toggle('on', V.cam);
  cam.querySelector('.c').replaceChildren(icon(V.cam ? 'video' : 'video-off'));
}
on('voice', ({ state }) => { S.voiceState = state; renderDock(); });
on('voice:local', renderDock);
let unlockBtn = null;
on('voice:blocked', (b) => {
  if (b && !unlockBtn) { unlockBtn = h('button', { class: 'btn unlock', onclick: () => { unblockAudio(); unlockAudio(); unlockBtn.remove(); unlockBtn = null; } }, icon('vol'), 'Tap to hear the room'); document.body.append(unlockBtn); }
  if (!b && unlockBtn) { unlockBtn.remove(); unlockBtn = null; }
});

function bindDock() {
  $('#b-mic').onclick = async () => {
    unlockAudio();
    const p = me(); if (!p) return;
    if (S.voiceState !== 'ready') return toast(S.voiceState === 'off' ? 'Voice isn’t set up on the server yet' : 'Voice is still connecting');
    if (p.forceMuted) { haptic('error'); return toast('An admin muted you'); }
    if (!p.canSpeak) { haptic('medium'); send({ t: 'hand', up: !p.hand }); if (!p.hand) toast('Hand raised — admins will let you speak'); return; }
    haptic('medium');
    const want = !V.mic;
    if (await setMic(want)) { micSound(want); renderDock(); }
  };
  $('#b-cam').onclick = () => {
    const p = me(); if (!p?.canSpeak) return toast('Only speakers can turn on the camera');
    haptic('light'); setCam(!V.cam);
  };
  $('#b-chat').onclick = openChat;
  $('#b-more').onclick = openMore;
  $('#b-leave').onclick = async () => {
    if (!(await confirmBox('Leave the voice chat?', 'Leave', true))) return;
    if (V.mic) await setMic(false);
    await leaveVoice(); closeWS(); stopMusic();
    try { tg?.disableClosingConfirmation?.(); tg?.close(); } catch {}
    gate('You left', 'Open the link in your group again to rejoin.');
    $('#room').hidden = true;
  };
}

// ======================= more =======================
function openMore() {
  const p = me();
  const body = h('div',
    h('div', { class: 'list' },
      p && !p.canSpeak ? actionRow('hand', p.hand ? 'Lower hand' : 'Raise hand', () => { send({ t: 'hand', up: !p.hand }); s.close(); }) : null,
      actionRow('music', 'Music player', () => { s.close(); openNP(); }),
      actionRow('search', 'Browse music', () => { s.close(); openSearch('home'); }),
      S.room.settings?.soundboard ? actionRow('sound', 'Soundboard', () => { s.close(); openBoard(); }) : null,
      screenSupported() && p?.canSpeak ? actionRow('screen', V.screen ? 'Stop sharing screen' : 'Share screen', () => { s.close(); setScreen(!V.screen); }) : null),
    h('div', { class: 'list' },
      switchRow('Join & leave sounds', null, store.get('joinSound', true), v => store.set('joinSound', v)),
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

// ======================= chat =======================
const REACTS = ['👍', '❤️', '😂', '🔥', '😮', '😢', '👏', '🎉'];
let chatSheet = null, replyTo = null;
function msgEl(m) {
  const mine = m.uid === S.uid;
  const el = h('div', { class: 'msg' + (mine ? ' mine' : ''), 'data-id': m.id, onclick: () => msgMenu(m) },
    mine ? null : avatarEl({ uid: m.uid, name: m.name, photo: m.photo }, 'ma'),
    h('div', { class: 'bub' },
      mine ? null : h('div', { class: 'who', style: { color: `hsl(${(Math.abs(m.uid) * 47) % 360} 70% 62%)` } }, m.name),
      m.reply ? h('div', { class: 'rq' }, h('b', null, m.reply.name), h('span', null, m.reply.text)) : null,
      h('span', { class: 'txt' }, m.text),
      h('span', { class: 'tm' }, new Date(m.ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })),
      reactsEl(m)));
  return el;
}
function reactsEl(m) {
  const e = Object.entries(m.reactions || {});
  if (!e.length) return null;
  return h('div', { class: 'reacts' }, ...e.map(([em, us]) => h('button', { class: us.includes(S.uid) ? 'mine' : '', onclick: (ev) => { ev.stopPropagation(); send({ t: 'react', id: m.id, emoji: em }); } }, em, String(us.length))));
}
function msgMenu(m) {
  haptic('light');
  const body = h('div',
    h('div', { class: 'react-pick' }, ...REACTS.map(e => h('button', { onclick: () => { send({ t: 'react', id: m.id, emoji: e }); s.close(); } }, e))),
    h('div', { class: 'list' },
      actionRow('reply', 'Reply', () => { s.close(); setReply(m); }),
      actionRow('copy', 'Copy text', () => { s.close(); copyText(m.text); }),
      S.admin || m.uid === S.uid ? actionRow('trash', 'Delete', () => { send({ t: 'chat.del', id: m.id }); s.close(); }, 'red') : null));
  const s = sheet({ title: m.name, body });
}
function setReply(m) {
  replyTo = m;
  const bar = chatSheet?.el.querySelector('.reply-bar');
  if (!bar) return;
  bar.hidden = !m;
  if (m) bar.replaceChildren(h('div', { class: 'rb' }, h('b', null, m.name), h('span', null, m.text)), h('button', { class: 'close-x', 'aria-label': 'Cancel reply', onclick: () => setReply(null) }, icon('close')));
  chatSheet.el.querySelector('textarea').focus();
}
function openChat() {
  S.unread = 0; updateChatBadge();
  const list = h('div', { class: 'chat-list' }, ...S.chat.map(msgEl));
  if (!S.chat.length) list.append(h('div', { class: 'empty-note' }, 'Say hi to everyone in the voice chat 👋'));
  const ta = h('textarea', { rows: 1, placeholder: 'Message', maxlength: 1000, 'aria-label': 'Message' });
  const sendIt = () => {
    const text = ta.value.trim(); if (!text) return;
    send({ t: 'chat', text, reply: replyTo?.id || null });
    ta.value = ''; ta.style.height = ''; setReply(null); haptic('light');
  };
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !('ontouchstart' in window)) { e.preventDefault(); sendIt(); } });
  chatSheet = sheet({ title: 'Chat', body: list, tall: true, onClose: () => { chatSheet = null; replyTo = null; } });
  chatSheet.el.append(h('div', { class: 'reply-bar', hidden: true }), h('div', { class: 'composer' }, ta, h('button', { class: 'send', 'aria-label': 'Send', onclick: sendIt }, icon('send'))));
  setTimeout(() => (chatSheet.body.scrollTop = chatSheet.body.scrollHeight), 50);
}
function updateChatBadge() { const b = $('#chat-badge'); b.hidden = !S.unread; b.textContent = S.unread > 99 ? '99+' : S.unread; }
on('ws:chat', ({ m }) => {
  S.chat.push(m); if (S.chat.length > 80) S.chat.shift();
  if (chatSheet) {
    const body = chatSheet.body;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    body.querySelector('.empty-note')?.remove();
    body.querySelector('.chat-list').append(msgEl(m));
    if (atBottom || m.uid === S.uid) body.scrollTo({ top: body.scrollHeight, behavior: 'smooth' });
  } else if (m.uid !== S.uid) { S.unread++; updateChatBadge(); }
});
on('ws:react', ({ id, reactions }) => {
  const m = S.chat.find(x => x.id === id); if (!m) return; m.reactions = reactions;
  const el = chatSheet?.body.querySelector(`.msg[data-id="${id}"]`);
  if (el) el.replaceWith(msgEl(m));
});
on('ws:chat.del', ({ id }) => {
  S.chat = S.chat.filter(x => x.id !== id);
  chatSheet?.body.querySelector(`.msg[data-id="${id}"]`)?.remove();
});

// ======================= admin =======================
let adminAlerts = 0;
function bumpAdminBadge() { adminAlerts++; const b = $('#admin-badge'); b.hidden = false; b.textContent = adminAlerts; }
function openAdmin(tab = 'room') {
  adminAlerts = 0; $('#admin-badge').hidden = true;
  const tabs = h('div', { class: 'seg', style: { marginBottom: '6px' } });
  const content = h('div');
  const body = h('div', null, tabs, content);
  const s = sheet({ title: 'Manage', body, tall: true });
  const TABS = [['room', 'Room'], ['logs', 'Logs'], ['safety', 'Safety']];
  const go = (k) => { tab = k; tabs.replaceChildren(...TABS.map(([id, l]) => h('button', { class: id === k ? 'on' : '', onclick: () => go(id) }, l))); ({ room: adminRoom, logs: adminLogs, safety: adminSafety })[k](content, s); };
  go(tab);
}
function adminRoom(c) {
  const st = S.room.settings;
  const set = (k, v) => send({ t: 'admin.settings', settings: { [k]: v } });
  const title = h('input', { class: 'field', value: st.title || '', maxlength: 80, placeholder: 'Title' });
  const desc = h('textarea', { class: 'field', maxlength: 300, placeholder: 'Description (optional)' }, st.description || '');
  c.replaceChildren(
    h('div', { class: 'grp-h' }, 'Title and description'),
    h('div', { style: { display: 'grid', gap: '8px', margin: '8px 0 16px' } }, title, desc,
      h('button', { class: 'btn sm', onclick: () => { send({ t: 'admin.settings', settings: { title: title.value, description: desc.value } }); toast('Saved'); } }, 'Save')),
    h('div', { class: 'grp-h' }, 'Permissions'),
    h('div', { class: 'list' },
      switchRow('New participants can speak', 'Off: they join as listeners and raise a hand', st.new_can_speak, v => set('new_can_speak', v)),
      switchRow('Lock voice chat', 'No new people can join', st.locked, v => set('locked', v)),
      switchRow('Only admins add songs', null, st.music_admin_only, v => set('music_admin_only', v)),
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
    h('div', { class: 'list' },
      S.room.rec?.on
        ? actionRow('rec', S.room.rec.by === S.uid ? 'Stop recording and send' : 'Recording in progress', () => { if (S.room.rec.by === S.uid) recorder.stop(); }, 'red', 'The file is sent to you by the bot')
        : actionRow('rec', 'Start recording', () => recorder.start(), '', 'Records voices and music from your device')),
    h('div', { class: 'grp-h' }, 'Schedule'),
    scheduleBox());
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
    h('button', { class: 'btn sm', onclick: () => { const at = new Date(when.value).getTime() / 1000; send({ t: 'admin.schedule', at, title: ttl.value }); setTimeout(load, 400); } }, 'Schedule reminder')), list);
  load().catch(() => {});
  return box;
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
