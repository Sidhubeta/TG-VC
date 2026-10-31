"""Minimal Telegram Bot API client: group links, membership checks, notifications."""
import asyncio
import logging
import os
import time

from . import auth, config, db
from .http import httpx

log = logging.getLogger("tg")
API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
_client: httpx.AsyncClient | None = None
BOT_USERNAME = ""
_member_cache: dict[tuple[int, int], tuple[float, dict]] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10))
    return _client


class ApiError(RuntimeError):
    """Telegram answered and said no (as opposed to a network problem)."""
    def __init__(self, text: str, code: int = 0):
        super().__init__(text)
        self.code = code


async def _post(method: str, **kw) -> dict:
    for attempt in range(2):
        r = await client().post(f"{API}/{method}", **kw)
        data = r.json()
        retry = (data.get("parameters") or {}).get("retry_after")
        if not data.get("ok") and r.status_code == 429 and retry and retry <= 10 and attempt == 0:
            await asyncio.sleep(retry)  # flood control: wait once, then retry
            continue
        if not data.get("ok"):
            raise ApiError(f"{method}: {data.get('description')}", int(data.get("error_code") or r.status_code or 0))
        return data["result"]


async def call(method: str, **params):
    return await _post(method, json=params)


async def call_multipart(method: str, data: dict, files: dict):
    return await _post(method, data=data, files=files, timeout=60)


async def group_link(chat_id: int) -> str:
    g = await db.get_group(chat_id)
    token = auth.make_group_token(chat_id, g["link_version"])
    mode = "&mode=fullscreen" if config.FULLSCREEN else ""
    return f"https://t.me/{BOT_USERNAME}/{config.APP_SHORT_NAME}?startapp={token}{mode}"


# ---- admin DM alerts (reports, join floods) ----
_admins_cache: dict[int, tuple[float, list[int]]] = {}
_alert_last: dict[tuple[int, str], float] = {}


def _full_name(u: dict) -> str:
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip() or "User"


