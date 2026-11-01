"""The assistant: a normal Telegram account the server uses to play the room's music into the group's own Telegram
voice chat (like the big Telegram music bots do).

Why: a web page can't keep playing once the phone puts Telegram in the background, and an iPhone turns all its sound
into call sound while a mic is on. Telegram's own voice chat has neither problem: Telegram itself keeps it going in
the background, on the lock screen, over Bluetooth, on iPhone and Android. The app stays the remote (queue, search,
effects, playlists); the sound comes through Telegram's voice chat.

Needs TG_API_ID / TG_API_HASH (my.telegram.org), MUSIC_IN_TG_VC=1, and the assistant logged in once — on the Health
page (the bot's owner: phone number, the code Telegram sends, the 2-step password if there is one). The login is kept
in the database. The assistant joins each group by itself (through an invite link the bot makes) and starts the voice
chat if none is running (it needs to be an admin for that: the bot makes it one if it may)."""
import asyncio
import logging
import time

from . import config, db

log = logging.getLogger("assistant")

try:
    from telethon import TelegramClient, functions
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover
    TelegramClient = None
try:
    from pytgcalls import PyTgCalls
    from pytgcalls import filters as tgfilters
    from pytgcalls.types import ChatUpdate, Device, ExternalMedia, MediaStream
    from pytgcalls.types.raw import AudioParameters
except Exception as _e:  # pragma: no cover
    PyTgCalls = None
    log.info("py-tgcalls not available: %s", _e)

RATE, CH = 48000, 2
_client = None
_calls = None
_me = None
_lock = asyncio.Lock()
_failed_at = 0.0
_err = ""
_ended: set[int] = set()          # chats whose call ended (the voice chat closed, the assistant was removed)
_joined: set[int] = set()         # chats the assistant is in the call of
_links: dict[int, tuple[float, str]] = {}
_login: dict = {}                 # a login in progress: {"client", "phone", "hash", "at"}


def available() -> bool:
    return bool(TelegramClient and PyTgCalls and config.TG_API_ID and config.TG_API_HASH)


def enabled() -> bool:
    return bool(config.MUSIC_IN_TG_VC) and available()


async def _session() -> str:
    try:
        row = await db.q1("SELECT v FROM kv WHERE k='assistant_session'")
    except Exception:
        row = None
    return ((row or {}).get("v") or config.ASSISTANT_SESSION or "").strip()


async def calls():
    """The running PyTgCalls of the logged-in assistant, or None (not logged in / failed a moment ago)."""
    global _client, _calls, _me, _failed_at, _err
    if _calls is not None and _client is not None and _client.is_connected():
        return _calls
    if time.time() - _failed_at < 30:
        return None
    async with _lock:
        if _calls is not None and _client is not None and _client.is_connected():
            return _calls
        s = await _session()
        if not s:
            _err = "Not logged in yet"
            return None
        c = None
        try:
            c = TelegramClient(StringSession(s), int(config.TG_API_ID), config.TG_API_HASH)
            await c.connect()
            if not await c.is_user_authorized():
                raise RuntimeError("the assistant's login ended — log in again on the Health page")
            me = await c.get_me()
            if getattr(me, "bot", False):
                raise RuntimeError("the assistant must be a normal Telegram account, not a bot")
            p = PyTgCalls(c)
            await p.start()

            @p.on_update(tgfilters.chat_update(ChatUpdate.Status.LEFT_CALL))
            async def _gone(_, update):  # the voice chat ended, or the assistant was removed from it
                _ended.add(update.chat_id)
                _joined.discard(update.chat_id)

            _client, _calls, _me, _err = c, p, me, ""
            _joined.clear()
            log.info("assistant @%s ready", getattr(me, "username", "") or me.id)
            return p
        except Exception as e:
            _failed_at, _err = time.time(), str(e)[:200]
            log.warning("assistant: %s", e)
            try:
                if c is not None:
                    await c.disconnect()
            except Exception:
                pass
            return None


async def _ensure_member(chat_id: int):
    """The assistant must be in the group: if it isn't, the bot makes an invite link and the assistant joins with it.
    Then the bot tries to make it an admin that may start voice chats (needs the bot's own "add admins" right)."""
    from . import tg
    try:
        await _client.get_permissions(chat_id, "me")
        member = True
    except Exception:
        member = False
    if not member:
        link = await tg.call("createChatInviteLink", chat_id=chat_id, name="Music assistant", member_limit=1)
        h = link["invite_link"].rsplit("/", 1)[-1].lstrip("+")
        try:
            await _client(functions.messages.ImportChatInviteRequest(h))
        except Exception as e:
            if "already" not in str(e).lower():
                raise RuntimeError(f"the assistant couldn't join the group: {e}")
    try:
        await tg.call("promoteChatMember", chat_id=chat_id, user_id=_me.id, can_manage_video_chats=True,
                      can_invite_users=True)
    except Exception as e:  # (not allowed: an admin can start the voice chat instead, or make it admin)
        log.info("assistant: not made an admin in %s: %s", chat_id, e)


