"""Realtime room engine: participants, roles, chat, music queue sync, admin tools, logs."""
import asyncio
import json
import logging
import math
import random
import re
import time
from collections import deque

from starlette.websockets import WebSocket

from . import auth, badges, db, lk, music, pin, tg

log = logging.getLogger("room")
ROOMS: dict[int, "Room"] = {}
SOUNDS = {"airhorn", "applause", "drumroll", "rimshot", "bell", "whoosh", "laugh", "wow"}
REACTIONS = {"👍", "❤️", "😂", "🔥", "😮", "😢", "👏", "🎉"}
# floating reactions over the room (like Telegram's live reactions)
FLY = {"👍", "❤️", "🔥", "😂", "😍", "🤩", "👏", "🎉", "😮", "😢", "🙏", "💯", "🥳", "😎", "🤣", "💔"}
FX_PRESETS = {"normal", "bass", "8d", "hall", "vocal", "slowed", "nightcore", "custom"}
# ---- music permissions ("music auth") ----
# What everyone may do by default; admins can change these and give chosen people ("music DJs") their own set.
MUSIC_PERMS = {
    "add": "Add songs",
    "next": "Play a song next (jump the queue)",
    "vote": "Vote to skip",
    "skip_own": "Skip songs they added",
    "playback": "Play, pause, seek, previous and skip any song",
    "queue": "Change the queue (remove, move, play now, shuffle, clear)",
    "modes": "Repeat and autoplay",
    "fx_own": "Sound effects on their own songs",
    "fx": "Sound effects on any song",
    "video": "Turn on video for everyone",
    "stop": "Stop the music",
}
MUSIC_DEFAULT = {"add": True, "next": False, "vote": True, "skip_own": True, "playback": False, "queue": False,
                 "modes": False, "fx_own": True, "fx": False, "video": True, "stop": False}
DENY = {"add": "add songs", "next": "play songs next", "vote": "vote to skip", "skip_own": "skip songs",
        "playback": "control playback", "queue": "change the queue", "modes": "change repeat and autoplay",
        "fx_own": "change the sound", "fx": "change the sound", "video": "turn on video for everyone",
        "stop": "stop the music"}
CHAT_KEEP = 48 * 3600  # recent messages are kept 48 hours; admins can read all of them back
REJOIN_GRACE = 45  # a dropped connection that comes back this fast is not "leaving" (chat history stays)
# A phone on weak internet (or one that minimizes Telegram for a moment) drops its connection and comes back a few
# seconds later. Each drop used to be a full leave + join: join/leave sounds for everyone, a new row in the logs,
# the "No username" alert again, and "Rapid join/leave" after four. Now someone stays in the room this long after
# their connection drops; coming back in time is just a reconnect.
LINGER = 15
QUEUE_MAX = 500
MOD_ACTIONS = {"admin.mute", "admin.unmute", "admin.role", "admin.kick", "admin.clearflags"}  # what moderators may do
XFADE = 4.0  # the next song starts this many seconds before the end (phones with crossfade on blend the two)
EMPTY_GRACE = 600  # keep the music running this long after the last person leaves (minimized apps reconnect)
FX_DEFAULT = {"preset": "normal", "bass": 0, "mid": 0, "treble": 0, "reverb": 0.0, "rate": 1.0, "mode": "none"}
DEFAULT_SETTINGS = {"title": "", "description": "", "locked": False, "new_can_speak": True,
                    "music_admin_only": False, "soundboard": True, "join_sound": True}


def now() -> float:
    return time.time()


_bg: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Fire-and-forget task that can't be garbage-collected mid-run and logs its errors."""
    t = asyncio.create_task(coro)
    _bg.add(t)

    def done(task: asyncio.Task):
        _bg.discard(task)
        if not task.cancelled() and task.exception():
            log.warning("background task failed: %r", task.exception())
    t.add_done_callback(done)
    return t


class Participant:
    def __init__(self, uid: int, user: dict, admin: bool):
        self.uid = uid
        self.name = user.get("name") or "User"
        self.username = user.get("username") or ""
        self.photo = f"/api/avatar/{uid}"
        self.bio = user.get("bio") or ""
        self.admin = admin
        self.role = "speaker"
        self.mic = False
        self.force_muted = False
        self.hand = 0.0
        self.video = False
        self.screen = False
        self.as_group = False
        self.joined = now()
        self.chat_since = self.joined  # members read back the chat from here (this visit)
        self.conns: set["Conn"] = set()
        self.session_id = 0
        self.mic_since = 0.0
        self.mic_secs = 0.0
        self.flags: list[str] = []
        self.last_sound = 0.0
        self.fly_times: deque[float] = deque(maxlen=6)


# Problems reported by phones (music errors, stalls, blocked sound, script errors): logged, and the latest ones
# are shown on the admins' Health page.
DIAG: deque[dict] = deque(maxlen=300)


def _device(ua: str) -> str:
    ua = ua or ""
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
           else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "?")
    m = re.search(r"Android (\d+)|OS (\d+)_", ua)
    ver = next((g for g in (m.groups() if m else ()) if g), "")
    app = "Telegram" if "Telegram" in ua else "WebView" if "; wv)" in ua else "browser"
    return f"{os_} {ver} {app}".replace("  ", " ").strip()


def _song_key(title: str) -> str:
    """A song's name without the upload's extras: "Kya Baat Ay (Official Video) | Harrdy Sandhu" and
    "Kya Baat Ay - Lyrics" are the same song."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", (title or "").lower())
    t = re.split(r"\s[|•]\s|\s-\s(?:lyrics?|official|full|audio|video)", t)[0]
    t = re.sub(r"\b(official|video|lyrics?|lyrical|audio|full song|hd|4k|song)\b", " ", t)
    return re.sub(r"[^\w]+", "", t)[:30]


def pid_of(chat_id: int, uid: int) -> int:
    """Stand-in id for someone appearing as the group: the same every time (their reactions stay theirs), never a
    real Telegram id or chat id, and not something a member can turn back into the person."""
    return -(10 ** 13 + int(auth._hmac(f"pid:{chat_id}:{uid}", 12), 16) % 10 ** 12)


def _dumps(msg: dict) -> str:
    # allow_nan=False: a NaN (or Infinity) would reach the phones as bare NaN, which their JSON.parse rejects, and
    # every music update after it was dropped. Numbers are cleaned where they come in; this makes sure.
    return json.dumps(msg, ensure_ascii=False, allow_nan=False)


def _dur(v) -> float:
    """A song length from outside: a real number of seconds, else 0 (unknown)."""
    try:
        d = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return d if math.isfinite(d) and 0 < d < 86400 else 0.0


class Conn:
    """One app connection. Messages go through its own queue and writer, so a person on a slow network only
    delays their own updates; broadcasts used to wait on each socket in turn, slowing the whole room down."""
    MAX_QUEUE = 400

    def __init__(self, ws: WebSocket, uid: int):
        self.ws = ws
        self.uid = uid
        try:
            self.device = _device(ws.headers.get("user-agent", ""))
        except Exception:
            self.device = "?"
        self.hits: deque[float] = deque(maxlen=40)
        self.xfade = False  # this phone blends songs (crossfade on, and it can): see Room._blend_ok
        self.bg = False     # the app is minimized: see bg_tick_loop
        self.alive = True
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self._writer = asyncio.create_task(self._write())

    async def send(self, msg: dict):
        await self.send_text(_dumps(msg))

    async def send_text(self, text: str):
        if not self.alive:
            return
        if self.q.qsize() >= self.MAX_QUEUE:
            # This phone can't keep up. Drop the connection; the app reconnects and gets a fresh state.
            self.alive = False
            spawn(self._drop())
            return
        self.q.put_nowait(text)

    async def _write(self):
        try:
            while True:
                text = await self.q.get()
                await self.ws.send_text(text)
                self.q.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.alive = False

    async def _drop(self):
        try:
            await self.ws.close(1013)
        except Exception:
            pass

    async def close(self, code: int = 1000):
        """Send what's queued (e.g. "you were removed"), then close."""
        if self.alive:
            try:
                await asyncio.wait_for(self.q.join(), 1.5)
            except Exception:
                pass
        self.alive = False
        try:
            await self.ws.close(code)
        except Exception:
            pass

    def stop(self):
        self.alive = False
        self._writer.cancel()


