"""The music played into the voice chat itself, like a music bot.

Every phone in the room is in the voice chat already. Instead of each phone playing the song by itself (a plain
player, Web Audio when Android wants a tap first, the room's stream when Telegram is minimized — three ways that
each had their own problems), the server joins the voice chat as "Music" and plays the song there: the same moment
for everyone, no tap, and it goes on in the background like the voices do. The effects are made here too.

One player per room: ffmpeg decodes the song from our cache (at the room's position, with the effects) and the
frames go to LiveKit. It follows the room's timeline: next song (blended with crossfade), skip, seek, pause, effects.
Turned on with MUSIC_IN_VOICE=1; without it (or without LiveKit) the phones play the music as before."""
import asyncio
import logging
import time
from collections import deque

from . import config, lk, music

log = logging.getLogger("voicemusic")

try:  # the LiveKit SDK for Python (a server-side participant)
    from livekit import rtc
    import numpy as np
except Exception as _e:  # pragma: no cover
    rtc = None
    np = None
    log.info("LiveKit Python SDK not available: %s", _e)

IDENTITY = "music"
RATE, CH = 48000, 2
FRAME = 960                       # 20 ms
FRAME_BYTES = FRAME * CH * 2      # 16-bit
# LiveKit's buffer. 200 ms was too little: a busy moment on the server (a download, a search) left it empty and the
# music stuttered for everyone. A skip, a seek or a pause still is heard at once: the buffer is emptied for those.
QUEUE_MS = 1000
IDLE_LEAVE = 600                  # no music this long: the player leaves the voice chat (LiveKit counts its minutes)
START_LAG = 0.15                  # ffmpeg's start: a new decoder's first sound comes about this much later
_players: dict[int, "Player"] = {}
_retry_at: dict[int, tuple[float, float]] = {}  # chat -> (don't start again before, next wait)
STATS = {"errors": 0, "last_error": "", "last_error_at": 0.0, "underruns": 0}
logging.getLogger("livekit").setLevel(logging.WARNING)  # (the SDK says a lot at INFO)


def enabled() -> bool:
    return bool(config.MUSIC_IN_VOICE) and lk.enabled() and rtc is not None


def _sig(room) -> tuple:
    c = room.current
    return (c and c["qid"], room.status, round(room.upd, 2), round(room.pos, 2), tuple(sorted(room.fx.items())))


def _filters(fx: dict) -> list[str]:
    """The room's sound effects as ffmpeg filters (the same ones the phones used to make with Web Audio)."""
    f = []
    if fx.get("bass"):
        f.append(f"bass=g={float(fx['bass']):.1f}:f=110")
    if fx.get("mid"):
        f.append(f"equalizer=f=1300:t=q:w=0.9:g={float(fx['mid']):.1f}")
    if fx.get("treble"):
        f.append(f"treble=g={float(fx['treble']):.1f}:f=7000")
    rev = float(fx.get("reverb") or 0)
    if rev > 0:
        d = min(0.9, rev)
        f.append(f"aecho=0.85:{1 - rev * 0.35:.2f}:43|71|113|167:{d * 0.5:.2f}|{d * 0.38:.2f}|{d * 0.27:.2f}|{d * 0.18:.2f}")
    if fx.get("mode") == "8d":
        f.append("apulsator=hz=0.11:amount=0.95")
    rate = float(fx.get("rate") or 1)
    if rate != 1:  # Slowed / Nightcore: speed and pitch together, on the room's clock
        f.append(f"aresample={RATE},asetrate={RATE * rate:.0f},aresample={RATE}")
    if f:
        f.append("alimiter=limit=0.95")
    return f


class _Voice:
    """One decoder (a song from a position, with the effects), read ahead into frames, with a fade in or out."""

    def __init__(self, args, fade_in: float):
        self.args = args  # the ffmpeg arguments, or a coroutine that finds them (the song may still be downloading)
        self.frames: deque[bytes] = deque()
        self.done = False
        self.started = False  # (has given frames: a gap after that is a stutter, before it the song is starting)
        self.proc = None
        self.gain, self.target, self.step = (0.0 if fade_in else 1.0), 1.0, (1.0 / max(1, fade_in / 0.02) if fade_in else 0)
        self.task = asyncio.get_running_loop().create_task(self._read())

    async def _read(self):
        try:
            if asyncio.iscoroutine(self.args):
                self.args = await self.args
            if not self.args:
                return
            self.proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *self.args,
                "-vn", "-ac", str(CH), "-ar", str(RATE), "-f", "s16le", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            while True:
                while len(self.frames) > 150:  # (3 s read ahead is plenty)
                    await asyncio.sleep(0.02)
                try:
                    data = await self.proc.stdout.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        self.frames.append(e.partial + b"\0" * (FRAME_BYTES - len(e.partial)))
                    break
                self.frames.append(data)
                self.started = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("music decoder: %s", e)
        finally:
            self.done = True

    def fade_out(self, secs: float):
        self.target = 0.0
        self.step = 1.0 / max(1, secs / 0.02)

    def finished(self) -> bool:
        return (self.done and not self.frames) or (self.target == 0.0 and self.gain <= 0.0)

    def next_frame(self):
        """This frame's samples (float, faded), or None if nothing is ready."""
        if not self.frames:
            return None
        a = np.frombuffer(self.frames.popleft(), dtype=np.int16).astype(np.float32)
        if self.gain != self.target or self.step:
            g0 = self.gain
            g1 = min(1.0, g0 + self.step) if self.target > g0 else max(0.0, g0 - self.step) if self.target < g0 else g0
            self.gain = g1
            if g0 != 1.0 or g1 != 1.0:
                a *= np.repeat(np.linspace(g0, g1, FRAME, dtype=np.float32), CH)
        return a

    def stop(self):
        if asyncio.iscoroutine(self.args):
            self.args.close()
        self.task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


