"""Full group member lists for invites, through Telegram's MTProto API (the Bot API can't list members).

Optional: needs TG_API_ID and TG_API_HASH. The bot logs in with its own token (no phone number) and only reads
member lists; it never receives updates this way, so the normal Bot API polling keeps working as before.
"""
import asyncio
import logging
import os
import time

from . import config

log = logging.getLogger("members")
SESSION_FILE = os.path.join(config.DATA_DIR, "mtproto.session")
_client = None
_lock = asyncio.Lock()
_failed_at = 0.0
_cache: dict[tuple, tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(config.TG_API_ID and config.TG_API_HASH and config.BOT_TOKEN)


async def _get_client():
    global _client, _failed_at
    if not enabled():
        return None
    if _client is not None and _client.is_connected():
        return _client
    if time.time() - _failed_at < 60:  # don't hammer Telegram's login after a failure
        return None
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            saved = open(SESSION_FILE).read().strip() if os.path.exists(SESSION_FILE) else ""
            c = TelegramClient(StringSession(saved), int(config.TG_API_ID), config.TG_API_HASH, receive_updates=False)
            await c.start(bot_token=config.BOT_TOKEN)
            with open(SESSION_FILE, "w") as fh:  # reuse the login after restarts (Telegram limits bot logins)
                fh.write(c.session.save())
            _client = c
            log.info("member lists: MTProto connected")
            return c
        except Exception as e:
            _failed_at = time.time()
            log.warning("member lists: MTProto login failed: %s", e)
            return None


def _person(u, admin: bool = False) -> dict:
    name = " ".join(x for x in (u.first_name, u.last_name) if x).strip() or "User"
    return {"uid": u.id, "name": name[:64], "username": u.username or "", "admin": admin,
            "bot": bool(u.bot), "deleted": bool(u.deleted)}


async def page(chat_id: int, q: str = "", offset: int = 0, limit: int = 50) -> dict | None:
    """{'people': [...], 'total': n} for one page of the group's members (search by name / username), or None."""
    key = (chat_id, q.lower(), offset, limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    c = await _get_client()
    if c is None:
        return None
    try:
        from telethon.tl import functions, types
        ent = await c.get_input_entity(chat_id)
        if isinstance(ent, types.InputPeerChannel):
            res = await c(functions.channels.GetParticipantsRequest(
                channel=types.InputChannel(ent.channel_id, ent.access_hash),
                filter=types.ChannelParticipantsSearch(q), offset=offset, limit=min(limit, 200), hash=0))
            users = {u.id: u for u in res.users}
            people = []
            for part in res.participants:
                u = users.get(getattr(part, "user_id", None))
                if u is None:
                    continue
                admin = isinstance(part, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator))
                anon = admin and bool(getattr(getattr(part, "admin_rights", None), "anonymous", False))
                if not anon:
                    people.append(_person(u, admin))
            out = {"people": people, "total": res.count}
        else:  # a basic (non-super) group: everyone comes in one answer
            full = await c(functions.messages.GetFullChatRequest(ent.chat_id))
            admins = {p.user_id for p in getattr(full.full_chat.participants, "participants", [])
                      if isinstance(p, (types.ChatParticipantAdmin, types.ChatParticipantCreator))}
            ql = q.lower()
            everyone = [_person(u, u.id in admins) for u in full.users
                        if not ql or ql in (" ".join(x for x in (u.first_name, u.last_name) if x)).lower() or ql in (u.username or "").lower()]
            out = {"people": everyone[offset:offset + limit], "total": len(everyone)}
    except Exception as e:
        log.warning("member list for %s failed: %s", chat_id, e)
        return None
    _cache[key] = (time.time(), out)
    if len(_cache) > 2000:
        for k in [k for k, v in _cache.items() if time.time() - v[0] > 60]:
            _cache.pop(k, None)
    return out


def status() -> dict:
    if not enabled():
        return {"ok": None, "detail": "Off — set TG_API_ID and TG_API_HASH to list every member"}
    if _client is not None and _client.is_connected():
        return {"ok": True, "detail": "Connected: invite lists show every member"}
    if _failed_at:
        return {"ok": False, "detail": "Login to Telegram failed — check TG_API_ID / TG_API_HASH (see the server log)"}
    return {"ok": None, "detail": "Set up; connects the first time someone opens Invite"}
