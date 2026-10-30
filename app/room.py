"""Realtime room engine: participants, roles, chat, music queue sync, admin tools, logs."""
import asyncio
import json
import logging
import random
import re
import time
from collections import deque

from starlette.websockets import WebSocket

from . import badges, db, lk, music, pin, tg

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
        self.alive = True
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self._writer = asyncio.create_task(self._write())

    async def send(self, msg: dict):
        if not self.alive:
            return
        if self.q.qsize() >= self.MAX_QUEUE:
            # This phone can't keep up. Drop the connection; the app reconnects and gets a fresh state.
            self.alive = False
            spawn(self._drop())
            return
        self.q.put_nowait(json.dumps(msg, ensure_ascii=False))

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
        self.recent: deque[str] = deque(maxlen=60)
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

    # ---------------- helpers -----------------
    def is_mod(self, p: Participant | None) -> bool:
        return bool(p) and not p.admin and str(p.uid) in self.settings["mods"]

    def can_speak(self, p: Participant) -> bool:
        if p.force_muted:
            return False
        return p.admin or p.role == "speaker"

    def pub(self, p: Participant, for_admin: bool) -> dict:
        d = {"uid": p.uid, "name": p.name, "username": p.username, "photo": p.photo, "bio": p.bio, "admin": p.admin,
             "role": p.role, "mic": p.mic, "forceMuted": p.force_muted, "canSpeak": self.can_speak(p),
             "hand": p.hand, "video": p.video, "screen": p.screen, "joined": p.joined, "asGroup": p.as_group,
             "mod": self.is_mod(p)}
        if p.as_group:
            d.update(name=self.group_title, username="", photo=f"/api/avatar/{self.chat_id}", bio="")
            if for_admin:
                d["realName"] = p.name
        if for_admin:
            d["flags"] = p.flags
        return d

    async def send_to(self, uid: int, msg: dict):
        p = self.parts.get(uid)
        if p:
            for c in list(p.conns):
                await c.send(msg)

    async def broadcast(self, msg: dict, admins_only: bool = False):
        for p in list(self.parts.values()):
            if admins_only and not (p.admin or self.is_mod(p)):  # moderators get the alerts too (hands, warnings)
                continue
            for c in list(p.conns):
                await c.send(msg)

    async def bcast_part(self, p: Participant):
        a, n = {"t": "p.upd", "p": self.pub(p, True)}, {"t": "p.upd", "p": self.pub(p, False)}
        for q in list(self.parts.values()):
            for c in list(q.conns):
                await c.send(a if q.admin else n)

    def room_state(self) -> dict:
        return {"settings": self.settings, "rec": self.rec, "count": len(self.parts)}

    def music_state(self) -> dict:
        need = max(1, (len(self.parts) + 1) // 2)
        return {"queue": self.queue, "current": self.current, "status": self.status, "pos": self.pos,
                "upd": self.upd, "loop": self.loop, "autoplay": self.autoplay, "votes": len(self.votes),
                "need": need, "fx": self.fx, "rate": self.fx["rate"], "xfade": XFADE}

    async def push_music(self):
        await self.broadcast({"t": "music", "m": self.music_state(), "now": now()})

    async def toast(self, uid: int, text: str):
        await self.send_to(uid, {"t": "toast", "text": text})

    # ---------------- join / leave -----------------
    async def join(self, conn: Conn, user: dict, admin: bool, as_group: bool):
        uid = conn.uid
        p = self.parts.get(uid)
        first = p is None
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
        p.conns.add(conn)
        if self._idle:
            self._idle.cancel()
            self._idle = None
        everyone = [self.pub(q, admin) for q in self.parts.values()]
        # Messages pop up and disappear, like Telegram's voice chat. Everyone can read back what was said since
        # they joined (leaving and coming back starts over); admins can read back the last 48 hours.
        since = now() - CHAT_KEEP if admin else max(p.chat_since, now() - CHAT_KEEP)
        await conn.send({"t": "init", "me": uid, "now": now(), "parts": everyone, "room": self.room_state(),
                         "music": self.music_state(), "chat": [m for m in self.chat if m["ts"] >= since],
                         "admin": admin,
                         "group": self.group_title})
        if first:
            a, n = {"t": "p.join", "p": self.pub(p, True)}, {"t": "p.join", "p": self.pub(p, False)}
            for q in list(self.parts.values()):
                if q.uid == uid:
                    continue
                for c in list(q.conns):
                    await c.send(a if q.admin else n)
            await self.broadcast({"t": "room", "room": self.room_state()})
            pin.schedule(self.chat_id)
            if p.flags:
                await self.broadcast({"t": "alert", "uid": uid, "name": p.name, "flags": p.flags}, admins_only=True)
                if "Joined during a join flood" in p.flags:
                    spawn(tg.alert_admins(self.chat_id, "flood",
                        f"⚠️ Many people are joining the voice chat at once ({len(self.parts)} now). Check the Safety tab."))

    async def leave(self, conn: Conn):
        p = self.parts.get(conn.uid)
        if not p:
            return
        p.conns.discard(conn)
        if p.conns or self.parts.get(conn.uid) is not p:
            return
        # Remove from the room before any await, so a quick rejoin creates a fresh participant
        # instead of being dropped by this (late) leave.
        self.parts.pop(p.uid, None)
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
        await self.broadcast({"t": "p.leave", "uid": p.uid})
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
        await lk.remove(self.chat_id, uid)

    # ---------------- message router -----------------
    async def handle(self, conn: Conn, msg: dict):
        p = self.parts.get(conn.uid)
        if not p:
            return
        t = msg.get("t", "")
        try:
            if t == "ping":
                await conn.send({"t": "pong", "id": msg.get("id"), "now": now()})
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
        reply = None
        rid = msg.get("reply")
        if rid:
            for m in self.chat:
                if m["id"] == rid:
                    reply = {"id": m["id"], "name": m["name"], "text": m["text"][:120]}
        name = self.group_title if p.as_group else p.name
        ts = now()
        mid = await db.x("INSERT INTO messages(chat_id,uid,name,text,reply,ts) VALUES(?,?,?,?,?,?)",
                         (self.chat_id, p.uid, name, text, json.dumps(reply) if reply else None, ts))
        m = {"id": mid, "uid": p.uid, "name": name, "photo": f"/api/avatar/{self.chat_id}" if p.as_group else p.photo, "text": text,
             "reply": reply, "reactions": {}, "ts": ts}
        self.chat.append(m)
        await self.broadcast({"t": "chat", "m": m})

    async def on_react(self, p: Participant, msg: dict):
        emoji = msg.get("emoji")
        if emoji not in REACTIONS:
            return
        for m in self.chat:
            if m["id"] == msg.get("id"):
                users = m["reactions"].setdefault(emoji, [])
                if p.uid in users:
                    users.remove(p.uid)
                    if not users:
                        m["reactions"].pop(emoji)
                else:
                    users.append(p.uid)
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
        await self.broadcast({"t": "sound", "id": sid, "uid": p.uid,
                              "name": self.group_title if p.as_group else p.name})

    async def on_fly(self, p: Participant, msg: dict):
        emoji = msg.get("emoji")
        if emoji not in FLY:
            return
        t = now()
        if len(p.fly_times) == p.fly_times.maxlen and t - p.fly_times[0] < 3:
            return  # max 6 every 3 seconds
        p.fly_times.append(t)
        await self.broadcast({"t": "fly", "emoji": emoji, "uid": p.uid,
                              "name": self.group_title if p.as_group else p.name})

    async def on_report(self, p: Participant, msg: dict):
        target = self.parts.get(int(msg.get("uid", 0)))
        if not target or target.uid == p.uid:
            return
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
        xf = min(XFADE, max(0.0, left - 0.5), length * 0.15) if (self.loop != "one" and nxt and music.is_ready(nxt["id"])) else 0.0
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

    def _item(self, t: dict, p: Participant | None, by: str | None = None) -> dict | None:
        vid = str(t.get("id", ""))
        if not re.fullmatch(r"[\w-]{11}", vid):
            return None
        self._qid += 1
        return {"qid": self._qid, "id": vid, "title": str(t.get("title", ""))[:200],
                "artist": str(t.get("artist", ""))[:120], "duration": float(t.get("duration") or 0),
                "thumb": f"/api/thumb/{vid}", "by": by or (p.name if p else ""), "byUid": p.uid if p else 0}

    async def _start(self, item: dict | None, from_prev: bool = False):
        cur = self.current
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
            spawn(self._watch_song(item["qid"], item["id"]))
            if self.queue:
                spawn(music.prefetch(self.queue[0]["id"]))
            t = now()
            await db.x("INSERT INTO plays(chat_id,vid,title,artist,duration,by_uid,by_name,ts) VALUES(?,?,?,?,?,?,?,?)",
                       (self.chat_id, item["id"], item["title"], item["artist"], item["duration"], item["byUid"],
                        item["by"], t))
            # what each person listens to, for their own "Made for you" recommendations
            await db.xmany("INSERT INTO listens(uid,vid,title,artist,ts) VALUES(?,?,?,?,?)",
                           [(uid, item["id"], item["title"], item["artist"], t) for uid in self.parts])
            if not item["duration"]:
                spawn(self._fill_duration(item))
        else:
            self.status = "stopped"
        self._arm_timer()
        await self.push_music()

    async def _when_ready(self, qid: int, vid: str):
        try:
            await asyncio.wait_for(music.resolve(vid), 15)
        except Exception as e:  # can't be found / too slow: start anyway, the phones retry on their own
            log.info("resolve before start failed %s: %s", vid, e)
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
                self.status, self.pos, self.upd = "playing", self.current.get("start") or 0.0, now()
                self._arm_timer()
                await self.push_music()

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
        trimmed = False
        for _ in range(400):
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
            d = (info.get("track") or {}).get("duration") or 0
            if d and self.current and self.current["qid"] == item["qid"]:
                self.current["duration"] = d
                self._arm_timer()
                await self.push_music()
        except Exception:
            pass

    async def _next(self, auto: bool = False):
        cur = self.current
        if cur and auto and self.loop == "one":
            return await self._start({**cur})
        if cur and self.loop == "all" and all(q["id"] != cur["id"] for q in self.queue):
            self._qid += 1
            self.queue.append({**cur, "qid": self._qid})
        if self.queue:
            return await self._start(self.queue.pop(0))
        if self.autoplay and cur:
            await self._start(None)
            spawn(self._autoplay_fill(cur["id"]))
            return
        await self._start(None)

    async def _autoplay_fill(self, seed: str):
        try:
            rel = await music.related(seed)
        except Exception as e:
            log.info("autoplay failed: %s", e)
            return
        rel = [r for r in rel if r["id"] not in self.recent and (not r["duration"] or r["duration"] < 900)]
        top = rel[:8]
        random.shuffle(top)  # (shuffling a slice in place would shuffle a throwaway copy)
        rel = top + rel[8:]
        async with self._lock:
            if not self.autoplay or self.current:
                return
            for r in rel[:3]:
                it = self._item(r, None, by="Autoplay")
                if it:
                    it["auto"] = True
                    self.queue.append(it)
            if self.queue:
                await self._start(self.queue.pop(0))

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
            if len([q for q in self.queue if q["byUid"] == p.uid]) + len(items) > 200 and not admin:
                return await self.toast(p.uid, "Queue limit reached")
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
                    if not (q["byUid"] == p.uid or self.can(p, "queue")):
                        return await self.deny(p, "queue")
                    self.queue.remove(q)
                    await self.push_music()
                    return
            return
        if cmd == "skip":
            if not self.current:
                return
            if (self.current["byUid"] == p.uid and self.can(p, "skip_own")) or self.can(p, "playback"):
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
                self.pos, self.upd = max(0.0, min(float(msg.get("pos", 0)), dur - 1)), now()
                self._arm_timer()
                await self.push_music()
        elif cmd == "prev":
            if self.current and self.cur_pos() > 5:
                self.pos, self.upd = 0.0, now()
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
            if self.autoplay and not self.current and not self.queue:
                last = self.recent[-1] if self.recent else None
                if last:
                    spawn(self._autoplay_fill(last))
        elif cmd == "ended":
            # client hint for tracks with unknown duration
            if self.current and self.current["qid"] == msg.get("qid") and not self.current.get("duration"):
                await self._next(auto=True)

    async def set_fx(self, p: Participant, new: dict):
        own = bool(self.current and self.current["byUid"] == p.uid)
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

    def _remember(self, q: Participant):
        self.sticky[q.uid] = {"force_muted": q.force_muted, "role": q.role}

    # ---------------- admin -----------------
    async def on_admin(self, p: Participant, t: str, msg: dict):
        await self._on_admin(p, t, msg)
        for q in self.parts.values():
            if not q.admin and (q.force_muted or q.role == "listener" or q.uid in self.sticky):
                self._remember(q)

    async def _on_admin(self, p: Participant, t: str, msg: dict):
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
            await lk.set_publish(self.chat_id, target.uid, False)
            await self.bcast_part(target)
            await self.toast(target.uid, ("An admin" if p.admin else "A moderator") + " muted you")
        elif t == "admin.unmute":
            target.force_muted = False
            await lk.set_publish(self.chat_id, target.uid, self.can_speak(target))
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
            await lk.set_publish(self.chat_id, target.uid, self.can_speak(target))
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
                await lk.remove(self.chat_id, uid)
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
            for q in self.parts.values():
                if not q.admin:
                    q.force_muted = True
                    if q.mic:
                        await self.set_mic(q, False)
                    await lk.set_publish(self.chat_id, q.uid, False)
                    await self.bcast_part(q)
            await self.broadcast({"t": "toast", "text": "Admin muted everyone"})
        elif t == "admin.unmuteall":
            for q in self.parts.values():
                if q.force_muted:
                    q.force_muted = False
                    await lk.set_publish(self.chat_id, q.uid, self.can_speak(q))
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
        elif t == "admin.link":
            await p_send(self, p, {"t": "link", "url": await tg.group_link(self.chat_id)})
        elif t == "admin.schedule":
            at = float(msg.get("at", 0))
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
    r = Room(chat_id, g)
    for m in reversed(rows):
        r.chat.append({"id": m["id"], "uid": m["uid"], "name": m["name"], "photo": f"/api/avatar/{m['uid']}", "text": m["text"],
                       "reply": json.loads(m["reply"]) if m["reply"] else None,
                       "reactions": json.loads(m["reactions"] or "{}"), "ts": m["ts"]})
    for pl in reversed(plays):
        r.history.append({"id": pl["vid"], "title": pl["title"] or "", "artist": pl["artist"] or "",
                          "duration": pl["duration"] or 0, "by": pl["by_name"] or "", "byUid": pl["by_uid"] or 0})
    ROOMS[chat_id] = r
    return r


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
