"""The room's music as one endless MP3 stream, for phones with Telegram minimized.

A minimized Telegram can freeze the app completely: no timers, no messages, nothing runs until it's opened again.
The song that was playing goes on (the phone's media player does that by itself), but at its end nothing starts
the next one — that took opening the app. While minimized, the player switches to this stream instead: the server
moves from song to song (and follows skips, seeks, pauses and the speed effects), the phone just plays. Opened
again, the app goes back to its own synced player.

One "station" per room makes the sound (one ffmpeg, however many phones listen); each phone gets a copy from the
moment it tunes in. It runs a little behind the room (the phone buffers), which nobody notices with the app closed."""
import asyncio
import logging
import time
from collections import deque

from . import music

log = logging.getLogger("radio")
KBPS = 96
BPS = KBPS * 1000 // 8
# How far ahead of real time the music is made and sent. Minimized, Telegram can slow the app's network down: a
# few seconds ahead stopped the music shortly after minimizing. Silence (pause, between songs) only just ahead, so
# the music coming back isn't stuck behind queued silence.
LEAD = 30.0
LEAD_SILENT = 2.0
MAX_LISTENERS = 200
_stations: dict[int, "Station"] = {}


def busy() -> bool:
    return sum(len(s.subs) for s in _stations.values()) >= MAX_LISTENERS


def _sig(room) -> tuple:
    """What the room is playing; a change means the station starts over from the room's new position."""
    c = room.current
    return (c and c["qid"], room.status, round(room.upd, 2), round(room.pos, 2), room.fx["rate"])


def _at_end(room) -> bool:
    c = room.current
    end = c and (c.get("end") or c.get("duration") or 0)
    return bool(end) and room.cur_pos() >= end - 0.3


async def _ffmpeg(args: list[str]):
    # (no ID3 tag or Xing header: each song is a new ffmpeg, and those headers in the middle of a stream are noise)
    return await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
        "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", f"{KBPS}k",
        "-id3v2_version", "0", "-write_xing", "0", "-f", "mp3", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


async def _source(room):
    """ffmpeg arguments for what's playing now, or None for silence."""
    c = room.current
    if room.status != "playing" or not c or _at_end(room):
        return None
    try:
        await music.wait_cached(c["id"], 12)  # (from our disk: a YouTube link read by ffmpeg gets throttled)
        info = await music.resolve(c["id"])
        src, headers = music._piece_source(info, c["id"])
    except Exception as e:
        log.info("radio: no source for %s: %s", c["id"], e)
        return None
    if room.current is not c or room.status != "playing" or _at_end(room):
        return None  # (it changed while we waited: go round again)
    pos = room.cur_pos()  # (where the room is now, after the wait)
    end = c.get("end") or c.get("duration") or 0
    rate = room.fx.get("rate") or 1.0
    log.info("radio: %s from %.1f s%s", c["id"], pos, f" at x{rate}" if rate != 1 else "")
    args = []
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    args += ["-ss", f"{max(0.0, pos):.2f}", "-i", src]
    if rate != 1:
        # Slowed / Nightcore: faster or slower with the pitch, like the app plays it (the room's clock runs at it)
        args += ["-af", f"aresample=44100,asetrate={44100 * rate:.0f},aresample=44100"]
    if end:
        args += ["-t", f"{(end - pos) / rate:.2f}"]
    return args