async def admin_list(chat_id: int) -> list[dict]:
    """Group admins (no bots): [{uid, name, username, anonymous}]. Cached for 10 minutes."""
    hit = _admins_cache.get(chat_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        res = await call("getChatAdministrators", chat_id=chat_id)
        out = [{"uid": a["user"]["id"], "name": _full_name(a["user"]), "username": a["user"].get("username") or "",
                "anonymous": bool(a.get("is_anonymous"))} for a in res if not a["user"].get("is_bot")]
    except Exception as e:
        # Not remembered for 10 minutes: one failed lookup used to leave the group "without admins" that long.
        log.info("getChatAdministrators %s failed: %s", chat_id, e)
        return hit[1] if hit else []
    _admins_cache[chat_id] = (time.time(), out)
    return out


async def group_admins(chat_id: int) -> list[int]:
    return [a["uid"] for a in await admin_list(chat_id)]


_info_cache: dict[int, tuple[float, dict]] = {}


async def hidden_members(chat_id: int) -> bool:
    """True when the group hides its member list (then invites only show admins)."""
    hit = _info_cache.get(chat_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1].get("has_hidden_members", False)
    try:
        info = await call("getChat", chat_id=chat_id)
    except Exception:
        info = hit[1] if hit else {}
    _info_cache[chat_id] = (time.time(), info)
    return bool(info.get("has_hidden_members"))


# ---- people seen in the group (the Bot API can't list members, so we remember who we've seen) ----
_SERVICE_IDS = {777000, 1087968824, 136817688}  # Telegram service account, anonymous admin bot, channel bot
_seen: dict[tuple[int, int], float] = {}


async def remember_member(chat_id: int, user: dict | None):
    if not user or user.get("is_bot") or user.get("id") in _SERVICE_IDS:
        return
    key = (chat_id, user["id"])
    if time.time() - _seen.get(key, 0) < 300:
        return
    _seen[key] = time.time()
    if len(_seen) > 20000:  # (only a "recently written" note: the old ones can go)
        cut = time.time() - 300
        for k in [k for k, t in _seen.items() if t < cut]:
            _seen.pop(k, None)
    await db.x("INSERT OR REPLACE INTO members(chat_id,uid,name,username,seen) VALUES(?,?,?,?,?)",
               (chat_id, user["id"], _full_name(user)[:64], user.get("username") or "", time.time()))


async def forget_member(chat_id: int, uid: int):
    _seen.pop((chat_id, uid), None)
    await db.x("DELETE FROM members WHERE chat_id=? AND uid=?", (chat_id, uid))


async def invite(chat_id: int, uid: int, name: str, by_name: str, live: int = 0) -> str | None:
    """DM the person a Join button; if they never started the bot, mention them in the group instead."""
    url = await group_link(chat_id)
    title = await chat_title(chat_id)
    try:
        await call("sendMessage", chat_id=uid, parse_mode="HTML",
                   text=f"🎧 <b>{_esc(by_name)}</b> invited you to the voice chat in <b>{_esc(title)}</b>",
                   reply_markup=join_button(url))
        return "dm"
    except Exception:
        pass
    g = await db.get_group(chat_id)
    params = {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": join_button(url),
              "text": f'🎧 <a href="tg://user?id={int(uid)}">{_esc(name)}</a>, <b>{_esc(by_name)}</b> invited you to the voice chat'}
    if g.get("pin_thread"):
        params["message_thread_id"] = g["pin_thread"]
    try:
        await call("sendMessage", **params)
    except Exception as e:
        if "message_thread_id" not in params:
            log.info("invite failed: %s", e)
            return None
        # the topic may be closed or gone: mention them in the main chat instead
        params.pop("message_thread_id")
        try:
            await call("sendMessage", **params)
        except Exception as e2:
            log.info("invite failed: %s / %s", e, e2)
            return None
    return "group"




async def alert_admins(chat_id: int, kind: str, text: str, every: int = 120):
    """Private message to every group admin who has started the bot. Rate-limited per kind."""
    if not config.BOT_TOKEN or (config.DEV_MODE and chat_id == -100123):
        return
    key = (chat_id, kind)
    if time.time() - _alert_last.get(key, 0) < every:
        return
    _alert_last[key] = time.time()
    title = await chat_title(chat_id)
    url = await group_link(chat_id)
    for uid in await group_admins(chat_id):
        try:
            await call("sendMessage", chat_id=uid, parse_mode="HTML", text=f"🛡 <b>{_esc(title)}</b>\n{text}",
                       reply_markup=join_button(url, "Open voice chat"))
        except Exception:
            pass  # admin hasn't started the bot in private


_member_calls: dict[tuple[int, int], asyncio.Task] = {}


async def member_status(chat_id: int, uid: int, fresh: bool = False) -> dict:
    """Returns {'member': bool, 'admin': bool}. Cached for 60s."""
    if config.DEV_MODE and uid < 0:  # fake dev users
        return {"member": True, "admin": uid == -1, "name": f"Dev {abs(uid)}", "username": f"dev{abs(uid)}", "bot": False}
    key = (chat_id, uid)
    hit = _member_cache.get(key)
    # "fresh" still accepts an answer from the last few seconds (opening the app asks several times at once)
    if hit and time.time() - hit[0] < (3 if fresh else 60):
        return hit[1]
    # Questions about the same person at the same moment share one Telegram call. When the cache ran out, a phone
    # loading thumbnails, avatars and songs used to send dozens at once — and Telegram answers that with 429.
    task = _member_calls.get(key)
    if task is None:
        task = _member_calls[key] = asyncio.get_running_loop().create_task(_member_lookup(chat_id, uid))
        task.add_done_callback(lambda _t: _member_calls.pop(key, None))
    return await asyncio.shield(task)


async def _member_lookup(chat_id: int, uid: int) -> dict:
    key = (chat_id, uid)
    hit = _member_cache.get(key)
    try:
        m = await call("getChatMember", chat_id=chat_id, user_id=uid)
        st = m.get("status")
        u = m.get("user") or {}
        res = {
            "member": st in ("creator", "administrator", "member") or (st == "restricted" and bool(m.get("is_member"))),
            "admin": st in ("creator", "administrator"),
            "owner": st == "creator",
            "name": _full_name(u)[:64], "username": u.get("username") or "", "bot": bool(u.get("is_bot")),
        }
    except ApiError as e:
        if e.code not in (400, 403):
            # Too many requests (429) or Telegram's own trouble (5xx) is not an answer: keep what we knew. It used
            # to count as "not a member", and the room's check removed everyone at once.
            log.warning("getChatMember %s %s: %s", chat_id, uid, e)
            return hit[1] if hit else {"member": False, "admin": False, "name": "", "username": "", "bot": False, "unsure": True}
        log.info("getChatMember %s %s: %s", chat_id, uid, e)  # Telegram's answer: not a member / chat gone
        res = {"member": False, "admin": False, "name": "", "username": "", "bot": False}
    except Exception as e:
        # Network trouble is not an answer: keep the last known status instead of kicking people out.
        log.warning("getChatMember failed %s %s: %s", chat_id, uid, e)
        return hit[1] if hit else {"member": False, "admin": False, "name": "", "username": "", "bot": False, "unsure": True}
    if len(_member_cache) > 5000:
        cutoff = time.time() - 120
        for k in [k for k, v in _member_cache.items() if v[0] < cutoff]:
            _member_cache.pop(k, None)
    _member_cache[key] = (time.time(), res)
    return res


async def chat_title(chat_id: int) -> str:
    g = await db.get_group(chat_id)
    if g.get("title"):
        return g["title"]
    try:
        c = await call("getChat", chat_id=chat_id)
        await db.x("UPDATE groups SET title=? WHERE chat_id=?", (c.get("title", ""), chat_id))
        return c.get("title", "")
    except Exception:
        return "Group"


_play_help_at: dict[int, float] = {}


async def play_help(chat_id: int, msg: dict, song: str = ""):
    """Someone typed /play (like with other music bots): music happens inside the voice chat, so show how to get there."""
    if time.time() - _play_help_at.get(chat_id, 0) < 20:  # don't flood the group if people spam /play
        return
    _play_help_at[chat_id] = time.time()
    url = await group_link(chat_id)
    asked = f"To play <b>{_esc(song)}</b>, join the voice chat and search for it there.\n\n" if song else ""
    text = (f"🎧 <b>Music plays in the voice chat!</b>\n{asked}"
            "Tap the pinned <b>Voice Chat</b> message at the top of this group, or the button below, to join.\n\n"
            "<b>Inside the voice chat you can:</b>\n"
            "🎵 Play any song — search, or paste a YouTube or Spotify link\n"
            "⏭ Queue songs, play next, skip, repeat, autoplay\n"
            "🎤 Read synced lyrics, watch the music video\n"
            "🎚 Add sound effects for everyone: Bass Boost, 8D, Slowed + Reverb…\n"
            "🎙 Talk with your mic or camera, chat and send reactions\n"
            "❤️ Like songs and save playlists\n\n"
            "Everyone hears the same song at the same moment.")
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": join_button(url),
              "reply_parameters": {"message_id": msg["message_id"], "allow_sending_without_reply": True}}
    if msg.get("is_topic_message") and msg.get("message_thread_id"):
        params["message_thread_id"] = msg["message_thread_id"]
    try:
        await call("sendMessage", **params)
    except Exception as e:
        log.info("play help failed: %s", e)


