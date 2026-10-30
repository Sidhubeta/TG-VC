// Admin recording: mixes everyone's voice + the music on the admin's device, then the bot sends the file.
import { S, api, h, off, on, send, sheet, toast } from './core.js';
import { musicTap } from './player.js';
import { getCtx } from './sounds.js';
import { audioTracks } from './voice.js';

let rec = null, recName = '';

// ---- highlights: who spoke when, and the songs, as chapters sent along with the recording ----
const mmss = (s) => { s = Math.floor(s); const h_ = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return (h_ ? h_ + ':' + String(m).padStart(2, '0') : String(m).padStart(2, '0')) + ':' + String(x).padStart(2, '0'); };
// samples: one per second, { who: uid | null, song: 'title' | null }. Returns the text of the chapters.
export function buildChapters(samples, names) {
  const events = [], talk = {};
  let run = null, gap = 0, lastSong = null, lastSpeaker = null;
  const flush = () => {
    if (run && run.len >= 5) {  // a word or two isn't a chapter
      talk[run.who] = (talk[run.who] || 0) + run.len;
      if (run.who !== lastSpeaker) { events.push({ t: run.start, text: names[run.who] || 'Someone' }); lastSpeaker = run.who; }
    }
    run = null;
  };
  samples.forEach((s, t) => {
    if (s.song && s.song !== lastSong) { events.push({ t, text: '🎵 ' + s.song }); lastSong = s.song; }
    if (!s.song) lastSong = null;
    if (s.who == null) { if (run && ++gap > 3) flush(); return; }
    if (run && run.who === s.who) { run.len += 1 + gap; gap = 0; return; }
    flush(); gap = 0;
    run = { who: s.who, start: t, len: 1 };
  });
  flush();
  if (!events.length) return '';
  events.sort((a, b) => a.t - b.t);
  const lines = events.slice(0, 80).map(e => `${mmss(e.t)} ${e.text}`);
  const top = Object.entries(talk).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([u, s]) => `${names[u] || 'Someone'} ${mmss(s)}`);
  return 'Chapters\n' + lines.join('\n') + (events.length > 80 ? '\n…' : '') + (top.length ? '\n\nTalk time: ' + top.join(' · ') : '');
}
export const recorder = {
  async start() {
    if (rec) return toast('Already recording');
    const ctx = getCtx();
    if (!ctx || !window.MediaRecorder) return toast('Recording is not supported on this device');
    const dest = ctx.createMediaStreamDestination();
    const added = new Set();
    const addTrack = (t) => {
      if (!t || added.has(t.id)) return;
      added.add(t.id);
      try { ctx.createMediaStreamSource(new MediaStream([t])).connect(dest); } catch (e) { console.warn(e); }
    };
    audioTracks().forEach(addTrack);
    const tap = musicTap();
    if (tap && tap.ctx === ctx) tap.node.connect(dest);
    const onTrack = on('voice:audio-track', ({ track }) => addTrack(track.mediaStreamTrack));
    // every second: who's talking (the one already talking keeps it while several overlap) and the song
    let speaking = new Set(), prev = null;
    const names = {}, samples = [];
    const onSpeak = on('voice:speaking', (set) => { speaking = set; });
    const tick = setInterval(() => {
      const who = speaking.has(prev) ? prev : [...speaking][0] ?? null;
      prev = who;
      if (who != null && !names[who]) names[who] = S.parts.get(who)?.name || 'Someone';
      const m = S.music;
      samples.push({ who, song: m?.status === 'playing' && m.current ? `${m.current.title}${m.current.artist ? ' — ' + m.current.artist : ''}` : null });
    }, 1000);
    const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg'].find(t => MediaRecorder.isTypeSupported?.(t)) || '';
    const mr = new MediaRecorder(dest.stream, type ? { mimeType: type, audioBitsPerSecond: 96000 } : undefined);
    const chunks = [];
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      off('voice:audio-track', onTrack);
      off('voice:speaking', onSpeak); clearInterval(tick);
      let chapters = '';
      try { chapters = buildChapters(samples, names); } catch (e) { console.warn(e); }
      if (tap) try { tap.node.disconnect(dest); } catch {}
      const blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
      const ext = (mr.mimeType || '').includes('mp4') ? 'm4a' : (mr.mimeType || '').includes('ogg') ? 'ogg' : 'webm';
      toast('Uploading recording…', 4000);
      const fd = new FormData();
      fd.append('file', blob, 'recording.' + ext);
      fd.append('ext', ext);
      fd.append('name', recName);
      if (chapters) fd.append('chapters', chapters.slice(0, 3800));
      try {
        await api('/api/recording', { body: fd });
        toast('Recording sent to your private chat with the bot', 4000);
      } catch (e) { toast(e.message, 5000); offerDownload(blob, ext); }
    };
    mr.start(5000);
    rec = { mr };
    send({ t: 'rec.start' });
    toast('Recording started');
  },
  stop(name = '') {
    recName = String(name || '').trim().slice(0, 60);
    if (!rec) return send({ t: 'rec.stop' });
    try { rec.mr.stop(); } catch {}
    rec = null;
    send({ t: 'rec.stop' });
  },
};

// if the upload fails, don't lose the recording: let the admin save it on the phone instead
function offerDownload(blob, ext) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { class: 'btn block', href: url, download: (recName || 'recording') + '.' + ext }, 'Save recording to this device');
  sheet({ title: 'Recording not sent', body: h('div', null, h('p', { class: 'muted', style: { margin: '0 0 14px' } }, 'The recording couldn’t be sent. You can save it here instead.'), a),
    onClose: () => setTimeout(() => URL.revokeObjectURL(url), 60000) });
}
