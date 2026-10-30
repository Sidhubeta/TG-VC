"""LiveKit: access tokens + server API (Twirp JSON) without extra SDK."""
import logging
import time

import jwt

from . import config
from .http import httpx

log = logging.getLogger("livekit")
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


def enabled() -> bool:
    return bool(config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET)


def room_name(chat_id: int) -> str:
    return f"g{chat_id}"


def join_token(chat_id: int, uid: int, name: str, can_publish: bool, ttl: int = 20 * 60) -> str:
    # Short-lived: a token only matters when joining. LiveKit hands connected apps fresh ones, and the app asks us
    # for a new one when it reconnects (/api/lk, which checks bans). A 6-hour token let a banned person with a
    # modified app get back into the voice for hours.
    now = int(time.time())
    claims = {
        "iss": config.LIVEKIT_API_KEY, "sub": str(uid), "nbf": now - 10, "exp": now + ttl, "name": "",  # (names come from the room, not LiveKit: someone appearing as the group stays hidden)
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
        r = await _http().post(f"{config.LIVEKIT_HTTP_URL}/twirp/livekit.RoomService/{method}", json=body,
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


async def check() -> dict:
    """For the admins' Health page: can the server talk to LiveKit?"""
    if not enabled():
        return {"ok": False, "detail": "Not set up (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)"}
    t = time.time()
    try:
        r = await _http().post(f"{config.LIVEKIT_HTTP_URL}/twirp/livekit.RoomService/ListRooms", json={},
                               headers={"Authorization": f"Bearer {_admin_token('')}"})
        if r.status_code >= 400:
            return {"ok": False, "detail": f"LiveKit answered {r.status_code}: {r.text[:120]} (check the key and secret)"}
        n = len(r.json().get("rooms") or [])
        return {"ok": True, "detail": f"Reachable in {int((time.time() - t) * 1000)} ms · {n} active room{'s' if n != 1 else ''}"}
    except Exception as e:
        return {"ok": False, "detail": f"Can't reach {config.LIVEKIT_HTTP_URL}: {e}"[:200]}