def join_button(url: str, text: str = "🎧 Join Voice Chat"):
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


async def send_join_message(chat_id: int, text: str):
    url = await group_link(chat_id)
    await call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=join_button(url))


async def send_recording(uid: int, data: bytes, filename: str, caption: str) -> bool:
    """Recordings only ever go to the private chat of the person who recorded, never to the group."""
    try:
        r = await client().post(f"{API}/sendDocument", data={"chat_id": str(uid), "caption": caption},
                                files={"document": (filename, data)}, timeout=300)
        return bool(r.json().get("ok"))
    except Exception as e:
        log.warning("recording send failed to %s: %s", uid, e)
        return False


# ---- update handling ----------------------------------------------------------------------
async def _handle(upd: dict):
    # every way the bot sees someone in the group adds them to the invite list
    if mr := upd.get("message_reaction"):
        if mr.get("chat", {}).get("type") in ("group", "supergroup"):
            await remember_member(mr["chat"]["id"], mr.get("user"))
        return
    if em := upd.get("edited_message"):
        if em.get("chat", {}).get("type") in ("group", "supergroup") and not em.get("sender_chat"):
            await remember_member(em["chat"]["id"], em.get("from"))
        return
    msg = upd.get("message")
    if cm := upd.get("chat_member"):  # someone joined / left the group (needs the bot to be admin)
        if cm["chat"]["type"] in ("group", "supergroup"):
            new = cm.get("new_chat_member") or {}
            if new.get("status") in ("left", "kicked"):
                await forget_member(cm["chat"]["id"], new["user"]["id"])
            else:
                await remember_member(cm["chat"]["id"], new.get("user"))
        return
    if msg and msg.get("chat", {}).get("type") in ("group", "supergroup"):
        gid = msg["chat"]["id"]
        if not msg.get("sender_chat"):
            await remember_member(gid, msg.get("from"))
        for u in msg.get("new_chat_members") or []:
            await remember_member(gid, u)
        if left := msg.get("left_chat_member"):
            await forget_member(gid, left["id"])
    if mcm := upd.get("my_chat_member"):
        chat = mcm["chat"]
        if chat["type"] in ("group", "supergroup"):
            await db.get_group(chat["id"])
            await db.x("UPDATE groups SET title=? WHERE chat_id=?", (chat.get("title", ""), chat["id"]))
            st = mcm["new_chat_member"]["status"]
            if st == "member":
                await call("sendMessage", chat_id=chat["id"],
                           text="👋 Thanks! Please make me an <b>admin</b> so I can verify members, then send /vc.",
                           parse_mode="HTML")
        return
    if not msg or "text" not in msg:
        return
    chat = msg["chat"]
    words = msg["text"].split()
    if not words or not words[0].startswith("/"):
        return
    text = words[0].lower()
    cmd = text.split("@")[0]
    if "@" in text and text.split("@")[1] != BOT_USERNAME.lower():
        return
    if cmd in ("/addsudo", "/delsudo", "/rmsudo", "/sudolist", "/sudo", "/id"):
        return await _sudo_cmd(msg, cmd, " ".join(words[1:]))
    if chat["type"] == "private":
        if cmd == "/start":
            await call("sendMessage", chat_id=chat["id"], parse_mode="HTML", text=(
                "🎧 <b>Voice Chat + Music</b>\n\n"
                "1. Add me to your group\n2. Make me <b>admin</b>\n3. Send /vc in the group\n\n"
                "Only members of that group can join its voice chat."))
        return
    if chat["type"] not in ("group", "supergroup"):
        return
    cid = chat["id"]
    await db.get_group(cid)
    await db.x("UPDATE groups SET title=? WHERE chat_id=?", (chat.get("title", ""), cid))
    if cmd in ("/vc", "/start", "/voice", "/join"):
        from . import pin
        if time.time() - _vc_last.get(cid, 0) < 20:  # spam: the pinned bar is already fresh
            return
        _vc_last[cid] = time.time()
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        try:
            await pin.post(cid, thread)
        except Exception as e:
            log.warning("pin post failed: %s", e)
            await send_join_message(cid, f"🎙 <b>{_esc(chat.get('title', 'Voice Chat'))}</b>\nTap below to join the voice chat.")
    elif cmd in ("/play", "/song", "/music", "/p", "/help", "/queue"):
        await play_help(cid, msg, " ".join(words[1:])[:80])
    elif cmd == "/resetlink":
        if not msg.get("from"):
            return
        st = await member_status(cid, msg["from"]["id"], fresh=True)
        if not st["admin"]:
            return
        await db.x("UPDATE groups SET link_version=link_version+1 WHERE chat_id=?", (cid,))
        from . import pin
        await pin.refresh(cid, force=True)
        await call("sendMessage", chat_id=cid, text="🔒 Old voice chat link disabled. The pinned Join button now uses the new link.")


