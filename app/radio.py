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
KBPS = 96
BPS = KBPS * 1000 // 8
# How far ahead of real time the sound is sent. Minimized, Telegram can stop the app's network too: then the phone
# plays only what it already has. With 4 s ahead the music stopped a few seconds after minimizing; now half a minute
# goes out at once and waits in the phone's buffer (a skip or a seek is heard that much later in the background,
# which nobody notices with the app closed).
LEAD = 30.0
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
    if _at_end(room):
        return None  # its end: silence until the room moves on
    try:
        await music.wait_cached(c["id"], 12)  # (from our disk: a YouTube link read by ffmpeg gets throttled)
        info = await music.resolve(c["id"])
        src, headers = music._piece_source(info, c["id"])
    except Exception as e:
        log.info("radio: no source for %s: %s", c["id"], e)
        return None
    if room.current is not c or room.status != "playing" or _at_end(room):
        return None  # (it changed while we waited: the caller goes round again)
    pos = room.cur_pos()  # (where the room is now, after the wait)
    end = c.get("end") or c.get("duration") or 0
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
    why = "phone left"
    log.info("radio: stream for %s starts (%d open)", getattr(request.state, "who", "?"), _open)
    proc = None
    quiet_until = 0.0  # (after a failed or empty song: silence for a moment instead of trying again at once)
    done_sig = None    # the song whose sound has all been sent: wait for the room to move on, don't send it again
    try:
        while not await request.is_disconnected():
            sig = _sig(room)
            if sig == done_sig:
                # All of this song is with the phone already (we're ahead of it); it plays it out while the room
                # gets to the end too. Nothing to send until the room moves on.
                await asyncio.sleep(0.25)
                continue
            args = await _source(room) if time.monotonic() >= quiet_until else None
            silent = args is None
            if silent and room.status == "playing" and not _at_end(room) and time.monotonic() >= quiet_until:
                quiet_until = time.monotonic() + 3  # (couldn't get the song: silence, then try again)
            # music goes out well ahead (the phone's buffer); silence only just ahead, so a pause ending or the
            # next song isn't stuck behind seconds of queued silence
            lead = 2.0 if silent else LEAD
            proc = await _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3600"] if silent else args)
            got, changed = 0, False
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
                got += len(chunk)
                sent += len(chunk)
                ahead = sent / BPS - (time.monotonic() - t0)
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                if await request.is_disconnected():
                    why = "phone left"
                    return
                # the room moved on (next song, skip, seek, pause / play): start over from where it is now; in
                # silence, also once the song can be played (it was loading, or a moment has passed)
                if _sig(room) != sig or (silent and time.monotonic() >= quiet_until and room.status == "playing"
                                         and not _at_end(room)):
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
    except asyncio.CancelledError:
        why = "phone left (cancelled)"
        raise
    except Exception as e:
        why = f"error: {e}"
        raise
    finally:
        _open -= 1
        if proc and proc.returncode is None:
            proc.kill()
        # (for the logs: how long a minimized phone kept the stream, and how much sound it got)
        log.info("radio: stream for %s ends after %.0f s, %.0f s of sound sent (%s)", getattr(request.state, "who", "?"),
                 time.monotonic() - t0, sent / BPS, why)


def _at_end(room) -> bool:
    c = room.current
    end = c and (c.get("end") or c.get("duration") or 0)
    return bool(end) and room.cur_pos() >= end - 0.3
