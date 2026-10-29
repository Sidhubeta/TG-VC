"""LiveKit: access tokens + server API (Twirp JSON) without extra SDK."""
import logging
import time

import jwt

from . import config
from .http import httpx

log = logging.getLogger("livekit")


def enabled() -> bool:
    return bool(config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET)


def room_name(chat_id: int) -> str:
    return f"g{chat_id}"


def join_token(chat_id: int, uid: int, name: str, can_publish: bool, ttl: int = 6 * 3600) -> str:
    now = int(time.time())
    claims = {
        "iss": config.LIVEKIT_API_KEY, "sub": str(uid), "nbf": now - 10, "exp": now + ttl, "name": name,
        "video": {"room": room_name(chat_id), "roomJoin": True, "canSubscribe": True,
                  "canPublish": can_publish, "canPublishData": False},
    }
    return jwt.encode(claims, config.LIVEKIT_API_SECRET, algorithm="HS256")


def _admin_token(room: str) -> str:
    now = int(time.time())
    return jwt.encode({"iss": config.LIVEKIT_API_KEY, "nbf": now - 10, "exp": now + 600,
                       "video": {"room": room, "roomAdmin": True, "roomList": True}},
                      config.LIVEKIT_API_SECRET, algorithm="HS256")


async def _twirp(method: str, body: dict):
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{config.LIVEKIT_HTTP_URL}/twirp/livekit.RoomService/{method}", json=body,
                             headers={"Authorization": f"Bearer {_admin_token(body.get('room', ''))}"})
            if r.status_code >= 400:
                log.info("livekit %s -> %s %s", method, r.status_code, r.text[:200])
                return None
            return r.json()
    except Exception as e:
        log.warning("livekit %s failed: %s", method, e)
        return None


async def set_publish(chat_id: int, uid: int, allowed: bool):
    room = room_name(chat_id)
    await _twirp("UpdateParticipant", {"room": room, "identity": str(uid), "permission": {
        "can_subscribe": True, "can_publish": allowed, "can_publish_data": False}})


async def remove(chat_id: int, uid: int):
    await _twirp("RemoveParticipant", {"room": room_name(chat_id), "identity": str(uid)})