_vc_last: dict[int, float] = {}


async def _sudo_cmd(msg: dict, cmd: str, arg: str):
    """The owner's commands: /addsudo and /delsudo (a user id, an @username, a mention, or as a reply), /sudolist.
    /id tells anyone their own Telegram id (to put in OWNER_ID)."""
    from . import sudo
    chat_id, frm = msg["chat"]["id"], msg.get("from") or {}
    say = lambda text: call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                            reply_parameters={"message_id": msg["message_id"], "allow_sending_without_reply": True})
    if cmd == "/id":
        target = (msg.get("reply_to_message") or {}).get("from") or frm
        return await say(f"🆔 <b>{_esc(_full_name(target))}</b>: <code>{target.get('id')}</code>")
    if not sudo.is_owner(frm.get("id", 0)):
        if msg["chat"]["type"] == "private":  # (in groups: no reply, nobody else needs to see it)
            await say("Only the bot's owner can manage sudo users." if config.OWNER_ID else
                      f"No owner is set yet. Put your Telegram id in the <code>OWNER_ID</code> variable on Railway: "
                      f"<code>{frm.get('id')}</code>")
        return
    if cmd in ("/sudolist", "/sudo") and not arg and not msg.get("reply_to_message"):
        rows = await sudo.listing()
        if not rows:
            return await say("No sudo users yet. Add one: <code>/addsudo @username</code>, a user id, or reply to "
                             "their message with /addsudo")
        lines = [f"• {_esc(r['name'] or 'User')}" + (f" @{_esc(r['username'])}" if r["username"] else "")
                 + f" — <code>{r['uid']}</code>" for r in rows]
        return await say(f"🛡 <b>Sudo users ({len(rows)})</b>\n" + "\n".join(lines))
    # who: a reply, a mention without a username (text_mention), else what was typed
    who = None
    if (rep := msg.get("reply_to_message")) and (u := rep.get("from")) and not u.get("is_bot"):
        who = {"uid": u["id"], "name": _full_name(u), "username": u.get("username") or ""}
    for e in msg.get("entities") or []:
        if not who and e.get("type") == "text_mention" and e.get("user"):
            u = e["user"]
            who = {"uid": u["id"], "name": _full_name(u), "username": u.get("username") or ""}
    if not who and arg:
        who = await sudo.find(arg.split()[0])
    if not who:
        return await say("Who? Send <code>/addsudo @username</code> or a user id, or reply to their message."
                         if arg == "" else "I don't know that username yet — ask them to open the voice chat once (or "
                         "write in a group with me), or use their numeric id (they can send me /id).")
    label = f"<b>{_esc(who.get('name') or 'User')}</b>" + (f" @{_esc(who['username'])}" if who.get("username") else "")
    if cmd == "/addsudo":
        if sudo.is_owner(who["uid"]):
            return await say("That's you — the owner always has access.")
        await sudo.add(who["uid"], who.get("name") or "", who.get("username") or "", frm["id"])
        return await say(f"✅ {label} is a sudo user now (they see the Health page)")
    gone = await sudo.remove(who["uid"])
    await say(f"🗑 {label} is no longer a sudo user" if gone else f"{label} wasn't a sudo user")


