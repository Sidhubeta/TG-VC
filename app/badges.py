"""Badges people earn in a group's voice chat, from what they do there (songs added, mic time, chat, when they
come). Worked out from the logs every few minutes; shown next to names and in each person's profile."""
import time
from datetime import datetime, timedelta, timezone

from . import config, db

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(config.TIMEZONE)
except Exception:  # no time zone data: India time
    TZ = timezone(timedelta(hours=5, minutes=30))

DAY = 86400
BADGES = {  # id: (emoji, name, what it means)
    "dj": ("🎧", "Top DJ", "Added the most songs this week"),
    "voice": ("🎤", "Voice of the week", "Talked the most this week"),
    "chat": ("💬", "Chatterbox", "Sent the most messages this week"),
    "owl": ("🦉", "Night owl", "In the voice chat after midnight on 3+ nights (2 weeks)"),
    "bird": ("🌅", "Early bird", "In the voice chat 5–8 am on 3+ mornings (2 weeks)"),
    "regular": ("🔥", "Regular", "In the voice chat on 5+ days in the last 2 weeks"),
    "marathon": ("⏱", "Marathon", "Stayed 3+ hours in one go (last 30 days)"),
    "lover": ("💿", "Music lover", "Added 100+ songs here"),
    "new": ("🌱", "New here", "First time in this voice chat"),
}
ORDER = list(BADGES)  # the order they're shown in (the first ones are the rarest)
_cache: dict[int, tuple[float, dict]] = {}


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


async def for_group(chat_id: int, ttl: float = 300) -> dict[int, list[str]]:
    """{uid: [badge ids]} for everyone who earned something in this group."""
    hit = _cache.get(chat_id)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    now = time.time()
    week, fortnight, month = now - 7 * DAY, now - 14 * DAY, now - 30 * DAY
    out: dict[int, list[str]] = {}

    def give(uid, b):
        if uid:
            out.setdefault(uid, []).append(b)

    top = await db.q1("""SELECT by_uid AS uid, COUNT(*) AS n FROM plays WHERE chat_id=? AND ts>? AND by_uid!=0
                         GROUP BY by_uid ORDER BY n DESC LIMIT 1""", (chat_id, week))
    if top and top["n"] >= 5:
        give(top["uid"], "dj")
    top = await db.q1("""SELECT uid, SUM(mic_secs) AS s FROM sessions WHERE chat_id=? AND joined>?
                         GROUP BY uid ORDER BY s DESC LIMIT 1""", (chat_id, week))
    if top and (top["s"] or 0) >= 600:
        give(top["uid"], "voice")
    top = await db.q1("""SELECT uid, COUNT(*) AS n FROM messages WHERE chat_id=? AND ts>?
                         GROUP BY uid ORDER BY n DESC LIMIT 1""", (chat_id, week))
    if top and top["n"] >= 10:
        give(top["uid"], "chat")
    nights: dict[int, set] = {}
    mornings: dict[int, set] = {}
    days: dict[int, set] = {}
    marathon = set()
    for s in await db.q("SELECT uid, joined, last_seen FROM sessions WHERE chat_id=? AND joined>? AND uid!=0",
                        (chat_id, month)):
        start, end = s["joined"], max(s["joined"], s["last_seen"] or s["joined"])
        if end - start >= 3 * 3600:
            marathon.add(s["uid"])
        if start < fortnight:
            continue
        t = start
        while True:  # walk the session hour by hour, in local time
            d = _local(t)
            days.setdefault(s["uid"], set()).add(d.date())
            if 0 <= d.hour < 4:
                nights.setdefault(s["uid"], set()).add(d.date())
            if 5 <= d.hour < 8:
                mornings.setdefault(s["uid"], set()).add(d.date())
            if t >= end:
                break
            t = min(end, t + 3600)
    for uid, ds in nights.items():
        if len(ds) >= 3:
            give(uid, "owl")
    for uid, ds in mornings.items():
        if len(ds) >= 3:
            give(uid, "bird")
    for uid, ds in days.items():
        if len(ds) >= 5:
            give(uid, "regular")
    for uid in marathon:
        give(uid, "marathon")
    for r in await db.q("SELECT by_uid AS uid FROM plays WHERE chat_id=? AND by_uid!=0 GROUP BY by_uid HAVING COUNT(*)>=100", (chat_id,)):
        give(r["uid"], "lover")
    for r in await db.q("SELECT uid FROM sessions WHERE chat_id=? AND uid!=0 GROUP BY uid HAVING MIN(joined)>?", (chat_id, now - 3 * DAY)):
        give(r["uid"], "new")
    for uid in out:
        out[uid] = sorted(set(out[uid]), key=ORDER.index)
    _cache[chat_id] = (now, out)
    if len(_cache) > 2000:
        _cache.clear()
    return out


def joined(chat_id: int, uid: int):
    """Someone came in: if they're not in the cached badges yet (a newcomer), work them out again soon."""
    hit = _cache.get(chat_id)
    if hit and uid not in hit[1]:
        _cache.pop(chat_id, None)


def describe(ids: list[str]) -> list[dict]:
    return [{"id": b, "e": BADGES[b][0], "name": BADGES[b][1], "why": BADGES[b][2]} for b in ids if b in BADGES]
