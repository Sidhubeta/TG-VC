"""The bot's owner (OWNER_ID) and the sudo users the owner adds: the only ones who see the Health page.

The owner adds people with /addsudo (a user id, an @username, or as a reply / mention) or on the Health page;
there's no limit. The list is kept in the database."""
import time

from . import config, db

_ids: set[int] = set()


def load():
    _ids.clear()
    _ids.update(r["uid"] for r in db.q_sync("SELECT uid FROM sudo"))


def is_owner(uid: int) -> bool:
    return (bool(config.OWNER_ID) and uid == config.OWNER_ID) or (config.DEV_MODE and uid == -1)


def is_sudo(uid: int) -> bool:
    return is_owner(uid) or uid in _ids


async def add(uid: int, name: str, username: str, by: int):
    await db.x("INSERT OR REPLACE INTO sudo(uid,name,username,added_by,ts) VALUES(?,?,?,?,?)",
               (uid, (name or "")[:64], (username or "")[:64], by, time.time()))
    _ids.add(uid)


async def remove(uid: int) -> bool:
    had = uid in _ids
    await db.x("DELETE FROM sudo WHERE uid=?", (uid,))
    _ids.discard(uid)
    return had


async def listing() -> list[dict]:
    return await db.q("SELECT uid,name,username,ts FROM sudo ORDER BY ts")


async def find(text: str) -> dict | None:
    """A person from what the owner typed: a numeric id, or an @username of someone the bot has seen (they opened
    the voice chat, or wrote in a group the bot is in). Telegram's bot API can't look up other usernames."""
    t = (text or "").strip()
    if t.lstrip("-").isdigit():
        uid = int(t)
        row = await db.q1("SELECT uid,name,username FROM users WHERE uid=?", (uid,)) \
            or await db.q1("SELECT uid,name,username FROM members WHERE uid=? ORDER BY seen DESC LIMIT 1", (uid,))
        return row or {"uid": uid, "name": "", "username": ""}
    name = t.lstrip("@").lower()
    if not name:
        return None
    return await db.q1("SELECT uid,name,username FROM users WHERE lower(username)=?", (name,)) \
        or await db.q1("SELECT uid,name,username FROM members WHERE lower(username)=? ORDER BY seen DESC LIMIT 1", (name,))