def _esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


STATS: dict = {"poll_ok_at": 0.0, "poll_err_at": 0.0, "poll_err": ""}  # for the admins' Health page
PRIVACY_ON = False  # privacy mode on = the bot doesn't see normal group messages (set from getMe)


async def run_polling():
    global BOT_USERNAME, PRIVACY_ON
    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN missing; bot disabled")
        return
    while True:
        try:
            me = await call("getMe")
            BOT_USERNAME = me["username"]
            PRIVACY_ON = not me.get("can_read_all_group_messages")
            if PRIVACY_ON:
                log.warning("Bot privacy mode is ON: the bot can't see group messages, so the invite list only fills "
                            "up slowly. Turn it off: @BotFather -> /setprivacy -> your bot -> Disable")
            base = [
                {"command": "vc", "description": "Open the voice chat"},
                {"command": "play", "description": "How to play music in the voice chat"},
                {"command": "resetlink", "description": "Admins: make a new private link"},
            ]
            await call("setMyCommands", commands=base)
            if config.OWNER_ID:  # the owner's own chat with the bot also lists the sudo commands
                try:
                    await call("setMyCommands", scope={"type": "chat", "chat_id": config.OWNER_ID}, commands=base + [
                        {"command": "addsudo", "description": "Add a sudo user (id, @username or reply)"},
                        {"command": "delsudo", "description": "Remove a sudo user"},
                        {"command": "sudolist", "description": "List sudo users"},
                        {"command": "id", "description": "Show a Telegram id"},
                    ])
                except Exception as e:
                    log.info("owner commands: %s", e)
            break
        except Exception as e:
            log.error("getMe failed: %s (retrying)", e)
            await asyncio.sleep(10)
    try:
        # a webhook set earlier (another host, an old setup) makes every getUpdates fail with 409
        await call("deleteWebhook", drop_pending_updates=False)
    except Exception as e:
        log.info("deleteWebhook: %s", e)
    log.info("Bot @%s running", BOT_USERNAME)
    offset = 0
    while True:
        try:
            ups = await call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "edited_message", "my_chat_member", "chat_member", "message_reaction"])
            STATS["poll_ok_at"] = time.time()
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    await _handle(u)
                except Exception:
                    log.exception("update failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATS["poll_err"], STATS["poll_err_at"] = str(e)[:200], time.time()
            log.warning("polling error: %s", e)
            if "Conflict" in str(e) and "other getUpdates" in str(e):
                STATS["conflicts"] = STATS.get("conflicts", 0) + 1
                if STATS["conflicts"] % 20 == 10:  # (a few during a deploy are normal: the old copy is stopping)
                    log.error("Another copy of this bot keeps polling Telegram (another server, service or PC uses "
                              "the same BOT_TOKEN). Stop it, or the bot misses messages.")
            await asyncio.sleep(3)


async def schedule_loop():
    while True:
        await asyncio.sleep(20)
        try:
            due = await db.q("SELECT * FROM schedules WHERE sent=0 AND at<=?", (time.time() + 30,))
            for s in due:
                await db.x("UPDATE schedules SET sent=1 WHERE id=?", (s["id"],))
                from . import pin
                pin.schedule(s["chat_id"], 0)
                try:
                    await send_join_message(s["chat_id"], f"⏰ <b>Voice chat is starting!</b>\n{_esc(s['title'] or '')}")
                except Exception as e:
                    log.warning("schedule send failed: %s", e)
        except Exception:
            log.exception("schedule loop")


# ---- profiles: bio + profile photos (served through our server so the bot token never reaches clients) ----
AV_DIR = os.path.join(config.DATA_DIR, "avatars")
os.makedirs(AV_DIR, exist_ok=True)
_av_locks: dict[int, asyncio.Lock] = {}


async def fetch_bio(uid: int) -> str | None:
    try:
        c = await call("getChat", chat_id=uid)
        return (c.get("bio") or "")[:140]
    except Exception:
        return None  # unknown (not "no bio"): the caller keeps the one it has


async def _download_file(file_id: str) -> bytes | None:
    f = await call("getFile", file_id=file_id)
    r = await client().get(f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{f['file_path']}")
    return r.content if r.status_code == 200 else None


async def avatar_path(entity_id: int, fallback_url: str = "") -> str | None:
    """Cached profile photo for a user (positive id) or group (negative id)."""
    path = os.path.join(AV_DIR, f"{entity_id}.jpg")
    miss = path + ".none"
    now = time.time()
    if os.path.exists(path) and now - os.path.getmtime(path) < 86400:
        return path
    if os.path.exists(miss) and now - os.path.getmtime(miss) < 6 * 3600:
        return None
    if len(_av_locks) > 2000:
        for k in [k for k, lk in _av_locks.items() if not lk.locked()]:
            _av_locks.pop(k, None)
    lock = _av_locks.setdefault(entity_id, asyncio.Lock())
    async with lock:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            return path
        data, answered = None, False
        try:
            if entity_id > 0:
                r = await call("getUserProfilePhotos", user_id=entity_id, limit=1)
                if r.get("photos"):
                    sizes = r["photos"][0]
                    pick = next((x for x in sizes if x.get("width", 0) >= 300), sizes[-1])
                    data = await _download_file(pick["file_id"])
                answered = True
            else:
                c = await call("getChat", chat_id=entity_id)
                if c.get("photo"):
                    data = await _download_file(c["photo"]["big_file_id"])
                answered = True
        except Exception as e:
            log.info("avatar %s: %s", entity_id, e)
        if not data and fallback_url:
            try:
                r = await client().get(fallback_url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    data = r.content
            except Exception:
                pass
        if data:
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)  # atomic: a request never sees a half-written photo
            if os.path.exists(miss):
                os.remove(miss)
            return path
        if answered:
            open(miss, "w").close()  # really no photo: don't ask again for 6 hours
        elif not os.path.exists(miss):
            # a failed request: try again in a few minutes (a "no photo" mark hid the avatar for 6 hours)
            open(miss, "w").close()
            os.utime(miss, (now - 6 * 3600 + 300, now - 6 * 3600 + 300))
        if os.path.exists(path):  # stale but better than nothing
            return path
        return None
