// Admin recording: mixes everyone's voice + the music on the admin's device, then the bot sends the file.
import { api, off, on, send, toast } from './core.js';
import { musicTap } from './player.js';
import { getCtx } from './sounds.js';
import { audioTracks } from './voice.js';

let rec = null;
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
    const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg'].find(t => MediaRecorder.isTypeSupported?.(t)) || '';
    const mr = new MediaRecorder(dest.stream, type ? { mimeType: type, audioBitsPerSecond: 96000 } : undefined);
    const chunks = [];
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      off('voice:audio-track', onTrack);
      if (tap) try { tap.node.disconnect(dest); } catch {}
      const blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
      const ext = (mr.mimeType || '').includes('mp4') ? 'm4a' : (mr.mimeType || '').includes('ogg') ? 'ogg' : 'webm';
      toast('Uploading recording…', 4000);
      const fd = new FormData();
      fd.append('file', blob, 'recording.' + ext);
      fd.append('ext', ext);
      try {
        const r = await api('/api/recording', { body: fd });
        toast(r.to === 'dm' ? 'Recording sent to you by the bot' : 'Recording posted in the group', 4000);
      } catch (e) { toast(e.message, 5000); }
    };
    mr.start(5000);
    rec = { mr };
    send({ t: 'rec.start' });
    toast('Recording started');
  },
  stop() {
    if (!rec) return send({ t: 'rec.stop' });
    try { rec.mr.stop(); } catch {}
    rec = null;
    send({ t: 'rec.stop' });
  },
};