async def join_call(chat_id: int):
    """Into the group's Telegram voice chat (started if none is running), ready for our sound."""
    p = await calls()
    if p is None:
        raise RuntimeError(f"assistant not ready: {_err or 'not logged in'}")
    await _ensure_member(chat_id)
    _ended.discard(chat_id)
    try:
        await p.play(chat_id, MediaStream(ExternalMedia.AUDIO, AudioParameters(RATE, CH),
                                          video_flags=MediaStream.Flags.IGNORE))
    except Exception as e:
        raise RuntimeError(f"couldn't join the group's voice chat ({e.__class__.__name__}: {e}) — start a voice chat "
                           "in the group, or make the assistant an admin that may manage voice chats")
    _joined.add(chat_id)


def in_call(chat_id: int) -> bool:
    return chat_id in _joined and chat_id not in _ended and _client is not None and _client.is_connected()


async def send(chat_id: int, pcm: bytes):
    """10 ms of 48 kHz stereo 16-bit sound."""
    await _calls.send_frame(chat_id, Device.MICROPHONE, pcm)


async def leave(chat_id: int):
    _joined.discard(chat_id)
    try:
        if _calls is not None:
            await _calls.leave_call(chat_id)
    except Exception:
        pass


async def listen_link(chat_id: int) -> str:
    """A link that opens the group's voice chat (or at least the group) in Telegram."""
    hit = _links.get(chat_id)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    link = ""
    try:
        if await calls() is not None:
            ent = await _client.get_input_entity(chat_id)
            if hasattr(ent, "channel_id"):
                full = await _client(functions.channels.GetFullChannelRequest(ent))
            else:
                full = await _client(functions.messages.GetFullChatRequest(ent.chat_id))
            if full.full_chat.call:
                res = await _client(functions.phone.ExportGroupCallInviteRequest(call=full.full_chat.call))
                link = res.link
    except Exception as e:
        log.info("voice chat link for %s: %s", chat_id, e)
    if not link:
        try:
            from . import tg
            link = await tg.call("exportChatInviteLink", chat_id=chat_id)
        except Exception as e:
            log.info("group link for %s: %s", chat_id, e)
    if link:
        _links[chat_id] = (time.time(), link)
    return link


# ---- logging the assistant in (the bot's owner, on the Health page) ----
async def login_start(phone: str) -> dict:
    if not available():
        return {"ok": False, "error": "Set TG_API_ID and TG_API_HASH (my.telegram.org) first"}
    await _login_drop()
    c = TelegramClient(StringSession(), int(config.TG_API_ID), config.TG_API_HASH)
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
    except Exception as e:
        await c.disconnect()
        return {"ok": False, "error": f"Telegram said: {e}"}
    _login.update(client=c, phone=phone, hash=sent.phone_code_hash, at=time.time())
    return {"ok": True, "next": "code"}


async def login_code(code: str) -> dict:
    c = _login.get("client")
    if not c or time.time() - _login.get("at", 0) > 600:
        return {"ok": False, "error": "Start again: the code is only good for a few minutes"}
    try:
        await c.sign_in(_login["phone"], code.replace(" ", ""), phone_code_hash=_login["hash"])
    except SessionPasswordNeededError:
        return {"ok": True, "next": "password"}
    except Exception as e:
        return {"ok": False, "error": f"Telegram said: {e}"}
    return await _login_finish()


async def login_password(password: str) -> dict:
    c = _login.get("client")
    if not c:
        return {"ok": False, "error": "Start again"}
    try:
        await c.sign_in(password=password)
    except Exception as e:
        return {"ok": False, "error": f"Telegram said: {e}"}
    return await _login_finish()


async def _login_finish() -> dict:
    global _failed_at
    c = _login["client"]
    me = await c.get_me()
    s = c.session.save()
    await db.x("INSERT OR REPLACE INTO kv(k,v) VALUES('assistant_session', ?)", (s,))
    await _login_drop()
    await _stop()
    _failed_at = 0.0
    name = " ".join(x for x in (me.first_name, me.last_name) if x)
    return {"ok": True, "next": "done", "name": name, "username": me.username or ""}


async def _login_drop():
    c = _login.pop("client", None)
    _login.clear()
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass


async def logout():
    await db.x("DELETE FROM kv WHERE k='assistant_session'")
    await _stop()


async def _stop():
    global _calls, _client, _me
    for chat_id in list(_joined):
        await leave(chat_id)
    c, _client, _calls, _me = _client, None, None, None
    try:
        if c is not None:
            await c.disconnect()
    except Exception:
        pass


def status() -> dict:
    if not config.MUSIC_IN_TG_VC:
        return {"ok": None, "detail": "Off — set MUSIC_IN_TG_VC=1 to play the music in the group's own Telegram voice "
                                      "chat (keeps playing in the background, on iPhone and Android)"}
    if not available():
        return {"ok": False, "detail": "Needs TG_API_ID and TG_API_HASH (my.telegram.org)"
                                       + ("" if PyTgCalls else " and the py-tgcalls package")}
    if _me is not None and _client is not None and _client.is_connected():
        who = f"@{_me.username}" if getattr(_me, "username", None) else str(_me.id)
        return {"ok": True, "detail": f"Assistant {who} · in {len(_joined)} voice chat{'s' if len(_joined) != 1 else ''}",
                "assistant": True}
    return {"ok": False, "detail": f"Assistant not ready: {_err or 'not logged in yet'} — log it in below", "assistant": True}