async def _source_args(room, ahead=0.0) -> list[str] | None:
    """ffmpeg arguments for the room's song from where it is now. ahead: how long until what we send now is heard
    (a number, or a function asked after the waits: LiveKit's buffer then holds what it holds)."""
    c = room.current
    if room.status != "playing" or not c:
        return None
    try:
        await music.wait_cached(c["id"], 12)
        info = await music.resolve(c["id"])
        src, headers = music._piece_source(info, c["id"])
    except Exception as e:
        log.info("music: no source for %s: %s", c["id"], e)
        return None
    if room.current is not c or room.status != "playing":
        return None
    rate = float(room.fx.get("rate") or 1)
    pos = room.cur_pos() + (ahead() if callable(ahead) else ahead) * rate
    end = c.get("end") or c.get("duration") or 0
    if end and pos >= end - 0.2:
        return None
    args = []
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    args += ["-ss", f"{max(0.0, pos):.3f}", "-i", src]
    fl = _filters(room.fx)
    if fl:
        args += ["-af", ",".join(fl)]
    if end:
        args += ["-t", f"{(end - pos) / rate:.3f}"]
    return args


def _near_end(item: dict, sig) -> bool:
    """Was the song (as it was playing under sig) at its crossfade point just now?"""
    xf = float(item.get("xf") or 0)
    end = item.get("end") or item.get("duration") or 0
    if not xf or not end or not sig or sig[1] != "playing":
        return False
    rate = float(dict(sig[4]).get("rate") or 1)
    pos = sig[3] + (time.time() - sig[2]) * rate
    return end - pos <= xf + 0.6


