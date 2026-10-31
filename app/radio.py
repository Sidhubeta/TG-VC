"""The room's music as one endless MP3 stream, for phones with Telegram minimized.

A minimized Telegram can freeze the app completely: no timers, no messages, nothing runs until it's opened again.
The song that was playing goes on (the phone's media player does that by itself), but at its end nothing starts
the next one — that took opening the app. While minimized, the player switches to this stream instead: the server
moves from song to song (and follows skips, seeks and pauses), the phone just plays. Opened again, the app goes
back to its own synced player.

It runs a little behind the room (the phone buffers a few seconds), which nobody notices with the app closed."""
import asyncio
import logging
import time

from . import music

log = logging.getLogger("radio")
KBPS = 128
BPS = KBPS * 1000 // 8
LEAD = 4.0          # seconds of sound sent ahead of real time (the phone's buffer)
MAX_STREAMS = 60
_open = 0


def busy() -> bool:
    return _open >= MAX_STREAMS


def _sig(room) -> tuple:
    """What the room is playing; a change means the stream starts over from the room's new position."""
    c = room.current
    return (c and c["qid"], room.status, round(room.upd, 2), round(room.pos, 2), room.fx["rate"])


async def _ffmpeg(args: list[str]):
    return await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
        "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", f"{KBPS}k", "-f", "mp3", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


async def _source(room):
    """ffmpeg arguments for what's playing now, or None for silence."""
    c = room.current
    if room.status != "playing" or not c:
        return None
    pos = room.cur_pos()
    end = c.get("end") or c.get("duration") or 0
    if _at_end(room):
        return None  # its end: silence until the room moves on
    try:
        info = await music.resolve(c["id"])
        src, headers = music._piece_source(info, c["id"])
    except Exception as e:
        log.info("radio: no source for %s: %s", c["id"], e)
        return None
    log.info("radio: %s from %.1f s", c["id"], pos)
    args = []
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    args += ["-ss", f"{max(0.0, pos):.2f}", "-i", src]
    if end:
        args += ["-t", f"{end - pos:.2f}"]
    return args


async def stream(room, request):
    global _open
    _open += 1
    t0, sent = time.monotonic(), 0
    proc = None
    quiet_until = 0.0  # (after a failed or empty song: silence for a moment instead of trying again at once)
    try:
        while not await request.is_disconnected():
            sig = _sig(room)
            args = await _source(room) if time.monotonic() >= quiet_until else None
            silent = args is None
            if silent and room.status == "playing" and not _at_end(room) and time.monotonic() >= quiet_until:
                quiet_until = time.monotonic() + 3  # (couldn't get the song: silence, then try again)
            proc = await _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3600"] if silent else args)
            got = 0
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
                got += len(chunk)
                sent += len(chunk)
                ahead = sent / BPS - (time.monotonic() - t0)
                if ahead > LEAD:
                    await asyncio.sleep(ahead - LEAD)
                if await request.is_disconnected():
                    return
                # the room moved on (next song, skip, seek, pause / play): start over from where it is now; in
                # silence, also once the song can be played (it was loading, or a moment has passed)
                if _sig(room) != sig or (silent and time.monotonic() >= quiet_until and room.status == "playing"
                                         and not _at_end(room)):
                    break
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            proc = None
            if not silent and got < 4096 and _sig(room) == sig:
                quiet_until = time.monotonic() + 3  # nothing came out (a bad link): don't spin
    finally:
        _open -= 1
        if proc and proc.returncode is None:
            proc.kill()


def _at_end(room) -> bool:
    c = room.current
    end = c and (c.get("end") or c.get("duration") or 0)
    return bool(end) and room.cur_pos() >= end - 0.3