class Station:
    def __init__(self, room):
        self.room = room
        self.subs: set[asyncio.Queue] = set()
        self.recent: deque[tuple[float, bytes]] = deque()  # (where in the stream it starts, s; bytes)
        self.t0 = time.monotonic()
        self.made = 0  # bytes made so far
        self.task = asyncio.get_running_loop().create_task(self._run())

    def now(self) -> float:
        return time.monotonic() - self.t0

    def tune_in(self) -> asyncio.Queue:
        """A new listener: from what's playing right now on, plus what's already made ahead."""
        q: asyncio.Queue = asyncio.Queue()
        start = self.now() - 0.5
        for ts, chunk in self.recent:
            if ts >= start:
                q.put_nowait(chunk)
        self.subs.add(q)
        return q

    def _emit(self, chunk: bytes):
        ts = self.made / BPS
        self.made += len(chunk)
        self.recent.append((ts, chunk))
        cut = self.now() - 2
        while self.recent and self.recent[0][0] < cut:
            self.recent.popleft()
        for q in list(self.subs):
            if q.qsize() > 4000:  # a phone that stopped reading: let it go
                self.subs.discard(q)
                q.put_nowait(None)
            else:
                q.put_nowait(chunk)

    async def _run(self):
        room, proc = self.room, None
        quiet_until, done_sig, empty_since = 0.0, None, None
        try:
            while True:
                if not self.subs:
                    empty_since = empty_since or time.monotonic()
                    if time.monotonic() - empty_since > 20:  # nobody's listening any more
                        return
                    await asyncio.sleep(0.5)  # (someone may tune in again in a moment: keep the station)
                    continue
                empty_since = None
                sig = _sig(room)
                if sig == done_sig:
                    # All of this song is made (we're ahead of the room): nothing until the room moves on
                    await asyncio.sleep(0.25)
                    continue
                args = await _source(room) if time.monotonic() >= quiet_until else None
                silent = args is None
                if silent and room.status == "playing" and not _at_end(room) and time.monotonic() >= quiet_until:
                    quiet_until = time.monotonic() + 3  # (couldn't get the song: silence, then try again)
                lead = LEAD_SILENT if silent else LEAD
                proc = await _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3600"] if silent else args)
                got, changed = 0, False
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        break
                    self._emit(chunk)
                    got += len(chunk)
                    ahead = self.made / BPS - self.now()
                    if ahead > lead:
                        await asyncio.sleep(ahead - lead)
                    if _sig(room) != sig or (silent and time.monotonic() >= quiet_until and room.status == "playing"
                                             and not _at_end(room)) or not self.subs:
                        changed = True
                        break
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                proc = None
                if not silent and not changed:
                    if got < 4096:
                        quiet_until = time.monotonic() + 3  # nothing came out (a bad link): don't spin
                    else:
                        done_sig = sig
                if self.made / BPS < self.now():  # (fell behind real time: carry on from now, not from the past)
                    self.made = int(self.now() * BPS)
        except Exception:
            log.exception("radio station")
        finally:
            if proc and proc.returncode is None:
                proc.kill()
            if _stations.get(room.chat_id) is self:
                _stations.pop(room.chat_id, None)
            for q in list(self.subs):
                q.put_nowait(None)


async def stream(room, request, still_allowed):
    """One phone's copy of the room's station. still_allowed(): checked every minute (banned, left the group)."""
    st = _stations.get(room.chat_id)
    if not st or st.task.done():
        st = _stations[room.chat_id] = Station(room)
    q = st.tune_in()
    who = getattr(request.state, "who", "?")
    t0, got, why = time.monotonic(), 0, "phone left"
    checked = time.monotonic()
    log.info("radio: %s tunes in (%d listening)", who, len(st.subs))
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), 2)
            except asyncio.TimeoutError:
                chunk = b""
            if chunk is None:
                why = "station stopped"
                return
            if chunk:
                got += len(chunk)
                yield chunk
            if await request.is_disconnected():
                return
            if time.monotonic() - checked > 60:
                checked = time.monotonic()
                if not await still_allowed():
                    why = "not allowed any more"
                    return
    except asyncio.CancelledError:
        why = "phone left (cancelled)"
        raise
    finally:
        st.subs.discard(q)
        # (for the logs: how long a minimized phone kept the stream, and how much sound it got)
        log.info("radio: %s tunes out after %.0f s, %.0f s of sound (%s)", who, time.monotonic() - t0, got / BPS, why)