class Room:
    def __init__(self, chat_id: int, group: dict):
        self.chat_id = chat_id
        self.settings = {**DEFAULT_SETTINGS, **group["settings"]}
        ev = self.settings.get("music_everyone") if isinstance(self.settings.get("music_everyone"), dict) else {}
        if "music_everyone" not in group["settings"] and self.settings.get("music_admin_only"):
            ev = {"add": False}  # old "only admins add songs" switch
        self.settings["music_everyone"] = {**MUSIC_DEFAULT, **{k: bool(v) for k, v in ev.items() if k in MUSIC_DEFAULT}}
        if not isinstance(self.settings.get("music_dj"), dict):
            self.settings["music_dj"] = {}
        if not isinstance(self.settings.get("mods"), dict):
            self.settings["mods"] = {}  # moderators: {uid: {"name"}} — can mute / move / remove people, nothing else
        if not self.settings["title"]:
            self.settings["title"] = group.get("title") or "Voice Chat"
        self.group_title = group.get("title") or "Group"
        self.parts: dict[int, Participant] = {}
        self.chat: deque[dict] = deque(maxlen=300)
        self.join_times: deque[float] = deque(maxlen=50)
        self.user_joins: dict[int, deque] = {}
        self.rec = {"on": False, "by": None, "since": 0, "name": ""}
        # music
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.status = "stopped"  # playing | paused | stopped
        self.pos = 0.0           # position at self.upd
        self.upd = now()
        self.loop = "off"        # off | one | all
        self.autoplay = False
        self.votes: set[int] = set()
        self.recent: deque[str] = deque(maxlen=300)
        self._auto_seen: deque[str] = deque(maxlen=500)  # every song autoplay ever lined up (played or not)
        self._auto_names: deque[str] = deque(maxlen=500)
        self.history: deque[dict] = deque(maxlen=50)  # played tracks, for "previous"
        self._left: dict[int, tuple[float, float]] = {}
        self._spotify_busy: dict[int, bool] = {}  # uid -> (left at, chat_since) for quick reconnects
        # sound effects are shared: everyone hears the same effect, in sync (speed changes the timeline)
        self.fx: dict = dict(FX_DEFAULT)
        self.fx_by_admin = False
        self._timer: asyncio.Task | None = None
        self._qid = 0
        self._lock = asyncio.Lock()
        self._idle: asyncio.Task | None = None
        # admin decisions survive leaving and rejoining: {uid: {"force_muted": bool, "role": str}}
        self.sticky: dict[int, dict] = {}
        self._lingering: dict[int, asyncio.Task] = {}   # uid -> pending leave after a dropped connection
        self._alerted: dict[tuple[int, str], float] = {}  # (uid, flag) -> when admins were last told
        self._watching: set[int] = set()                # qids with a _watch_song task
        self._idle_paused = False                       # the music was paused because the room emptied
        self._prefetched = ""                           # the next song we last asked the cache for
        self._filling = False                           # autoplay is looking for songs
        self._bad: deque[str] = deque(maxlen=200)       # songs that couldn't be played (skipped)
        self._load_seek: int | None = None              # qid that was seeked while still loading
        self.last_active = now()
        self._qsig: tuple | None = None                 # the queue as last sent to everyone
        self._next_on_resume = False                    # restored with the song over: start the next on resume
        self._as_group_by: dict[int, int] = {}          # qid -> real uid of who added it as the group
        self._save_again = False
        self._gave_up: dict[int, set[int]] = {}         # qid -> people whose phone gave up on that song
        self._probing: set[int] = set()
        self._auto_skips: list[float] = []              # when songs were skipped because nobody could play them
        self._block_told = 0.0

    # ---------------- helpers -----------------
    def is_mod(self, p: Participant | None) -> bool:
        return bool(p) and not p.admin and str(p.uid) in self.settings["mods"]

    def may_publish(self, uid: int, admin: bool) -> bool:
        """For a voice token: what this person may do now, including an admin's earlier mute or "listener"."""
        if admin:
            return True
        p = self.parts.get(uid)
        if p:
            return self.can_speak(p)
        st = self.sticky.get(uid) or {}
        if st.get("force_muted"):
            return False
        return st.get("role", "speaker" if self.settings["new_can_speak"] else "listener") == "speaker"

    def can_speak(self, p: Participant) -> bool:
        if p.force_muted:
            return False
        return p.admin or p.role == "speaker"

    def staff(self, q: Participant) -> bool:
        return q.admin or self.is_mod(q)

    # ---- someone appearing "as the group" (admins only) ----
    # Members must not learn who it is. Everything a member's phone gets about them carries a stand-in id instead
    # of the Telegram id: their tile, chat messages, reactions, sounds, leaving, and their LiveKit identity.
    def pid(self, uid: int) -> int:
        return pid_of(self.chat_id, uid)

    def masked(self, p: Participant | None, viewer: Participant | None) -> bool:
        return bool(p and p.as_group and not (viewer and (viewer.admin or viewer.uid == p.uid)))

    def vis_uid(self, p: Participant, viewer: Participant | None) -> int:
        return self.pid(p.uid) if self.masked(p, viewer) else p.uid

    def real_uid(self, uid) -> int:
        """A uid sent by a phone: the stand-in id of someone appearing as the group means them."""
        try:
            uid = int(uid or 0)
        except (TypeError, ValueError):
            return 0
        if uid in self.parts:
            return uid
        return next((q.uid for q in self.parts.values() if q.as_group and self.pid(q.uid) == uid), uid)

    def lk_id(self, uid: int) -> str:
        p = self.parts.get(uid)
        return str(self.pid(uid) if p and p.as_group else uid)

    def pub(self, p: Participant, for_admin: bool, mod_view: bool = False, viewer_uid: int | None = None) -> dict:
        hide = p.as_group and not for_admin and viewer_uid != p.uid
        d = {"uid": self.pid(p.uid) if hide else p.uid, "name": p.name, "username": p.username, "photo": p.photo, "bio": p.bio, "admin": p.admin,
             "role": p.role, "mic": p.mic, "forceMuted": p.force_muted, "canSpeak": self.can_speak(p),
             "hand": p.hand, "video": p.video, "screen": p.screen, "joined": p.joined, "asGroup": p.as_group,
             "mod": self.is_mod(p)}
        if p.as_group:
            d.update(name=self.group_title, username="", photo=f"/api/avatar/{self.chat_id}", bio="",
                     lk=self.pid(p.uid))  # (their LiveKit identity, for matching their voice to their tile)
            if for_admin:
                d["realName"] = p.name
        if for_admin or mod_view:
            d["flags"] = p.flags  # moderators handle warnings too
        return d

    def chat_view(self, m: dict, viewer: Participant) -> dict:
        if not m.get("asg") or viewer.admin or viewer.uid == m["uid"]:
            return m
        return {**m, "uid": self.pid(m["uid"])}

    def pub_for(self, p: Participant, viewer: Participant) -> dict:
        return self.pub(p, viewer.admin, self.is_mod(viewer), viewer.uid)

    async def bcast_masked(self, p: Participant, msg: dict):
        """A message with p's uid in it: members get the stand-in id if p appears as the group."""
        if not p.as_group:
            return await self.broadcast(msg)
        for q in list(self.parts.values()):
            m = {**msg, "uid": self.vis_uid(p, q)}
            for c in list(q.conns):
                await c.send(m)

    async def send_to(self, uid: int, msg: dict):
        p = self.parts.get(uid)
        if p:
            for c in list(p.conns):
                await c.send(msg)

    async def broadcast(self, msg: dict, admins_only: bool = False):
        text = _dumps(msg)  # once, not once per phone (the queue alone can be big)
        for p in list(self.parts.values()):
            if admins_only and not (p.admin or self.is_mod(p)):  # moderators get the alerts too (hands, warnings)
                continue
            for c in list(p.conns):
                await c.send_text(text)

    async def bcast_part(self, p: Participant, t: str = "p.upd"):
        views: dict[tuple, str] = {}
        for q in list(self.parts.values()):
            if t == "p.join" and q.uid == p.uid:
                continue
            key = (q.admin, self.is_mod(q), q.uid if q.uid == p.uid else None)
            if key not in views:
                views[key] = _dumps({"t": t, "p": self.pub(p, *key)})
            for c in list(q.conns):
                await c.send_text(views[key])

    def room_state(self) -> dict:
        rec = self.rec
        if rec.get("by") and (q := self.parts.get(rec["by"])) and q.as_group:
            rec = {**rec, "by": self.pid(q.uid)}  # (recording as the group: the stand-in id)
        return {"settings": self.settings, "rec": rec, "count": len(self.parts)}

    def mine(self, item: dict | None, p: Participant) -> bool:
        """Did p add this song (under their own id, or the stand-in one when they appear as the group)?"""
        return bool(item) and item.get("byUid") in (p.uid, self.pid(p.uid))

    def music_state(self) -> dict:
        need = max(1, (len(self.parts) + 1) // 2)
        return {"queue": self.queue, "current": self.current, "status": self.status, "pos": self.pos,
                "upd": self.upd, "loop": self.loop, "autoplay": self.autoplay, "votes": len(self.votes),
                "need": need, "fx": self.fx, "rate": self.fx["rate"], "xfade": XFADE}

    async def push_music(self):
        # The queue goes out only when it changed. Most updates (a vote, the song's silence info, crossfade being
        # set up every few seconds) used to carry the whole queue — up to hundreds of songs — to every phone.
        st = self.music_state()
        sig = tuple((q["qid"], q.get("title"), q.get("duration")) for q in self.queue)
        if sig == self._qsig:
            st.pop("queue")
            st["queueSame"] = True
        self._qsig = sig
        await self.broadcast({"t": "music", "m": st, "now": now()})
        # whatever is next now (after a move, play now, shuffle, remove…) starts downloading, so it can start
        # straight away and crossfade
        nxt = self.queue[0]["id"] if self.queue else ""
        if nxt and nxt != self._prefetched:
            self._prefetched = nxt
            spawn(music.prefetch(nxt))
        spawn(self._save_soon())

    async def toast(self, uid: int, text: str):
        await self.send_to(uid, {"t": "toast", "text": text})

    # ---------------- join / leave -----------------
    async def join(self, conn: Conn, user: dict, admin: bool, as_group: bool):
        uid = conn.uid
        p = self.parts.get(uid)
        first = p is None
        self.last_active = now()
        if (t := self._lingering.pop(uid, None)) and not t.done():
            t.cancel()  # back within LINGER: the same visit carries on
        if self._idle_paused and (self.status == "paused" or self._next_on_resume):
            # the music was paused only because everyone had gone: carry on now someone is back (a member
            # without playback rights couldn't start it again, so it stayed off until an admin came)
            await self._resume_after_idle()
        if first:
            p = Participant(uid, user, admin)
            p.as_group = as_group and admin
            if not admin and not self.settings["new_can_speak"]:
                p.role = "listener"
            if not admin and uid in self.sticky:
                p.force_muted = self.sticky[uid].get("force_muted", False)
                p.role = self.sticky[uid].get("role", p.role)
            self.parts[uid] = p
            prev = self._left.pop(uid, None)
            if prev and now() - prev[0] < REJOIN_GRACE:  # only the connection dropped: keep their chat
                p.chat_since = prev[1]
            if not admin:
                self._detect(p)
            p.session_id = await db.x(
                "INSERT INTO sessions(chat_id,uid,name,username,joined,last_seen) VALUES(?,?,?,?,?,?)",
                (self.chat_id, uid, p.name, p.username, p.joined, p.joined))
            badges.joined(self.chat_id, uid)
        p.admin = admin
        toggled = not first and p.as_group != (as_group and admin)
        if toggled:
            # Switched "appear as the group" on or off: the app reconnects within a moment, while it still counts
            # as here (LINGER) — so the switch is made on the same participant (it used to be ignored).
            seen_as = {q.uid: self.vis_uid(p, q) for q in self.parts.values() if q.uid != uid}
            p.as_group = as_group and admin
        p.conns.add(conn)
        if self._idle:
            self._idle.cancel()
            self._idle = None
        everyone = [self.pub(q, admin, self.is_mod(p), uid) for q in self.parts.values()]
        # Messages pop up and disappear, like Telegram's voice chat. Everyone can read back what was said since
        # they joined (leaving and coming back starts over); admins can read back the last 48 hours.
        since = now() - CHAT_KEEP if admin else max(p.chat_since, now() - CHAT_KEEP)
        await conn.send({"t": "init", "me": uid, "now": now(), "parts": everyone, "room": self.room_state(),
                         "music": self.music_state(), "chat": [self.chat_view(m, p) for m in self.chat if m["ts"] >= since],
                         "admin": admin,
                         "sudo": bool(getattr(conn, "sudo", False)),  # (bot owner / sudo user: sees the Health tab)
                         "owner": bool(getattr(conn, "owner", False)),  # (the bot's owner: manages sudo users)
                         "group": self.group_title,
                         # a fresh session: one opened 12 hours ago would otherwise expire (and the song links with it)
                         "session": auth.make_session(uid, self.chat_id),
                         **({"pid": self.pid(uid)} if p.as_group else {}),
                         # appearing as the group (or just stopped): a LiveKit token under the identity to use now
                         # (the one from login has the real id); the app joins the voice again if it changed
                         **({"livekit": lk.join_info(self.chat_id, int(self.lk_id(uid)), self.can_speak(p)),
                             "lkid": int(self.lk_id(uid))} if (p.as_group or toggled) and lk.enabled() else {})})
        if toggled:
            # everyone else: the old tile goes and the new one comes (its id may have changed)
            for q in list(self.parts.values()):
                if q.uid == uid:
                    continue
                now_as = self.vis_uid(p, q)
                for c in list(q.conns):
                    if seen_as.get(q.uid, now_as) != now_as:
                        await c.send({"t": "p.leave", "uid": seen_as[q.uid], "quiet": True})
                    await c.send({"t": "p.join" if seen_as.get(q.uid, now_as) != now_as else "p.upd",
                                  "p": self.pub_for(p, q), "quiet": True})
        if first:
            await self.bcast_part(p, "p.join")
            await self.broadcast({"t": "room", "room": self.room_state()})
            pin.schedule(self.chat_id)
            # each warning once in a while, not on every reconnect
            new = [f for f in p.flags if now() - self._alerted.get((uid, f), 0) > 6 * 3600]
            for f in new:
                self._alerted[(uid, f)] = now()
            if len(self._alerted) > 2000:
                self._alerted = {k: v for k, v in self._alerted.items() if now() - v < 6 * 3600}
            if new:
                await self.broadcast({"t": "alert", "uid": uid, "name": p.name, "flags": new}, admins_only=True)
                if "Joined during a join flood" in new:
                    spawn(tg.alert_admins(self.chat_id, "flood",
                        f"⚠️ Many people are joining the voice chat at once ({len(self.parts)} now). Check the Safety tab."))

    async def leave(self, conn: Conn, at_once: bool = False):
        p = self.parts.get(conn.uid)
        if not p:
            return
        p.conns.discard(conn)
        if p.conns or self.parts.get(conn.uid) is not p:
            return
        if at_once:
            return await self._gone(p)
        # The connection dropped: give them LINGER seconds to come back before they count as gone.
        old = self._lingering.get(p.uid)
        if not old or old.done():
            self._lingering[p.uid] = spawn(self._linger(p))

    async def _linger(self, p: Participant):
        await asyncio.sleep(LINGER)
        self._lingering.pop(p.uid, None)
        if not p.conns and self.parts.get(p.uid) is p:
            await self._gone(p)

    async def _resume_after_idle(self):
        self._idle_paused = False
        if self._next_on_resume:  # (restored after a restart, and the song had ended meanwhile)
            self._next_on_resume = False
            if self.queue:
                await self._start(self.queue.pop(0))
            return
        cur = self.current
        if not cur:
            return
        if music.is_ready(cur["id"]):
            self.status, self.upd = "playing", now()
            self._arm_timer()
            spawn(self.push_music())
            if not cur.get("duration"):
                spawn(self._fill_duration(cur))
        else:
            # After a restart nothing is looked up yet: wait for the stream like a new song does ("loading"),
            # then carry on from where it was. (It used to start the clock at once while phones were still
            # finding the stream, so everyone came in late.)
            self.status, self._load_seek, self.upd = "loading", cur["qid"], now()
            spawn(self._when_ready(cur["qid"], cur["id"]))
            if not cur.get("duration"):
                spawn(self._fill_duration(cur))
            spawn(self.push_music())

    async def _gone(self, p: Participant):
        if (t := self._lingering.pop(p.uid, None)) and not t.done() and t is not asyncio.current_task():
            t.cancel()
        if self.parts.get(p.uid) is not p:
            return
        # Remove from the room before any await, so a quick rejoin creates a fresh participant
        # instead of being dropped by this (late) leave.
        self.parts.pop(p.uid, None)
        self.last_active = now()
        self._left[p.uid] = (now(), p.chat_since)
        if len(self._left) > 500:
            cut = now() - REJOIN_GRACE
            self._left = {k: v for k, v in self._left.items() if v[0] >= cut}
        self.votes.discard(p.uid)
        self._mic_flush(p)
        t = now()
        await db.x("UPDATE sessions SET left=?, last_seen=?, mic_secs=?, open=0 WHERE id=?",
                   (t, t, p.mic_secs, p.session_id))
        if self.rec["on"] and self.rec["by"] == p.uid:
            self.rec = {"on": False, "by": None, "since": 0, "name": ""}
        await self.bcast_masked(p, {"t": "p.leave", "uid": p.uid})
        await self.broadcast({"t": "room", "room": self.room_state()})
        pin.schedule(self.chat_id)
        if not self.parts and not self._idle:
            # Nobody left. Don't pause straight away: a phone that minimizes Telegram drops its connection
            # for a moment, and pausing made the song jump back to where it was when they come back.
            self._idle = spawn(self._pause_when_idle())

    async def _pause_when_idle(self):
        try:
            await asyncio.sleep(EMPTY_GRACE)
            async with self._lock:
                if not self.parts and self.status == "playing":
                    self.pos = self.cur_pos()
                    self.upd = now()
                    self.status = "paused"
                    self._idle_paused = True  # the next person to come in starts it again
                    self._cancel_timer()
        finally:
            self._idle = None

    def _mic_flush(self, p: Participant):
        if p.mic and p.mic_since:
            p.mic_secs += now() - p.mic_since
            p.mic_since = now()

    def _detect(self, p: Participant):
        t = now()
        self.join_times.append(t)
        recent = [x for x in self.join_times if t - x < 20]
        if len(recent) >= 8:
            p.flags.append("Joined during a join flood")
        dq = self.user_joins.setdefault(p.uid, deque(maxlen=10))
        dq.append(t)
        if len([x for x in dq if t - x < 120]) >= 4:
            p.flags.append("Rapid join/leave")
        if not p.username:
            p.flags.append("No username")

    async def heartbeat(self):
        cutoff = now() - CHAT_KEEP
        while self.chat and self.chat[0]["ts"] < cutoff:
            self.chat.popleft()
        parts = list(self.parts.values())
        if not parts:
            return
        t = now()
        if t - getattr(self, "_sess_at", 0) > 3600:  # keep sessions fresh for people who stay connected for hours
            self._sess_at = t
            for p in parts:
                await self.send_to(p.uid, {"t": "session", "s": auth.make_session(p.uid, self.chat_id)})
        for p in parts:
            self._mic_flush(p)
        await db.xmany("UPDATE sessions SET last_seen=?, mic_secs=? WHERE id=?",
                       [(t, p.mic_secs, p.session_id) for p in parts])
        # people who left (or were removed from) the Telegram group lose access within a minute
        sem = asyncio.Semaphore(8)

        async def check(p: Participant):
            async with sem:
                return p, await tg.member_status(self.chat_id, p.uid, fresh=True)

        for p, st in await asyncio.gather(*(check(p) for p in parts)):
            if self.parts.get(p.uid) is not p:
                continue
            if st.get("unsure"):
                continue  # Telegram didn't really answer: check again next minute
            if not st["member"]:
                await self.kick(p.uid, "You left the group, so you were removed from its voice chat.")
            elif st["admin"] != p.admin:
                p.admin = st["admin"]
                await self.bcast_part(p)

    async def kick(self, uid: int, reason: str = ""):
        p = self.parts.get(uid)
        if not p:
            return
        await self.send_to(uid, {"t": "kicked", "ban": False, "reason": reason})
        for c in list(p.conns):
            await c.close(4003)
        ident = self.lk_id(uid)
        await self._gone(p)
        await lk.remove(self.chat_id, ident)

    # ---------------- message router -----------------
    async def handle(self, conn: Conn, msg: dict):
        p = self.parts.get(conn.uid)
        if not p:
            return
        t = msg.get("t", "")
        try:
            if t == "ping":
                await conn.send({"t": "pong", "id": msg.get("id"), "now": now()})
            elif t == "bg":
                conn.bg = bool(msg.get("on"))
            elif t == "caps":
                # whether this phone blends songs; crossfade is used only when every phone does
                xf = bool(msg.get("xfade"))
                if xf != conn.xfade:
                    conn.xfade = xf
                    async with self._lock:
                        if self.status == "playing" and self.current:
                            before = self.current.get("xf")
                            self._arm_timer()
                            if self.current.get("xf") != before:
                                await self.push_music()
            elif t == "diag":
                kind, detail = str(msg.get("kind", ""))[:40], str(msg.get("detail", ""))[:300]
                cur = (self.current or {}).get("id", "-")
                # Telegram says which app it is ("ios 8.0"); iPhone's Telegram doesn't put that in its user agent
                app = re.sub(r"[^\w. -]", "", str(msg.get("app", "")))[:24]
                device = conn.device.replace(" browser", "").replace(" WebView", "").replace(" Telegram", "") + (f" · Telegram {app}" if app else "")
                log.warning("client problem [%s] %s (%s) on %s: %s | song %s %s", kind, p.name, p.uid, device, detail,
                            cur, self.status)
                DIAG.append({"ts": now(), "chat": self.chat_id, "uid": p.uid, "name": p.name, "device": device,
                             "kind": kind, "detail": detail})
                cur_ = self.current
                if kind == "music-gaveup" and cur_ and f"song={cur_['id']}" in detail and self.status == "playing":
                    gave = self._gave_up.setdefault(cur_["qid"], set())
                    gave.add(p.uid)
                    listening = len([q for q in self.parts.values() if q.conns])
                    if len(gave) >= max(1, (listening + 1) // 2) and cur_["qid"] not in self._probing:
                        self._probing.add(cur_["qid"])
                        spawn(self._check_dead_song(cur_["qid"], cur_["id"]))
            elif t == "mic":
                await self.set_mic(p, bool(msg.get("on")))
            elif t == "hand":
                p.hand = now() if msg.get("up") else 0
                await self.bcast_part(p)
                if p.hand:
                    await self.broadcast({"t": "hand", "uid": p.uid, "name": p.name}, admins_only=True)
            elif t == "video":
                p.video = bool(msg.get("on")) and self.can_speak(p)
                await self.bcast_part(p)
            elif t == "screen":
                p.screen = bool(msg.get("on")) and self.can_speak(p)
                await self.bcast_part(p)
            elif t == "chat":
                await self.on_chat(p, msg)
            elif t == "react":
                await self.on_react(p, msg)
            elif t == "chat.del":
                await self.on_chat_delete(p, msg)
            elif t == "sound":
                await self.on_sound(p, msg)
            elif t == "fly":
                await self.on_fly(p, msg)
            elif t == "report":
                await self.on_report(p, msg)
            elif t.startswith("music."):
                async with self._lock:
                    await self.on_music(p, t[6:], msg)
            elif t.startswith("admin.") or t.startswith("rec."):
                if not p.admin and not (self.is_mod(p) and t in MOD_ACTIONS):
                    return await self.toast(p.uid, "Admins only")
                await self.on_admin(p, t, msg)
        except Exception:
            log.exception("handle %s", t)

    async def set_mic(self, p: Participant, on: bool):
        if on and not self.can_speak(p):
            await self.send_to(p.uid, {"t": "mic.denied"})
            return
        if on == p.mic:
            return
        self._mic_flush(p)
        p.mic = on
        p.mic_since = now() if on else 0
        if on:
            p.hand = 0
        await self.bcast_part(p)
        pin.schedule(self.chat_id, 5)

    # ---------------- chat -----------------
    async def on_chat(self, p: Participant, msg: dict):
        text = str(msg.get("text", "")).strip()[:1000]
        if not text:
            return
        t0 = now()
        hits = getattr(p, "chat_hits", None)
        if hits is None:
            hits = p.chat_hits = deque(maxlen=8)
        if len(hits) == hits.maxlen and t0 - hits[0] < 10:
            return await self.toast(p.uid, "Slow down 🙂")  # at most 8 messages in 10 seconds
        hits.append(t0)
        reply = None
        rid = msg.get("reply")
        if rid:
            for m in self.chat:
                if m["id"] == rid:
                    reply = {"id": m["id"], "name": m["name"], "text": m["text"][:120]}
        name = self.group_title if p.as_group else p.name
        ts = now()
        mid = await db.x("INSERT INTO messages(chat_id,uid,name,text,reply,ts,asg) VALUES(?,?,?,?,?,?,?)",
                         (self.chat_id, p.uid, name, text, json.dumps(reply) if reply else None, ts, int(p.as_group)))
        m = {"id": mid, "uid": p.uid, "name": name, "photo": f"/api/avatar/{self.chat_id}" if p.as_group else p.photo, "text": text,
             "reply": reply, "reactions": {}, "ts": ts}
        if p.as_group:
            m["asg"] = True
        self.chat.append(m)
        if not p.as_group:
            return await self.broadcast({"t": "chat", "m": m})
        for q in list(self.parts.values()):  # sent "as the group": only admins (and the sender) see who it was
            for c in list(q.conns):
                await c.send({"t": "chat", "m": self.chat_view(m, q)})

    async def on_react(self, p: Participant, msg: dict):
        emoji = msg.get("emoji")
        if emoji not in REACTIONS:
            return
        for m in self.chat:
            if m["id"] == msg.get("id"):
                users = m["reactions"].setdefault(emoji, [])
                me = self.pid(p.uid) if p.as_group else p.uid  # (as the group: the stand-in id, for everyone)
                if me in users:
                    users.remove(me)
                    if not users:
                        m["reactions"].pop(emoji)
                else:
                    users.append(me)
                await db.x("UPDATE messages SET reactions=? WHERE id=?", (json.dumps(m["reactions"]), m["id"]))
                await self.broadcast({"t": "react", "id": m["id"], "reactions": m["reactions"]})
                return

    async def on_chat_delete(self, p: Participant, msg: dict):
        for m in list(self.chat):
            if m["id"] == msg.get("id") and (p.admin or self.is_mod(p) or m["uid"] == p.uid):
                self.chat.remove(m)
                await db.x("DELETE FROM messages WHERE id=?", (m["id"],))
                await self.broadcast({"t": "chat.del", "id": m["id"]})

    async def on_sound(self, p: Participant, msg: dict):
        sid = msg.get("id")
        if sid not in SOUNDS or not self.settings["soundboard"]:
            return
        if now() - p.last_sound < 4:
            return await self.toast(p.uid, "Slow down 🙂")
        p.last_sound = now()
        await self.bcast_masked(p, {"t": "sound", "id": sid, "uid": p.uid,
                                    "name": self.group_title if p.as_group else p.name})

    async def on_fly(self, p: Participant, msg: dict):
        emoji = msg.get("emoji")
        if emoji not in FLY:
            return
        t = now()
        if len(p.fly_times) == p.fly_times.maxlen and t - p.fly_times[0] < 3:
            return  # max 6 every 3 seconds
        p.fly_times.append(t)
        await self.bcast_masked(p, {"t": "fly", "emoji": emoji, "uid": p.uid,
                                    "name": self.group_title if p.as_group else p.name})

    async def on_report(self, p: Participant, msg: dict):
        target = self.parts.get(self.real_uid(msg.get("uid")))
        if not target or target.uid == p.uid:
            return
        if now() - getattr(p, "last_report", 0) < 30:
            return await self.toast(p.uid, "You just sent a report — admins will look at it")
        p.last_report = now()
        reason = str(msg.get("reason", ""))[:300]
        await db.x("INSERT INTO reports(chat_id,uid,name,by_uid,by_name,reason,ts) VALUES(?,?,?,?,?,?,?)",
                   (self.chat_id, target.uid, target.name, p.uid, p.name, reason, now()))
        flag = (f"Reported by {p.name}" + (f": {reason}" if reason else ""))[:120]
        if flag not in target.flags:
            target.flags.append(flag)
            del target.flags[:-10]  # keep the latest few
        await self.bcast_part(target)
        await self.broadcast({"t": "alert", "uid": target.uid, "name": target.name, "flags": [flag]}, admins_only=True)
        spawn(tg.alert_admins(self.chat_id, f"report:{target.uid}",
            f"🚩 <b>{tg._esc(p.name)}</b> reported <b>{tg._esc(target.name)}</b>" + (f": {tg._esc(reason)}" if reason else "")))
        await self.toast(p.uid, "Reported to admins")

    # ---------------- music -----------------
    def cur_pos(self) -> float:
        if self.status == "playing":
            return self.pos + (now() - self.upd) * self.fx["rate"]
        return self.pos

    def _cancel_timer(self):
        # Never cancel the task we are running in: when the end-of-track timer itself starts the next
        # song, cancelling it would abort the rest of _start() and the new state would reach only some people.
        if self._timer and not self._timer.done() and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    def _arm_timer(self):
        self._cancel_timer()
        if self.status != "playing" or not self.current:
            return
        dur = self.current.get("duration") or 0
        if dur <= 0:
            return
        end = self.current.get("end") or dur  # where the sound ends (a silent tail is skipped)
        nxt = self.queue[0] if self.queue else None
        # crossfade: only when the next song can start straight away (otherwise the end would just be cut off)
        left = end - self.cur_pos()
        length = end - (self.current.get("start") or 0)
        # Crossfade only when every phone in the room blends songs. The room moves on XFADE seconds early for
        # everyone; a phone that can't blend (iPhone, crossfade switched off, an effect on) used to lose the last
        # seconds of every song in a hard cut.
        blend = self._blend_ok() and self.loop != "one" and nxt and music.is_ready(nxt["id"])
        xf = min(XFADE, max(0.0, left - 0.5), length * 0.15) if blend else 0.0
        xf = round(xf, 2) if xf >= 1 else 0.0  # (re-armed every few seconds: keep the same switch point to the end)
        self.current["xf"] = xf
        remaining = max(0.5, (end - self.cur_pos()) / self.fx["rate"] + (-xf if xf else 0.8))
        qid = self.current["qid"]

        async def fire():
            await asyncio.sleep(remaining)
            async with self._lock:
                if self.current and self.current["qid"] == qid and self.status == "playing":
                    await self._next(auto=True)

        self._timer = spawn(fire())

    def _note_as_group(self, p: Participant | None) -> dict:
        if p and p.as_group:
            self._as_group_by[self._qid] = p.uid
            if len(self._as_group_by) > 2000:
                for k in list(self._as_group_by)[:500]:
                    self._as_group_by.pop(k, None)
        return {}

    def _blend_ok(self) -> bool:
        conns = [c for q in self.parts.values() for c in q.conns]
        return bool(conns) and all(c.xfade for c in conns)

    def _item(self, t: dict, p: Participant | None, by: str | None = None) -> dict | None:
        if not isinstance(t, dict):
            return None
        vid = str(t.get("id", ""))
        if not re.fullmatch(r"[\w-]{11}", vid):
            return None
        self._qid += 1
        return {"qid": self._qid, "id": vid, "title": str(t.get("title", ""))[:200],
                "artist": str(t.get("artist", ""))[:120], "duration": _dur(t.get("duration")),
                "thumb": f"/api/thumb/{vid}",
                "by": by or ((self.group_title if p.as_group else p.name) if p else ""),
                "byUid": (self.pid(p.uid) if p.as_group else p.uid) if p else 0,
                **self._note_as_group(p)}

    async def _start(self, item: dict | None, from_prev: bool = False, repeat: bool = False):
        cur = self.current
        self._idle_paused = False
        self._load_seek = None
        self._gave_up.clear()
        if cur and not from_prev and (not item or item["id"] != cur["id"]):
            self.history.append(cur)
        if (item or {}).get("qid") != (cur or {}).get("qid") and not self.fx_by_admin:
            self.fx = dict(FX_DEFAULT)  # an effect picked by a song's owner ends with their song
        self.current = item
        self.votes.clear()
        self.pos = 0.0
        self.upd = now()
        if item:
            tr = music.trim(item["id"])
            if tr:
                item["start"], item["end"] = tr
            self.pos = item.get("start") or 0.0  # start where the sound starts
            # Finding the stream on YouTube can take several seconds. The room's clock used to start at once, so
            # everyone joined the song a few seconds in (or heard nothing at first). Now it waits ("loading") and
            # starts at 0 the moment the song can be played.
            if music.is_ready(item["id"]):
                self.status = "playing"
            else:
                self.status = "loading"
                spawn(self._when_ready(item["qid"], item["id"]))
            self.recent.append(item["id"])
            spawn(music.prefetch(item["id"]))
            if item["qid"] not in self._watching:  # (repeat-one keeps the qid: one watcher, not one more per repeat)
                self._watching.add(item["qid"])
                spawn(self._watch_song(item["qid"], item["id"]))
            if not repeat:  # a song on repeat counts once in the history and badges
                t = now()
                real = self._as_group_by.get(item["qid"])  # (added as the group: the real id stays on the server)
                await db.x("INSERT INTO plays(chat_id,vid,title,artist,duration,by_uid,by_name,ts,asg) VALUES(?,?,?,?,?,?,?,?,?)",
                           (self.chat_id, item["id"], item["title"], item["artist"], item["duration"],
                            real or item["byUid"], item["by"], t, int(bool(real))))
                # what each person listens to, for their own "Made for you" recommendations
                await db.xmany("INSERT INTO listens(uid,vid,title,artist,ts) VALUES(?,?,?,?,?)",
                               [(uid, item["id"], item["title"], item["artist"], t) for uid in self.parts])
            if not item["duration"]:
                spawn(self._fill_duration(item))
            if self.autoplay and not self.queue:
                spawn(self._autoplay_fill(item["id"]))  # line up the next song now, not after this one ends
        else:
            self.status = "stopped"
        self._arm_timer()
        await self.push_music()

    async def _when_ready(self, qid: int, vid: str):
        try:
            await asyncio.wait_for(music.resolve(vid), 15)
        except asyncio.TimeoutError:  # YouTube is slow: start anyway, the phones retry on their own
            log.info("resolve before start too slow %s", vid)
        except Exception as e:
            # Private, removed, blocked here, age-restricted: skip it. (Before, the room sat on the song: with no
            # length it never ended, with one everyone heard silence.) Other failures (YouTube blocking the server
            # for a bit) start it anyway: the phones retry, and skipping would run through the whole queue.
            log.info("resolve before start failed %s: %s", vid, e)
            if music.unplayable(e):
                async with self._lock:
                    await self._skip_unplayable(qid, str(e))
                return
        try:
            await music.prefetch(vid)  # make sure the download (and with it the silence check) has started
        except Exception:
            pass
        for _ in range(8):  # up to 2 s more to learn about a silent intro, only while it's being worked out
            if music.trim(vid) or not music.trim_pending(vid) or not self.current or self.current["qid"] != qid:
                break
            await asyncio.sleep(0.25)
        async with self._lock:
            if self.current and self.current["qid"] == qid and self.status == "loading":
                tr = music.trim(vid)
                if tr:
                    self.current["start"], self.current["end"] = tr
                start = self.current.get("start") or 0.0
                # someone moved the slider while it was loading: start there
                pos = self.pos if self._load_seek == qid else start
                self._load_seek = None
                self.status, self.pos, self.upd = "playing", pos, now()
                self._arm_timer()
                await self.push_music()

    async def _check_dead_song(self, qid: int, vid: str):
        """Half the room's phones gave up on the song. Skip it only if the server can't get it either, and only
        while it's this one song: when YouTube is blocking everything, skipping would empty the queue."""
        try:
            if await music.probe(vid):
                log.info("phones gave up on %s, but the server gets it fine: not skipping", vid)
                return
            t = now()
            self._auto_skips = [x for x in self._auto_skips if t - x < 600]
            if len(self._auto_skips) >= 2 or music.youtube_blocking():
                if t - self._block_told > 600:
                    self._block_told = t
                    await self.broadcast({"t": "toast", "text": "YouTube is refusing songs right now — the music "
                                          "will come back by itself"}, admins_only=True)
                return
            self._auto_skips.append(t)
            async with self._lock:
                await self._skip_unplayable(qid, "no phone (and not the server) could get it")
        finally:
            self._probing.discard(qid)

    async def _skip_unplayable(self, qid: int, why: str = ""):
        cur = self.current
        if not cur or cur["qid"] != qid:
            return
        log.info("skipping unplayable song %s: %s", cur["id"], why[:200])
        self._bad.append(cur["id"])
        await self.broadcast({"t": "toast", "text": f"Can't play “{cur['title'][:60]}” — skipped"})
        self.current = None  # (not put back by repeat, not repeated)
        self.queue = [q for q in self.queue if q["id"] != cur["id"]]
        if self.queue:
            await self._start(self.queue.pop(0))
        else:
            await self._start(None)
            if self.autoplay:
                spawn(self._autoplay_fill(cur["id"]))

    async def _import_job(self, p: Participant, source: str, fetch):
        """Find each Spotify / SoundCloud song on YouTube and queue them in order, a few at a time (the first ones
        play while the rest are still being looked up)."""
        try:
            try:
                name, songs = await fetch()
            except Exception as e:
                log.info("%s import failed: %s", source, e)
                return await self.toast(p.uid, f"Couldn't read that {source} link — is it public?")
            if not songs:
                return await self.toast(p.uid, f"That {source} link has no songs")
            await self.toast(p.uid, f"Adding {len(songs)} song{'s' if len(songs) > 1 else ''} from “{name[:40]}”…")
            sem, found, missed = asyncio.Semaphore(4), 0, 0

            async def one(song):
                async with sem:
                    return await music.match(song)
            for i in range(0, len(songs), 4):
                if p.uid not in self.parts:  # they left: stop
                    break
                batch = [t for t in await asyncio.gather(*(one(sg) for sg in songs[i:i + 4])) if t]
                missed += len(songs[i:i + 4]) - len(batch)
                if batch:
                    found += len(batch)
                    async with self._lock:
                        await self.on_music(p, "addMany", {"tracks": batch, "quiet": True})
            await self.toast(p.uid, f"Added {found} song{'s' if found != 1 else ''} from {source}"
                             + (f" ({missed} not found on YouTube)" if missed else ""))
        finally:
            self._spotify_busy.pop(p.uid, None)

    async def _watch_song(self, qid: int, vid: str):
        """While a song plays: apply its silence info once known (skip a silent intro that's still playing, end
        before a silent tail), and switch crossfade on once the next song is ready."""
        try:
            await self._watch_loop(qid, vid)
        finally:
            self._watching.discard(qid)

    async def _watch_loop(self, qid: int, vid: str):
        trimmed = False
        for _ in range(2400):
            await asyncio.sleep(3)
            async with self._lock:
                cur = self.current
                if not cur or cur["qid"] != qid:
                    return
                changed = False
                tr = music.trim(vid)
                if tr and not trimmed:
                    trimmed = True
                    cur["start"], cur["end"] = tr
                    if self.status == "playing" and self.cur_pos() < tr[0] - 0.3:
                        self.pos, self.upd = tr[0], now()
                    changed = True
                if self.status == "playing":
                    before = cur.get("xf")
                    self._arm_timer()
                    changed = changed or cur.get("xf") != before
                if changed:
                    await self.push_music()

    async def _fill_duration(self, item: dict):
        try:
            info = await music.resolve(item["id"])
        except Exception as e:
            if music.unplayable(e) and self.status != "loading":  # (_when_ready deals with it while loading)
                async with self._lock:
                    await self._skip_unplayable(item["qid"], str(e))
            return
        d = _dur((info.get("track") or {}).get("duration"))
        async with self._lock:
            if d and self.current and self.current["qid"] == item["qid"]:
                self.current["duration"] = d
                self._arm_timer()
                await self.push_music()

    async def _next(self, auto: bool = False):
        cur = self.current
        if cur and auto and self.loop == "one":
            return await self._start({**cur}, repeat=True)
        if cur and self.loop == "all" and all(q["id"] != cur["id"] for q in self.queue):
            self._qid += 1
            self.queue.append({**cur, "qid": self._qid})
        if self.queue:
            return await self._start(self.queue.pop(0))
        if self.autoplay and cur:
            # (normally the next autoplay song is lined up already; this is when that lookup hadn't finished)
            await self._start(None)
            spawn(self._autoplay_fill(cur["id"]))
            return
        await self._start(None)

    async def _autoplay_fill(self, seed: str):
        """Line up a few songs like the last one. While a song plays they go into the queue (so the next one starts
        right away, with no "Nothing playing" gap); with nothing playing, the first one starts."""
        if self._filling:
            return
        self._filling = True
        try:
            rel: list[dict] = []
            # Songs not to suggest again. A related list overlaps a lot with the one before (each song's "mix"
            # holds the same hits), so autoplay kept bringing back the songs it had just played: everything it
            # ever lined up, what played in the last 24 hours (kept across restarts), and what's queued now.
            rows = await db.q("SELECT DISTINCT vid, title FROM plays WHERE chat_id=? AND ts>?", (self.chat_id, now() - 86400))
            skip = set(self.recent) | set(self._auto_seen) | set(self._bad) | {r["vid"] for r in rows} \
                | {q["id"] for q in self.queue} | ({self.current["id"]} if self.current else set())
            # the same song under another upload (official video, lyrics, audio) counts as the same song
            names = {_song_key(r["title"]) for r in rows} | {_song_key(q.get("title", "")) for q in self.queue} \
                | set(self._auto_names) | ({_song_key(self.current.get("title", ""))} if self.current else set())
            names.discard("")
            seeds = [seed] + [v for v in reversed(self.recent) if v != seed][:2]
            for sd in seeds:  # the related list can fail or come back empty: try the songs before it
                try:
                    rel = await music.related(sd, 50)
                except Exception as e:
                    log.info("autoplay lookup failed for %s: %s", sd, e)
                    continue
                rel = [r for r in rel if r["id"] not in skip and _song_key(r["title"]) not in names
                       and (not r["duration"] or r["duration"] < 900)]
                if rel:
                    break
            top = rel[:8]
            random.shuffle(top)  # (shuffling a slice in place would shuffle a throwaway copy)
            rel = top + rel[8:]
            async with self._lock:
                if not self.autoplay or (self.current and self.queue):
                    return
                if not rel:
                    if not self.current:
                        await self.broadcast({"t": "toast", "text": "Autoplay couldn't find more songs right now"})
                    return
                for r in rel[:3]:
                    it = self._item(r, None, by="Autoplay")
                    if it:
                        it["auto"] = True
                        self.queue.append(it)
                        self._auto_seen.append(it["id"])
                        self._auto_names.append(_song_key(it["title"]))
                if not self.current:
                    await self._start(self.queue.pop(0))
                else:
                    self._arm_timer()  # (the next song is known now: crossfade can be set up)
                    await self.push_music()
        finally:
            self._filling = False

    def can(self, p: Participant, perm: str) -> bool:
        if p.admin:
            return True
        dj = self.settings["music_dj"].get(str(p.uid))
        if dj and perm in dj.get("perms", {}):
            return bool(dj["perms"][perm])
        return bool(self.settings["music_everyone"].get(perm, False))

    async def deny(self, p: Participant, perm: str):
        who = "admins and music DJs" if self.settings["music_dj"] else "admins"
        await self.send_to(p.uid, {"t": "denied", "text": f"Only {who} can {DENY.get(perm, 'do that')}"})

    async def on_music(self, p: Participant, cmd: str, msg: dict):
        admin = p.admin
        if cmd in ("add", "addMany"):
            now_, next_ = bool(msg.get("now")), bool(msg.get("next"))
            if not self.can(p, "add"):
                return await self.deny(p, "add")
            if now_ and not self.can(p, "playback"):
                return await self.deny(p, "playback")
            if next_ and not self.can(p, "next"):
                return await self.deny(p, "next")
            tracks = msg.get("tracks") if cmd == "addMany" else [msg.get("track") or {}]
            items, seen = [], set()
            for t in (tracks or [])[:200]:
                it = self._item(t, p)
                if it and it["id"] not in seen:  # the same song twice in one batch
                    seen.add(it["id"])
                    items.append(it)
            if not items:
                return
            if len([q for q in self.queue if self.mine(q, p)]) + len(items) > 200 and not admin:
                return await self.toast(p.uid, "Queue limit reached")
            if len(self.queue) + len(items) > QUEUE_MAX:  # the whole queue goes to every phone on each change
                items = items[:max(0, QUEUE_MAX - len(self.queue))]
                if not items:
                    return await self.toast(p.uid, f"The queue is full ({QUEUE_MAX} songs)")
            # No duplicates in the queue. "Play now" / "Play next" on a song that's already queued moves it instead.
            queued = {q["id"] for q in self.queue}
            if now_ or next_:
                self.queue = [q for q in self.queue if q["id"] not in seen]
            else:
                dupes = [it for it in items if it["id"] in queued or (self.current and self.current["id"] == it["id"])]
                items = [it for it in items if it not in dupes]
                if not items:
                    playing = self.current and self.current["id"] == dupes[0]["id"]
                    return await self.toast(p.uid, "This song is playing now" if playing and len(dupes) == 1
                                            else "Already in the queue")
            if now_ or next_:
                self.queue[0:0] = items
            else:
                # people's songs go before the songs autoplay picked; autoplay carries on after them
                at = next((i for i, q in enumerate(self.queue) if q.get("auto")), len(self.queue))
                self.queue[at:at] = items
            if not self.current:
                await self._start(self.queue.pop(0))
            elif now_:
                await self._next()
            else:
                await self.push_music()
                spawn(music.prefetch(self.queue[0]["id"]))
            skipped = len(seen) - len(items)
            if now_:
                text = f"Playing: {items[0]['title']}"
            elif next_:
                text = f"Plays next: {items[0]['title']}" if len(items) == 1 else f"{len(items)} songs play next"
            else:
                text = f"Added {len(items)} songs" if len(items) > 1 else f"Added: {items[0]['title']}"
            if skipped and not (now_ or next_):
                text += f" ({skipped} already in the queue)"
            if not msg.get("quiet"):
                await self.toast(p.uid, text)
            return
        if cmd in ("spotify", "import"):  # a Spotify or SoundCloud link: find its songs on YouTube and queue them
            if not self.can(p, "add"):
                return await self.deny(p, "add")
            if self._spotify_busy.get(p.uid):
                return await self.toast(p.uid, "Still adding your last link…")
            url = str(msg.get("url", ""))
            if sp := music.parse_spotify(url):
                source, fetch = "Spotify", (lambda: music.spotify(*sp))
            elif sc := music.parse_soundcloud(url):
                source, fetch = "SoundCloud", (lambda: music.soundcloud(sc))
            else:
                return await self.toast(p.uid, "That's not a Spotify or SoundCloud link")
            self._spotify_busy[p.uid] = True
            spawn(self._import_job(p, source, fetch))
            return
        if cmd == "fx":
            return await self.set_fx(p, msg.get("fx") or {})
        if cmd == "voteskip":
            if not self.current:
                return
            if not self.can(p, "vote"):
                return await self.deny(p, "vote")
            self.votes.add(p.uid)
            if len(self.votes) >= max(1, (len(self.parts) + 1) // 2):
                await self._next()
            else:
                await self.push_music()
            return
        if cmd == "remove":
            qid = msg.get("qid")
            for q in self.queue:
                if q["qid"] == qid:
                    if not (self.mine(q, p) or self.can(p, "queue")):
                        return await self.deny(p, "queue")
                    self.queue.remove(q)
                    await self.push_music()
                    return
            return
        if cmd == "skip":
            if not self.current:
                return
            if (self.mine(self.current, p) and self.can(p, "skip_own")) or self.can(p, "playback"):
                return await self._next()
            return await self.deny(p, "playback")
        need = {"play": "playback", "pause": "playback", "seek": "playback", "prev": "playback", "stop": "stop",
                "clear": "queue", "move": "queue", "playnow": "queue", "shuffle": "queue",
                "loop": "modes", "autoplay": "modes", "video": "video"}.get(cmd)
        if need and not self.can(p, need):
            return await self.deny(p, need)
        if cmd == "video":
            # a song switched to video is shown as video to everyone (each person can still switch back to audio)
            if self.current and msg.get("on") and not self.current.get("video"):
                self.current["video"] = True
                await self.push_music()
            return
        if cmd == "play":
            if self.status == "paused":
                self._idle_paused = False
                self.status, self.upd = "playing", now()
                self._arm_timer()
                await self.push_music()
            elif self.status == "stopped" and self.queue:
                await self._start(self.queue.pop(0))
        elif cmd == "pause":
            if self.status in ("playing", "loading"):
                self.pos, self.upd, self.status = self.cur_pos(), now(), "paused"
                self._cancel_timer()
                await self.push_music()
        elif cmd == "seek":
            if self.current:
                dur = self.current.get("duration") or 1e9
                try:
                    to = float(msg.get("pos", 0))
                except (TypeError, ValueError):
                    return
                if not math.isfinite(to):
                    return
                self.pos, self.upd = max(0.0, min(to, dur - 1)), now()
                if self.status == "loading":
                    self._load_seek = self.current["qid"]  # (kept when it starts)
                self._arm_timer()
                await self.push_music()
        elif cmd == "prev":
            start = (self.current or {}).get("start") or 0.0
            if self.current and self.cur_pos() - start > 5:
                self.pos, self.upd = start, now()  # back to where the sound starts, not the silent intro
                self._arm_timer()
                await self.push_music()
            elif self.history:
                # walk back through what was played (the old version re-read the plays table and just
                # bounced between the last two songs)
                last = self.history.pop()
                it = self._item(last, None, by=last.get("by"))
                if it:
                    it["byUid"] = last.get("byUid", 0)
                    if self.current:
                        self._qid += 1
                        self.queue = [q for q in self.queue if q["id"] != self.current["id"]]
                        self.queue.insert(0, {**self.current, "qid": self._qid})
                    self.queue = [q for q in self.queue if q["id"] != it["id"]]
                    await self._start(it, from_prev=True)
        elif cmd == "stop":
            self.queue.clear()
            await self._start(None)
        elif cmd == "clear":
            self.queue.clear()
            await self.push_music()
        elif cmd == "move":
            qid, to = msg.get("qid"), int(msg.get("to", 0))
            for i, q in enumerate(self.queue):
                if q["qid"] == qid:
                    self.queue.insert(max(0, min(to, len(self.queue) - 1)), self.queue.pop(i))
                    break
            await self.push_music()
        elif cmd == "playnow":
            qid = msg.get("qid")
            for i, q in enumerate(self.queue):
                if q["qid"] == qid:
                    await self._start(self.queue.pop(i))
                    break
        elif cmd == "shuffle":
            random.shuffle(self.queue)
            await self.push_music()
        elif cmd == "loop":
            if msg.get("mode") in ("off", "one", "all"):
                self.loop = msg["mode"]
                await self.push_music()
        elif cmd == "autoplay":
            self.autoplay = bool(msg.get("on"))
            await self.push_music()
            if self.autoplay and not self.queue:
                last = self.current["id"] if self.current else (self.recent[-1] if self.recent else None)
                if last:
                    spawn(self._autoplay_fill(last))
        elif cmd == "ended":
            # client hint for tracks with unknown duration; it must have played a while (anyone can send this)
            if (self.current and self.current["qid"] == msg.get("qid") and not self.current.get("duration")
                    and self.status == "playing" and self.cur_pos() > 20):
                await self._next(auto=True)

    async def set_fx(self, p: Participant, new: dict):
        own = self.mine(self.current, p)
        if not (self.can(p, "fx") or (own and self.can(p, "fx_own"))):
            return await self.deny(p, "fx")
        if not isinstance(new, dict):
            return

        def num(k, lo, hi):
            try:
                v = float(new.get(k, self.fx[k]))
            except (TypeError, ValueError):
                return self.fx[k]
            return min(hi, max(lo, v)) if v == v else self.fx[k]
        fx = {"preset": new.get("preset") if new.get("preset") in FX_PRESETS else "custom",
              "bass": num("bass", -12, 12), "mid": num("mid", -12, 12), "treble": num("treble", -12, 12),
              "reverb": num("reverb", 0, 0.8), "rate": round(num("rate", 0.7, 1.4), 2),
              "mode": "8d" if new.get("mode") == "8d" else "none"}
        if fx["rate"] != self.fx["rate"]:
            self.pos, self.upd = self.cur_pos(), now()  # keep the song where it is, then run at the new speed
        self.fx = fx
        self.fx_by_admin = p.admin
        self._arm_timer()
        await self.push_music()

    # ---------------- surviving a restart -----------------
    # The queue and the song playing lived only in memory: every deploy or restart emptied them. They're saved
    # (a moment after each change) and read back when the room is built again.
    async def _save_soon(self):
        self._save_again = True
        if getattr(self, "_saving", False):
            return  # (the running save goes round once more: a change made while it saved isn't lost)
        self._saving = True
        try:
            while self._save_again:
                self._save_again = False
                await asyncio.sleep(2)
                await self.save_now()
        finally:
            self._saving = False

    async def save_now(self):
        data = {"queue": self.queue, "current": self.current, "status": self.status, "pos": self.cur_pos(),
                "at": now(), "loop": self.loop, "autoplay": self.autoplay, "fx": self.fx, "qid": self._qid}
        try:
            await db.x("INSERT OR REPLACE INTO room_state(chat_id,data,ts) VALUES(?,?,?)",
                       (self.chat_id, _dumps(data), now()))
        except Exception as e:
            log.info("saving music state failed: %s", e)

    def restore(self, data: dict):
        if not isinstance(data, dict):
            return
        self._qid = int(data.get("qid") or 0)
        self.queue = [q for q in data.get("queue") or [] if isinstance(q, dict) and q.get("id")][:QUEUE_MAX]
        self.loop = data.get("loop") if data.get("loop") in ("off", "one", "all") else "off"
        self.autoplay = bool(data.get("autoplay"))
        if isinstance(data.get("fx"), dict):
            self.fx = {**FX_DEFAULT, **data["fx"]}
        cur = data.get("current")
        if isinstance(cur, dict) and cur.get("id") and data.get("status") in ("playing", "paused", "loading"):
            self.current = cur
            pos = float(data.get("pos") or 0)
            if data.get("status") == "playing":
                pos += (now() - float(data.get("at") or now())) * self.fx["rate"]
            dur = cur.get("duration") or 0
            if dur and pos >= (cur.get("end") or dur) - 1:
                # it would have ended while the server was down: the next one starts (properly, with _start)
                # when someone comes
                self.current = None
                self.history.append(cur)
                if self.queue:
                    self._idle_paused = self._next_on_resume = True
            if self.current:
                # paused until someone is here; the first person in starts it again (see join)
                self.pos, self.upd, self.status, self._idle_paused = pos, now(), "paused", True
                self._watching.add(self.current["qid"])
                spawn(self._watch_song(self.current["qid"], self.current["id"]))
                spawn(music.prefetch(self.current["id"]))

    def _remember(self, q: Participant):
        self.sticky[q.uid] = {"force_muted": q.force_muted, "role": q.role}

    # ---------------- admin -----------------
    async def on_admin(self, p: Participant, t: str, msg: dict):
        await self._on_admin(p, t, msg)
        for q in list(self.parts.values()):
            if not q.admin and (q.force_muted or q.role == "listener" or q.uid in self.sticky):
                self._remember(q)

    async def _on_admin(self, p: Participant, t: str, msg: dict):
        if not p.admin and "uid" in msg:  # (moderators see stand-in ids)
            msg = {**msg, "uid": self.real_uid(msg.get("uid"))}
        target = self.parts.get(int(msg.get("uid", 0) or 0))
        if t in ("admin.mute", "admin.unmute", "admin.role", "admin.kick", "admin.ban", "admin.clearflags") and not target:
            if t != "admin.ban":
                return
        if target and target.admin and t in ("admin.mute", "admin.kick", "admin.ban", "admin.role") and target.uid != p.uid:
            return await self.toast(p.uid, "Can't do that to another admin")
        if not p.admin and (not target or self.is_mod(target)):  # moderators: only people in the room, not each other
            return await self.toast(p.uid, "Moderators can't do that to another moderator" if target else "They're not here")
        if t == "admin.mute":
            target.force_muted = True
            if target.mic:
                await self.set_mic(target, False)
            await lk.set_publish(self.chat_id, self.lk_id(target.uid), False)
            await self.bcast_part(target)
            await self.toast(target.uid, ("An admin" if p.admin else "A moderator") + " muted you")
        elif t == "admin.unmute":
            target.force_muted = False
            await lk.set_publish(self.chat_id, self.lk_id(target.uid), self.can_speak(target))
            await self.bcast_part(target)
            await self.toast(target.uid, ("An admin" if p.admin else "A moderator") + " allowed you to speak")
        elif t == "admin.role":
            role = msg.get("role")
            if role not in ("speaker", "listener"):
                return
            target.role, target.hand = role, 0
            if role == "speaker":
                target.force_muted = False
            if not self.can_speak(target) and target.mic:
                await self.set_mic(target, False)
            await lk.set_publish(self.chat_id, self.lk_id(target.uid), self.can_speak(target))
            await self.bcast_part(target)
            await self.toast(target.uid, "You can speak now — tap the mic" if role == "speaker" else "You are now a listener")
        elif t in ("admin.kick", "admin.ban"):
            uid = target.uid if target else int(msg.get("uid", 0))
            name = target.name if target else str(msg.get("name", ""))[:64]
            if not uid or uid == p.uid:
                return
            if not target and (await tg.member_status(self.chat_id, uid))["admin"]:
                return await self.toast(p.uid, "Can't do that to another admin")
            if t == "admin.ban":
                await db.x("INSERT OR REPLACE INTO bans(chat_id,uid,name,by_uid,ts) VALUES(?,?,?,?,?)",
                           (self.chat_id, uid, name, p.uid, now()))
            if target:
                await self.send_to(uid, {"t": "kicked", "ban": t == "admin.ban"})
                for c in list(target.conns):
                    await c.close(4003)
                ident = self.lk_id(uid)
                await self._gone(target)
                await lk.remove(self.chat_id, ident)
            await self.toast(p.uid, f"{'Banned' if t == 'admin.ban' else 'Removed'} {name}")
        elif t == "admin.musicperms":
            ev = msg.get("everyone") or {}
            if isinstance(ev, dict):
                for k, v in ev.items():
                    if k in MUSIC_PERMS:
                        self.settings["music_everyone"][k] = bool(v)
                self.settings["music_admin_only"] = not self.settings["music_everyone"]["add"]
                await db.save_group_settings(self.chat_id, self.settings)
                await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "admin.dj":
            uid = int(msg.get("uid", 0) or 0)
            perms = msg.get("perms") or {}
            djs = self.settings["music_dj"]
            if not uid or not isinstance(perms, dict) or (str(uid) not in djs and len(djs) >= 50):
                return
            who = self.parts.get(uid)
            name = (who.name if who else str(msg.get("name") or djs.get(str(uid), {}).get("name") or "User"))[:64]
            djs[str(uid)] = {"name": name, "perms": {k: bool(v) for k, v in perms.items() if k in MUSIC_PERMS}}
            await db.save_group_settings(self.chat_id, self.settings)
            await self.broadcast({"t": "room", "room": self.room_state()})
            if who:
                await self.toast(uid, "🎧 An admin made you a music DJ")
        elif t == "admin.mod":
            uid = int(msg.get("uid", 0) or 0)
            who = self.parts.get(uid)
            mods = self.settings["mods"]
            if not who or who.admin or (str(uid) not in mods and len(mods) >= 50):
                return
            mods[str(uid)] = {"name": who.name[:64]}
            await db.save_group_settings(self.chat_id, self.settings)
            await self.broadcast({"t": "room", "room": self.room_state()})
            await self.bcast_part(who)
            await self.toast(uid, "🛡 An admin made you a moderator: you can mute, move and remove people")
        elif t == "admin.unmod":
            uid = int(msg.get("uid", 0) or 0)
            if self.settings["mods"].pop(str(uid), None) is not None:
                await db.save_group_settings(self.chat_id, self.settings)
                await self.broadcast({"t": "room", "room": self.room_state()})
                if who := self.parts.get(uid):
                    await self.bcast_part(who)
        elif t == "admin.undj":
            if self.settings["music_dj"].pop(str(int(msg.get("uid", 0) or 0)), None) is not None:
                await db.save_group_settings(self.chat_id, self.settings)
                await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "admin.unban":
            await db.x("DELETE FROM bans WHERE chat_id=? AND uid=?", (self.chat_id, int(msg.get("uid", 0))))
            await self.toast(p.uid, "Unbanned")
        elif t == "admin.clearflags":
            target.flags = []
            await self.bcast_part(target)
        elif t == "admin.muteall":
            for q in list(self.parts.values()):
                if not q.admin:
                    q.force_muted = True
                    if q.mic:
                        await self.set_mic(q, False)
                    await lk.set_publish(self.chat_id, self.lk_id(q.uid), False)
                    await self.bcast_part(q)
            await self.broadcast({"t": "toast", "text": "Admin muted everyone"})
        elif t == "admin.unmuteall":
            for q in list(self.parts.values()):
                if q.force_muted:
                    q.force_muted = False
                    await lk.set_publish(self.chat_id, self.lk_id(q.uid), self.can_speak(q))
                    await self.bcast_part(q)
        elif t == "admin.settings":
            s = msg.get("settings") or {}
            for k, v in s.items():
                if k in ("title", "description") and isinstance(v, str):
                    self.settings[k] = v.strip()[:80 if k == "title" else 300]
                elif k in ("locked", "new_can_speak", "music_admin_only", "soundboard", "join_sound"):
                    self.settings[k] = bool(v)
                    if k == "music_admin_only":
                        self.settings["music_everyone"]["add"] = not bool(v)
            await db.save_group_settings(self.chat_id, self.settings)
            await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "admin.resetlink":
            await db.x("UPDATE groups SET link_version=link_version+1 WHERE chat_id=?", (self.chat_id,))
            await p_send(self, p, {"t": "link", "url": await tg.group_link(self.chat_id), "reset": True})
            spawn(pin.refresh(self.chat_id, force=True))  # the pinned JOIN button gets the new link too
        elif t == "admin.link":
            await p_send(self, p, {"t": "link", "url": await tg.group_link(self.chat_id)})
        elif t == "admin.schedule":
            try:
                at = float(msg.get("at", 0))
            except (TypeError, ValueError):
                at = 0.0
            if not math.isfinite(at) or not at:
                return await self.toast(p.uid, "Pick a date and time")
            if at > now() + 60 * 86400:
                return await self.toast(p.uid, "Pick a time within the next 60 days")
            if at < now() + 30:
                return await self.toast(p.uid, "Pick a time in the future")
            await db.x("INSERT INTO schedules(chat_id,title,at,by_uid) VALUES(?,?,?,?)",
                       (self.chat_id, str(msg.get("title", ""))[:100], at, p.uid))
            await self.toast(p.uid, "Scheduled — the bot will post a reminder in the group")
        elif t == "admin.unschedule":
            await db.x("DELETE FROM schedules WHERE id=? AND chat_id=?", (int(msg.get("id", 0)), self.chat_id))
        elif t == "rec.start":
            if self.rec["on"]:
                return await self.toast(p.uid, "Already recording")
            self.rec = {"on": True, "by": p.uid, "since": now(), "name": _clean_name(msg.get("name"))}
            await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "rec.name":
            if self.rec["on"] and self.rec["by"] == p.uid:
                self.rec["name"] = _clean_name(msg.get("name"))
                await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "rec.stop":
            self.rec = {"on": False, "by": None, "since": 0, "name": ""}
            await self.broadcast({"t": "room", "room": self.room_state()})


def _clean_name(v) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(v or "")).strip()[:60]


async def p_send(room: Room, p: Participant, msg: dict):
    await room.send_to(p.uid, msg)


async def get_room(chat_id: int) -> Room:
    r = ROOMS.get(chat_id)
    if r:
        return r
    g = await db.get_group(chat_id)
    rows = await db.q("SELECT * FROM messages WHERE chat_id=? AND ts>? ORDER BY id DESC LIMIT 300", (chat_id, now() - CHAT_KEEP))
    plays = await db.q("SELECT vid, title, artist, duration, by_uid, by_name FROM plays WHERE chat_id=? "
                       "ORDER BY id DESC LIMIT 20", (chat_id,))
    if chat_id in ROOMS:  # another request built it while we were reading the database
        return ROOMS[chat_id]
    saved = await db.q1("SELECT data FROM room_state WHERE chat_id=?", (chat_id,))
    if chat_id in ROOMS:
        return ROOMS[chat_id]
    r = Room(chat_id, g)
    if saved:
        try:
            r.restore(json.loads(saved["data"]))
        except Exception as e:
            log.info("couldn't restore the music of %s: %s", chat_id, e)
    for m in reversed(rows):
        asg = bool(m.get("asg"))
        r.chat.append({"id": m["id"], "uid": m["uid"], "name": m["name"], "text": m["text"],
                       "photo": f"/api/avatar/{chat_id}" if asg else f"/api/avatar/{m['uid']}",
                       "reply": json.loads(m["reply"]) if m["reply"] else None,
                       "reactions": json.loads(m["reactions"] or "{}"), "ts": m["ts"], **({"asg": True} if asg else {})})
    for pl in reversed(plays):
        r.history.append({"id": pl["vid"], "title": pl["title"] or "", "artist": pl["artist"] or "",
                          "duration": pl["duration"] or 0, "by": pl["by_name"] or "", "byUid": pl["by_uid"] or 0})
    ROOMS[chat_id] = r
    return r


async def bg_tick_loop():
    """A minimized Telegram stops the app's timers, but messages from the server still wake it. Without that, a
    song that failed to load in the background (its retry waits on a timer) stayed silent until the app was opened
    again, while everyone else's phone had moved on. Minimized apps get a small tick every 3 seconds, and the
    player checks itself on each one."""
    tick = _dumps({"t": "tick"})
    while True:
        await asyncio.sleep(3)
        for r in list(ROOMS.values()):
            if r.status not in ("playing", "loading"):
                continue
            for p in list(r.parts.values()):
                for c in list(p.conns):
                    if c.bg:
                        await c.send_text(tick)


async def heartbeat_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await db.x("DELETE FROM messages WHERE ts<?", (now() - CHAT_KEEP,))
        except Exception:
            log.exception("chat cleanup")
        for r in list(ROOMS.values()):
            try:
                await r.heartbeat()
            except Exception:
                log.exception("heartbeat")
            # a voice chat nobody has been in for 6 hours, with no music playing, is let go of (it's built
            # again from the database when someone comes back)
            if not r.parts and not r._lingering and r.status != "playing" and now() - r.last_active > 6 * 3600:
                await r.save_now()
                r._cancel_timer()
                ROOMS.pop(r.chat_id, None)
