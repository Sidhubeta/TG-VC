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


async def call(method: str, **params):
    r = await client().post(f"{API}/{method}", json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


async def call_multipart(method: str, data: dict, files: dict):
    r = await client().post(f"{API}/{method}", data=data, files=files, timeout=60)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"{method}: {res.get('description')}")
    return res["result"]


async def group_link(chat_id: int) -> str:
    g = await db.get_group(chat_id)
    token = auth.make_group_token(chat_id, g["link_version"])
    mode = "&mode=fullscreen" if config.FULLSCREEN else ""
    return f"https://t.me/{BOT_USERNAME}/{config.APP_SHORT_NAME}?startapp={token}{mode}"


# ---- admin DM alerts (reports, join floods) ----
_admins_cache: dict[int, tuple[float, list[int]]] = {}
_alert_last: dict[tuple[int, str], float] = {}


async def group_admins(chat_id: int) -> list[int]:
    hit = _admins_cache.get(chat_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        res = await call("getChatAdministrators", chat_id=chat_id)
        ids = [a["user"]["id"] for a in res if not a["user"].get("is_bot")]
    except Exception:
        ids = hit[1] if hit else []
    _admins_cache[chat_id] = (time.time(), ids)
    return ids


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


async def member_status(chat_id: int, uid: int, fresh: bool = False) -> dict:
    """Returns {'member': bool, 'admin': bool}. Cached for 60s."""
    if config.DEV_MODE and uid < 0:  # fake dev users
        return {"member": True, "admin": uid == -1}
    key = (chat_id, uid)
    hit = _member_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < 60:
        return hit[1]
    try:
        m = await call("getChatMember", chat_id=chat_id, user_id=uid)
        st = m.get("status")
        res = {
            "member": st in ("creator", "administrator", "member") or (st == "restricted" and m.get("is_member")),
            "admin": st in ("creator", "administrator"),
        }
    except Exception as e:
        log.warning("getChatMember failed %s %s: %s", chat_id, uid, e)
        res = {"member": False, "admin": False}
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


def join_button(url: str, text: str = "🎧 Join Voice Chat"):
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


async def send_join_message(chat_id: int, text: str):
    url = await group_link(chat_id)
    await call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=join_button(url))


async def send_recording(uid: int, chat_id: int, data: bytes, filename: str, caption: str):
    files = {"audio": (filename, data)}
    for target in (uid, chat_id):
        try:
            r = await client().post(f"{API}/sendDocument", data={"chat_id": str(target), "caption": caption},
                                    files={"document": files["audio"]}, timeout=300)
            if r.json().get("ok"):
                return target
        except Exception as e:
            log.warning("recording send failed to %s: %s", target, e)
    return None


# ---- update handling ----------------------------------------------------------------------
async def _handle(upd: dict):
    msg = upd.get("message")
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
    text = msg["text"].split()[0].lower()
    cmd = text.split("@")[0]
    if "@" in text and text.split("@")[1] != BOT_USERNAME.lower():
        return
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
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        try:
            await pin.post(cid, thread)
        except Exception as e:
            log.warning("pin post failed: %s", e)
            await send_join_message(cid, f"🎙 <b>{_esc(chat.get('title', 'Voice Chat'))}</b>\nTap below to join the voice chat.")
    elif cmd == "/resetlink":
        st = await member_status(cid, msg["from"]["id"], fresh=True)
        if not st["admin"]:
            return
        await db.x("UPDATE groups SET link_version=link_version+1 WHERE chat_id=?", (cid,))
        from . import pin
        await pin.refresh(cid, force=True)
        await call("sendMessage", chat_id=cid, text="🔒 Old voice chat link disabled. The pinned Join button now uses the new link.")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def run_polling():
    global BOT_USERNAME
    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN missing; bot disabled")
        return
    while True:
        try:
            me = await call("getMe")
            BOT_USERNAME = me["username"]
            await call("setMyCommands", commands=[
                {"command": "vc", "description": "Open the voice chat"},
                {"command": "resetlink", "description": "Admins: make a new private link"},
            ])
            break
        except Exception as e:
            log.error("getMe failed: %s (retrying)", e)
            await asyncio.sleep(10)
    log.info("Bot @%s running", BOT_USERNAME)
    offset = 0
    while True:
        try:
            ups = await call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "my_chat_member"])
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    await _handle(u)
                except Exception:
                    log.exception("update failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("polling error: %s", e)
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


async def fetch_bio(uid: int) -> str:
    try:
        c = await call("getChat", chat_id=uid)
        return (c.get("bio") or "")[:140]
    except Exception:
        return ""


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
    lock = _av_locks.setdefault(entity_id, asyncio.Lock())
    async with lock:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            return path
        data = None
        try:
            if entity_id > 0:
                r = await call("getUserProfilePhotos", user_id=entity_id, limit=1)
                if r.get("photos"):
                    sizes = r["photos"][0]
                    pick = next((x for x in sizes if x.get("width", 0) >= 300), sizes[-1])
                    data = await _download_file(pick["file_id"])
            else:
                c = await call("getChat", chat_id=entity_id)
                if c.get("photo"):
                    data = await _download_file(c["photo"]["big_file_id"])
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
            with open(path, "wb") as fh:
                fh.write(data)
            if os.path.exists(miss):
                os.remove(miss)
            return path
        open(miss, "w").close()
        if os.path.exists(path):  # stale but better than nothing
            return path
        return None
