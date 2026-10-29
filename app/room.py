"""Realtime room engine: participants, roles, chat, music queue sync, admin tools, logs."""
import asyncio
import json
import logging
import random
import re
import time
from collections import deque

from starlette.websockets import WebSocket

from . import db, lk, music, pin, tg

log = logging.getLogger("room")
ROOMS: dict[int, "Room"] = {}
SOUNDS = {"airhorn", "applause", "drumroll", "rimshot", "bell", "whoosh", "laugh", "wow"}
REACTIONS = {"👍", "❤️", "😂", "🔥", "😮", "😢", "👏", "🎉"}
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
        self.conns: set["Conn"] = set()
        self.session_id = 0
        self.mic_since = 0.0
        self.mic_secs = 0.0
        self.flags: list[str] = []
        self.last_sound = 0.0


class Conn:
    def __init__(self, ws: WebSocket, uid: int):
        self.ws = ws
        self.uid = uid
        self.hits: deque[float] = deque(maxlen=40)
        self.alive = True

    async def send(self, msg: dict):
        if not self.alive:
            return
        try:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            self.alive = False


class Room:
    def __init__(self, chat_id: int, group: dict):
        self.chat_id = chat_id
        self.settings = {**DEFAULT_SETTINGS, **group["settings"]}
        if not self.settings["title"]:
            self.settings["title"] = group.get("title") or "Voice Chat"
        self.group_title = group.get("title") or "Group"
        self.parts: dict[int, Participant] = {}
        self.chat: deque[dict] = deque(maxlen=80)
        self.join_times: deque[float] = deque(maxlen=50)
        self.user_joins: dict[int, deque] = {}
        self.rec = {"on": False, "by": None, "since": 0}
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
        self._timer: asyncio.Task | None = None
        self._qid = 0
        self._lock = asyncio.Lock()
        # admin decisions survive leaving and rejoining: {uid: {"force_muted": bool, "role": str}}
        self.sticky: dict[int, dict] = {}

    # ---------------- helpers -----------------
    def can_speak(self, p: Participant) -> bool:
        if p.force_muted:
            return False
        return p.admin or p.role == "speaker"

    def pub(self, p: Participant, for_admin: bool) -> dict:
        d = {"uid": p.uid, "name": p.name, "username": p.username, "photo": p.photo, "bio": p.bio, "admin": p.admin,
             "role": p.role, "mic": p.mic, "forceMuted": p.force_muted, "canSpeak": self.can_speak(p),
             "hand": p.hand, "video": p.video, "screen": p.screen, "joined": p.joined, "asGroup": p.as_group}
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
            if admins_only and not p.admin:
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
                "need": need}

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
            if not admin:
                self._detect(p)
            p.session_id = await db.x(
                "INSERT INTO sessions(chat_id,uid,name,username,joined,last_seen) VALUES(?,?,?,?,?,?)",
                (self.chat_id, uid, p.name, p.username, p.joined, p.joined))
        p.admin = admin
        p.conns.add(conn)
        everyone = [self.pub(q, admin) for q in self.parts.values()]
        await conn.send({"t": "init", "me": uid, "now": now(), "parts": everyone, "room": self.room_state(),
                         "music": self.music_state(), "chat": list(self.chat), "admin": admin,
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
        self.votes.discard(p.uid)
        self._mic_flush(p)
        t = now()
        await db.x("UPDATE sessions SET left=?, last_seen=?, mic_secs=?, open=0 WHERE id=?",
                   (t, t, p.mic_secs, p.session_id))
        if self.rec["on"] and self.rec["by"] == p.uid:
            self.rec = {"on": False, "by": None, "since": 0}
        await self.broadcast({"t": "p.leave", "uid": p.uid})
        await self.broadcast({"t": "room", "room": self.room_state()})
        pin.schedule(self.chat_id)
        if not self.parts:
            # nobody left: pause music so it does not run with an empty room
            if self.status == "playing":
                self.pos = self.cur_pos()
                self.upd = now()
                self.status = "paused"
                self._cancel_timer()

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
            try:
                await c.ws.close(4003)
            except Exception:
                pass
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
            elif t == "report":
                await self.on_report(p, msg)
            elif t.startswith("music."):
                async with self._lock:
                    await self.on_music(p, t[6:], msg)
            elif t.startswith("admin.") or t.startswith("rec."):
                if not p.admin:
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
            if m["id"] == msg.get("id") and (p.admin or m["uid"] == p.uid):
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
            return self.pos + (now() - self.upd)
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
        remaining = max(0.5, dur - self.cur_pos()) + 0.8
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
        self.current = item
        self.votes.clear()
        self.pos = 0.0
        self.upd = now()
        if item:
            self.status = "playing"
            self.recent.append(item["id"])
            spawn(music.prefetch(item["id"]))
            if self.queue:
                spawn(music.prefetch(self.queue[0]["id"]))
            await db.x("INSERT INTO plays(chat_id,vid,title,artist,duration,by_uid,by_name,ts) VALUES(?,?,?,?,?,?,?,?)",
                       (self.chat_id, item["id"], item["title"], item["artist"], item["duration"], item["byUid"],
                        item["by"], now()))
            if not item["duration"]:
                spawn(self._fill_duration(item))
        else:
            self.status = "stopped"
        self._arm_timer()
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
        if cur and self.loop == "all":
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
                    self.queue.append(it)
            if self.queue:
                await self._start(self.queue.pop(0))

    async def on_music(self, p: Participant, cmd: str, msg: dict):
        admin = p.admin
        can_add = admin or not self.settings["music_admin_only"]
        if cmd in ("add", "addMany"):
            if not can_add:
                return await self.toast(p.uid, "Only admins can add songs right now")
            tracks = msg.get("tracks") if cmd == "addMany" else [msg.get("track") or {}]
            items = [it for t in (tracks or [])[:200] if (it := self._item(t, p))]
            if not items:
                return
            if len([q for q in self.queue if q["byUid"] == p.uid]) + len(items) > 200 and not admin:
                return await self.toast(p.uid, "Queue limit reached")
            if msg.get("next") and admin:
                self.queue[0:0] = items
            else:
                self.queue.extend(items)
            if not self.current:
                await self._start(self.queue.pop(0))
            else:
                await self.push_music()
                spawn(music.prefetch(self.queue[0]["id"]))
            await self.toast(p.uid, f"Added {len(items)} songs" if len(items) > 1 else f"Added: {items[0]['title']}")
            return
        if cmd == "voteskip":
            if not self.current:
                return
            self.votes.add(p.uid)
            if len(self.votes) >= max(1, (len(self.parts) + 1) // 2):
                await self._next()
            else:
                await self.push_music()
            return
        if cmd == "remove":
            qid = msg.get("qid")
            for q in self.queue:
                if q["qid"] == qid and (admin or q["byUid"] == p.uid):
                    self.queue.remove(q)
                    await self.push_music()
                    return
            return
        if cmd == "skip" and self.current and self.current["byUid"] == p.uid and not admin:
            return await self._next()
        if not admin:
            return await self.toast(p.uid, "Only admins can control playback — use vote skip")
        if cmd == "play":
            if self.status == "paused":
                self.status, self.upd = "playing", now()
                self._arm_timer()
                await self.push_music()
            elif self.status == "stopped" and self.queue:
                await self._start(self.queue.pop(0))
        elif cmd == "pause":
            if self.status == "playing":
                self.pos, self.upd, self.status = self.cur_pos(), now(), "paused"
                self._cancel_timer()
                await self.push_music()
        elif cmd == "seek":
            if self.current:
                dur = self.current.get("duration") or 1e9
                self.pos, self.upd = max(0.0, min(float(msg.get("pos", 0)), dur - 1)), now()
                self._arm_timer()
                await self.push_music()
        elif cmd == "skip":
            await self._next()
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
                        self.queue.insert(0, {**self.current, "qid": self._qid})
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
        if t == "admin.mute":
            target.force_muted = True
            if target.mic:
                await self.set_mic(target, False)
            await lk.set_publish(self.chat_id, target.uid, False)
            await self.bcast_part(target)
            await self.toast(target.uid, "An admin muted you")
        elif t == "admin.unmute":
            target.force_muted = False
            await lk.set_publish(self.chat_id, target.uid, self.can_speak(target))
            await self.bcast_part(target)
            await self.toast(target.uid, "An admin allowed you to speak")
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
                    try:
                        await c.ws.close(4003)
                    except Exception:
                        pass
                await lk.remove(self.chat_id, uid)
            await self.toast(p.uid, f"{'Banned' if t == 'admin.ban' else 'Removed'} {name}")
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
            self.rec = {"on": True, "by": p.uid, "since": now()}
            await self.broadcast({"t": "room", "room": self.room_state()})
        elif t == "rec.stop":
            self.rec = {"on": False, "by": None, "since": 0}
            await self.broadcast({"t": "room", "room": self.room_state()})


async def p_send(room: Room, p: Participant, msg: dict):
    await room.send_to(p.uid, msg)


async def get_room(chat_id: int) -> Room:
    r = ROOMS.get(chat_id)
    if r:
        return r
    g = await db.get_group(chat_id)
    rows = await db.q("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 60", (chat_id,))
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
        for r in list(ROOMS.values()):
            try:
                await r.heartbeat()
            except Exception:
                log.exception("heartbeat")