class Player:
    def __init__(self, room):
        self.room = room
        self.lkroom = None
        self.source = None
        self.voices: list[_Voice] = []
        self.started = time.time()
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def _connect(self):
        self.lkroom = rtc.Room()
        token = lk.join_token(self.room.chat_id, IDENTITY, "Music", True)
        await self.lkroom.connect(config.LIVEKIT_URL, token)
        self.source = rtc.AudioSource(RATE, CH, queue_size_ms=QUEUE_MS)
        track = rtc.LocalAudioTrack.create_audio_track("music", self.source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO  # (music, not a voice: no voice processing)
        opts.dtx = False
        opts.red = False
        opts.audio_encoding.max_bitrate = 128000
        await self.lkroom.local_participant.publish_track(track, opts)
        log.info("music: playing into the voice chat of %s", self.room.chat_id)

    async def _change(self, old_sig, sig):
        """The room moved: next song (crossfade if the room blends), skip, seek, pause, other effects."""
        room = self.room
        prev = self._item
        same_song = bool(prev and room.current and prev["qid"] == room.current["qid"])
        playing = room.status == "playing" and room.current
        xf = 0.0
        if not same_song and prev and playing and any(not v.finished() for v in self.voices) and _near_end(prev, old_sig):
            # the room started the next song early (crossfade): the old one fades out under it, everyone hears
            # the same blend (no more "can this phone blend"). Only at the song's end: a skip in the middle of a
            # song used to blend the two for seconds too, instead of switching at once.
            xf = float(prev.get("xf") or 0)
        fade = xf or (0.08 if same_song else 0.05)
        for v in self.voices:
            v.fade_out(fade)
        if not xf and self.source and any(not v.finished() for v in self.voices):
            self.source.clear_queue()  # (a skip, a seek or a pause is heard at once, not after the buffer)
        self._item = room.current
        if playing:
            # (found in the background: waiting here for a song still downloading starved the old one's fade-out
            # and the buffer, which was heard as a stutter at every song change)
            # It's heard after what LiveKit's buffer holds when it starts (a whole second while the old song
            # blends out under it; next to nothing after a skip, which empties it: the new song comes at once)
            self.voices.append(_Voice(_source_args(room, self._ahead), fade_in=fade if (xf or same_song) else 0.02))

    def _ahead(self) -> float:
        q = getattr(self.source, "queued_duration", None) if self.source else None
        return max(START_LAG, float(q or 0))

    async def _run(self):
        self._item = None
        sig = pend = None
        empty_since = quiet_since = None
        pend_at = 0.0
        try:
            await self._connect()
            _retry_at.pop(self.room.chat_id, None)
            nxt = time.monotonic()
            while True:
                room = self.room
                if not self.lkroom.isconnected():
                    # thrown out of the voice chat (LiveKit restarted, the network gave up): join again soon
                    raise RuntimeError("left the voice chat")
                if not room.parts:
                    empty_since = empty_since or time.monotonic()
                    if time.monotonic() - empty_since > 60:  # nobody here: leave the voice chat
                        return
                else:
                    empty_since = None
                # No music for 10 minutes: leave the voice chat (LiveKit counts every minute a participant is in
                # it, also a silent one); the next song brings the player back.
                if room.status in ("playing", "loading"):
                    quiet_since = None
                else:
                    quiet_since = quiet_since or time.monotonic()
                    if time.monotonic() - quiet_since > IDLE_LEAVE and not any(not v.finished() for v in self.voices):
                        return
                s = _sig(room)
                if s != sig and sig and s[:4] == sig[:4]:
                    # Only the effects changed: while someone drags a slider they change many times a second, and
                    # each change restarted the song (it stuttered). Wait until it rests for a moment.
                    if s != pend:
                        pend, pend_at = s, time.monotonic()
                    if time.monotonic() - pend_at < 0.3:
                        s = sig
                if s != sig:
                    old, sig, pend = sig, s, None
                    await self._change(old, s)
                elif room.status == "playing" and room.current and not any(not v.done or v.frames for v in self.voices if v.target):
                    # the song's sound has all gone out; wait for the room to move on (nothing to do)
                    pass
                self.voices = [v for v in self.voices if not v.finished() or v.stop()]
                mix = None
                for v in self.voices:
                    a = v.next_frame()
                    if a is not None:
                        mix = a if mix is None else mix + a
                if mix is None and any(v.started and not v.done and v.target for v in self.voices):
                    STATS["underruns"] += 1  # a song is playing but its decoder had nothing ready: a gap
                if mix is None:
                    # Nothing to send right now (paused, between songs, a song still starting, a slow moment of
                    # its decoder): keep time, but don't fill LiveKit's buffer with silence. Silence there would
                    # put the song's sound behind the room's clock (and a skip behind a second of it).
                    nxt += 0.02
                    await asyncio.sleep(max(0.0, nxt - time.monotonic()))
                    continue
                data = np.clip(mix, -32768, 32767).astype(np.int16).tobytes()
                nxt = time.monotonic()
                await self.source.capture_frame(rtc.AudioFrame(data, RATE, CH, FRAME))  # (waits: real time)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATS["errors"] += 1
            STATS["last_error"] = f"{time.strftime('%H:%M:%S')} {e}"[:200]
            STATS["last_error_at"] = time.time()
            log.warning("music player for %s stopped: %s", self.room.chat_id, e)
            # (the phones play the music themselves meanwhile; try again later, waiting longer each time)
            # (the first retry soon: a dropped connection is usually back in a few seconds)
            wait = min(300.0, (_retry_at.get(self.room.chat_id, (0, 2.5))[1]) * 2)
            _retry_at[self.room.chat_id] = (time.time() + wait, wait)
        finally:
            for v in self.voices:
                v.stop()
            if _players.get(self.room.chat_id) is self:
                _players.pop(self.room.chat_id, None)
            try:
                if self.lkroom:
                    await self.lkroom.disconnect()
            except Exception:
                pass

    def alive(self) -> bool:
        return not self.task.done()


def ensure(room):
    """A player for this room while people are in it (started on the first join, ends a minute after the last)."""
    if not enabled() or not room.parts:
        return
    p = _players.get(room.chat_id)
    if p and p.alive():
        return
    if room.status not in ("playing", "loading"):
        return  # (nothing to play: it joins with the next song)
    if time.time() < _retry_at.get(room.chat_id, (0, 0))[0]:
        return  # (it failed a moment ago)
    _players[room.chat_id] = Player(room)


def status() -> dict:
    if not config.MUSIC_IN_VOICE:
        return {"ok": None, "detail": "Off — phones play the music themselves (set MUSIC_IN_VOICE=1 to play it in the voice chat)"}
    if rtc is None:
        return {"ok": False, "detail": "The LiveKit Python SDK is missing (pip install livekit numpy)"}
    if not lk.enabled():
        return {"ok": False, "detail": "LiveKit isn't set up"}
    n = sum(1 for p in _players.values() if p.alive())
    # (an old error doesn't keep it red: only one in the last 10 minutes with no voice chat playing now)
    recent = STATS["last_error_at"] and time.time() - STATS["last_error_at"] < 600
    return {"ok": n > 0 or not recent,
            "detail": f"On · playing in {n} voice chat{'s' if n != 1 else ''}"
            + (f" · {STATS['underruns']} gap(s) of 20 ms" if STATS["underruns"] else "")
            + (f" · {STATS['errors']} error(s), last: {STATS['last_error']}" if STATS["errors"] else "")}
