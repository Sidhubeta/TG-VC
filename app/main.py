import asyncio
import json
import random
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth, config, db, lk, members, music, pin, tg
from .room import Conn, ROOMS, get_room, heartbeat_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token: keep it out of the logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class Denied(Exception):
    def __init__(self, msg: str, code: int = 403):
        self.msg, self.code = msg, code


def err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _vid(request: Request) -> str:
    vid = request.path_params["vid"]
    if not music.valid_id(vid):
        raise Denied("Not found", 404)
    return vid


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else default
    except (TypeError, ValueError):
        return default


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise Denied("Bad request", 400)
    if not isinstance(body, dict):
        raise Denied("Bad request", 400)
    return body


async def is_banned(chat_id: int, uid: int) -> bool:
    return bool(await db.q1("SELECT 1 FROM bans WHERE chat_id=? AND uid=?", (chat_id, uid)))


async def session(request: Request, need_admin: bool = False) -> tuple[int, int, dict]:
    tok = request.headers.get("x-session") or request.query_params.get("s") or ""
    try:
        uid, chat_id = auth.read_session(tok)
    except ValueError:
        raise Denied("Session expired — reopen the app", 401)
    st = await tg.member_status(chat_id, uid)
    if not st["member"] or await is_banned(chat_id, uid):
        raise Denied("Not allowed")
    if need_admin and not st["admin"]:
        raise Denied("Admins only")
    return uid, chat_id, st


def guarded(need_admin: bool = False):
    def deco(fn):
        async def wrapper(request: Request):
            try:
                uid, chat_id, st = await session(request, need_admin)
            except Denied as d:
                return err(d.msg, d.code)
            try:
                return await fn(request, uid, chat_id, st)
            except Denied as d:
                return err(d.msg, d.code)
            except Exception as e:
                log.exception("api error")
                return err(f"Something went wrong: {str(e)[:150]}", 500)
        return wrapper
    return deco


# ---------------- auth ----------------
async def api_auth(request: Request):
    try:
        body = await _json(request)
    except Denied as d:
        return err(d.msg, d.code)
    if config.DEV_MODE and body.get("dev"):
        uid = -abs(int(body.get("uid", 1)))
        user = {"id": uid, "first_name": body.get("name", f"Dev {abs(uid)}"), "username": f"dev{abs(uid)}"}
        chat_id = int(body.get("chat", -100123))
        await db.get_group(chat_id)
        await db.x("UPDATE groups SET title=COALESCE(NULLIF(title,''),'Dev Group') WHERE chat_id=?", (chat_id,))
    else:
        try:
            data = auth.verify_init_data(body.get("initData", ""))
        except ValueError:
            return err("Please open this app from Telegram", 401)
        user = data["user"]
        uid = int(user["id"])
        sp = data.get("start_param") or ""
        if not sp:
            return err("Open the voice chat from your group's link (send /vc in the group)", 403)
        try:
            chat_id, version = auth.parse_group_token(sp)
        except ValueError:
            return err("Invalid link", 403)
        g = await db.get_group(chat_id)
        if version != g["link_version"]:
            return err("This link was reset by an admin. Ask for the new link (/vc in the group)", 403)
    st = await tg.member_status(chat_id, uid, fresh=True)
    if not st["member"]:
        return err("Only members of this group can join", 403)
    if await is_banned(chat_id, uid):
        return err("You are banned from this voice chat", 403)
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip() or "User"
    old = await db.q1("SELECT bio, updated FROM users WHERE uid=?", (uid,))
    bio = (old or {}).get("bio") or ""
    if uid > 0 and (not old or time.time() - (old.get("updated") or 0) > 1800):
        bio = await tg.fetch_bio(uid)
    if config.DEV_MODE and uid < 0:
        bio = ["Music lover 🎧", "", "Living one song at a time", "", "Punjab ⇄ Toronto", ""][abs(uid) % 6]
    await db.x("INSERT OR REPLACE INTO users(uid,name,username,photo,bio,updated) VALUES(?,?,?,?,?,?)",
               (uid, name[:64], user.get("username") or "", user.get("photo_url") or "", bio, time.time()))
    room = await get_room(chat_id)
    if room.settings["locked"] and not st["admin"] and uid not in room.parts:
        return err("The voice chat is locked by admins", 403)
    can_pub = st["admin"] or room.settings["new_can_speak"] or (uid in room.parts and room.can_speak(room.parts[uid]))
    out = {"session": auth.make_session(uid, chat_id), "uid": uid, "chat": chat_id, "admin": st["admin"], "bot": tg.BOT_USERNAME,
           "group": "Dev Group" if config.DEV_MODE and uid < 0 else await tg.chat_title(chat_id),
           "livekit": {"url": config.LIVEKIT_URL, "token": lk.join_token(chat_id, uid, name, can_pub)} if lk.enabled() else None}
    return JSONResponse(out)


async def api_lk_token(request: Request):
    """Fresh LiveKit token reflecting current permissions (used on reconnect)."""
    try:
        uid, chat_id, st = await session(request)
    except Denied as d:
        return err(d.msg, d.code)
    if not lk.enabled():
        return JSONResponse({"livekit": None})
    room = await get_room(chat_id)
    p = room.parts.get(uid)
    u = await db.q1("SELECT name FROM users WHERE uid=?", (uid,))
    can = room.can_speak(p) if p else (st["admin"] or room.settings["new_can_speak"])
    return JSONResponse({"livekit": {"url": config.LIVEKIT_URL, "token": lk.join_token(chat_id, uid, (u or {}).get("name", "User"), can)}})


# ---------------- music ----------------
@guarded()
async def api_search(request: Request, uid, chat_id, st):
    q = (request.query_params.get("q") or "").strip()[:200]
    if not q:
        return JSONResponse({"tracks": []})
    vid, pl = music.parse_youtube(q)
    if pl:
        title, tracks = await music.playlist(pl)
        return JSONResponse({"playlist": {"id": pl, "title": title, "tracks": tracks}})
    if vid:
        info = await music.resolve(vid)
        return JSONResponse({"tracks": [info["track"]] if info.get("track") else []})
    return JSONResponse({"tracks": await music.search(q)})


async def api_stream(request: Request):
    try:
        await session(request)
    except Denied as d:
        return Response(d.msg, status_code=d.code)
    vid = request.path_params["vid"]
    kind = request.path_params["kind"]
    if kind not in ("audio", "video") or not music.valid_id(vid):
        return Response(status_code=404)
    return await music.stream(request, vid, kind, request.query_params.get("c", "aac"),
                              request.query_params.get("q", "medium"))


async def api_avatar(request: Request):
    try:
        uid, chat_id, _ = await session(request)
    except Denied as d:
        return Response(status_code=d.code)
    try:
        eid = int(request.path_params["eid"])
    except ValueError:
        return Response(status_code=404)
    # only people/groups relevant to this room: the group, people who joined its voice chat, members the bot
    # has seen in the group, and its admins (the invite list shows all of these)
    if eid != chat_id and not (
            await db.q1("SELECT 1 FROM sessions WHERE chat_id=? AND uid=? LIMIT 1", (chat_id, eid))
            or await db.q1("SELECT 1 FROM members WHERE chat_id=? AND uid=?", (chat_id, eid))
            or any(a["uid"] == eid and not a["anonymous"] for a in await tg.admin_list(chat_id))):
        return Response(status_code=404)
    u = await db.q1("SELECT photo FROM users WHERE uid=?", (eid,)) if eid > 0 else None
    path = await tg.avatar_path(eid, (u or {}).get("photo") or "")
    if not path:
        return Response(status_code=404, headers={"Cache-Control": "private, max-age=3600"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


EMOJI_DIR = os.path.join(config.DATA_DIR, "emoji")
os.makedirs(EMOJI_DIR, exist_ok=True)
EMOJI_CDN = "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15/img/apple/64/"


async def emoji(request: Request):
    """Apple-style emoji images, cached on our server (same on Android and iPhone)."""
    name = request.path_params["name"]
    if not re.fullmatch(r"[0-9a-f]{2,6}(-[0-9a-f]{2,6}){0,9}\.png", name):
        return Response(status_code=404)
    path = os.path.join(EMOJI_DIR, name)
    if not os.path.exists(path):
        try:
            r = await music.http().get(EMOJI_CDN + name, timeout=8)
        except Exception:
            return Response(status_code=502)
        if r.status_code != 200:
            return Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
        tmp = f"{path}.{os.getpid()}.{id(r)}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, path)  # atomic, so parallel requests never serve a half-written file
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def api_thumb(request: Request):
    try:
        await session(request)
    except Denied as d:
        return Response(status_code=d.code)
    return await music.thumb(request.path_params["vid"])


@guarded()
async def api_formats(request: Request, uid, chat_id, st):
    info = await music.resolve(_vid(request))
    return JSONResponse({"heights": music.video_heights(info)})


@guarded()
async def api_lyrics(request: Request, uid, chat_id, st):
    p = request.query_params
    res = await music.lyrics(_vid(request), p.get("title", "")[:200], p.get("artist", "")[:120],
                             _num(p.get("duration")))
    return JSONResponse(res)


@guarded()
async def api_history(request: Request, uid, chat_id, st):
    rows = await db.q("SELECT vid as id,title,artist,duration,by_name,ts FROM plays WHERE chat_id=? ORDER BY id DESC LIMIT 100", (chat_id,))
    for r in rows:
        r["thumb"] = f"/api/thumb/{r['id']}"
    return JSONResponse({"tracks": rows})


@guarded()
async def api_browse(request: Request, uid, chat_id, st):
    rows = await db.q("SELECT vid as id,title,artist,duration,MAX(ts) AS ts FROM plays WHERE chat_id=? GROUP BY vid ORDER BY ts DESC LIMIT 20", (chat_id,))
    for r in rows:
        r["thumb"] = f"/api/thumb/{r['id']}"
    return JSONResponse({"genres": music.genre_list(), "recent": rows})


@guarded()
async def api_browse_section(request: Request, uid, chat_id, st):
    sec = request.path_params["sec"]
    if sec == "foryou":
        # built from what *this person* listens to and likes (not just what the room played)
        mine = await db.q("""SELECT vid, COUNT(*) AS n, MAX(ts) AS last FROM listens WHERE uid=? AND ts > ?
                             GROUP BY vid ORDER BY n DESC, last DESC LIMIT 20""", (uid, time.time() - 30 * 86400))
        recent = await db.q("SELECT vid FROM listens WHERE uid=? ORDER BY id DESC LIMIT 15", (uid,))
        liked = await db.q("""SELECT i.track FROM playlist_items i JOIN playlists p ON p.id=i.playlist_id
                              WHERE p.uid=? AND p.name='Liked Songs' ORDER BY i.id DESC LIMIT 10""", (uid,))
        liked_ids = [json.loads(it["track"])["id"] for it in liked]
        seeds = [r["vid"] for r in mine[:2]] + random.sample(liked_ids, min(1, len(liked_ids)))
        if len(seeds) < 3:  # new listener: fall back to what the room plays
            rows = await db.q("SELECT vid, MAX(ts) AS ts FROM plays WHERE chat_id=? GROUP BY vid ORDER BY ts DESC LIMIT 5", (chat_id,))
            seeds += [r["vid"] for r in rows]
        seeds = list(dict.fromkeys(seeds))[:3]
        if not seeds:
            return JSONResponse({"title": "Made for you", "tracks": (await music.collection("trending"))["tracks"][:20], "fallback": True})
        exclude = {r["vid"] for r in recent} | set(liked_ids[:5])
        return JSONResponse({"title": "Made for you", "tracks": await music.recommend(seeds, exclude), "personal": bool(mine or liked_ids)})
    try:
        return JSONResponse(await music.collection(sec))
    except KeyError:
        return err("Not found", 404)


# ---------------- playlists (per user) ----------------
LIKED = "Liked Songs"


async def _liked_id(uid: int) -> int:
    r = await db.q1("SELECT id FROM playlists WHERE uid=? AND name=?", (uid, LIKED))
    return r["id"] if r else await db.x("INSERT INTO playlists(uid,name,created) VALUES(?,?,?)", (uid, LIKED, time.time()))


def _clean_track(t) -> dict | None:
    if not isinstance(t, dict):
        return None
    vid = str(t.get("id", ""))
    if not music.valid_id(vid):
        return None
    return {"id": vid, "title": str(t.get("title", ""))[:200], "artist": str(t.get("artist", ""))[:120],
            "duration": _num(t.get("duration")), "thumb": f"/api/thumb/{vid}"}


async def _add_items(pid: int, tracks) -> None:
    if not isinstance(tracks, list):
        return
    now = time.time()
    await db.xmany("INSERT INTO playlist_items(playlist_id,track,added) VALUES(?,?,?)",
                   [(pid, json.dumps(c), now) for t in tracks[:300] if (c := _clean_track(t))])


@guarded()
async def api_playlists(request: Request, uid, chat_id, st):
    if request.method == "POST":
        body = await _json(request)
        name = str(body.get("name", "")).strip()[:60] or "My Playlist"
        if name == LIKED:
            name = "My Liked Songs"
        pid = await db.x("INSERT INTO playlists(uid,name,created) VALUES(?,?,?)", (uid, name, time.time()))
        await _add_items(pid, body.get("tracks"))
        return JSONResponse({"id": pid})
    await _liked_id(uid)
    rows = await db.q("""SELECT p.id, p.name, COUNT(i.id) AS count,
        (SELECT track FROM playlist_items WHERE playlist_id=p.id ORDER BY id LIMIT 1) AS first
        FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id=p.id WHERE p.uid=? GROUP BY p.id
        ORDER BY (p.name=?) DESC, p.id DESC""", (uid, LIKED))
    for r in rows:
        f = json.loads(r.pop("first") or "null")
        r["thumb"] = f["thumb"] if f else ""
    return JSONResponse({"playlists": rows})


@guarded()
async def api_playlist(request: Request, uid, chat_id, st):
    pid = int(request.path_params["pid"])
    pl = await db.q1("SELECT * FROM playlists WHERE id=? AND uid=?", (pid, uid))
    if not pl:
        return err("Not found", 404)
    if request.method == "DELETE":
        if pl["name"] == LIKED:
            return err("Can't delete Liked Songs")
        await db.x("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        await db.x("DELETE FROM playlists WHERE id=?", (pid,))
        return JSONResponse({"ok": True})
    if request.method == "POST":
        body = await _json(request)
        if "rename" in body:
            new = str(body["rename"]).strip()[:60]
            # renaming Liked Songs (or to it) would break the likes list
            if not new or pl["name"] == LIKED or new == LIKED:
                return err("Can't use that name")
            await db.x("UPDATE playlists SET name=? WHERE id=?", (new, pid))
        await _add_items(pid, body.get("add"))
        if body.get("remove"):
            await db.x("DELETE FROM playlist_items WHERE id=? AND playlist_id=?", (int(_num(body["remove"])), pid))
        return JSONResponse({"ok": True})
    items = await db.q("SELECT id, track FROM playlist_items WHERE playlist_id=? ORDER BY id", (pid,))
    return JSONResponse({"id": pid, "name": pl["name"],
                         "tracks": [{**json.loads(i["track"]), "item": i["id"]} for i in items]})


@guarded()
async def api_like(request: Request, uid, chat_id, st):
    lid = await _liked_id(uid)
    if request.method == "GET":
        rows = await db.q("SELECT json_extract(track,'$.id') AS vid FROM playlist_items WHERE playlist_id=?", (lid,))
        return JSONResponse({"ids": [r["vid"] for r in rows if r["vid"]]})
    t = _clean_track((await _json(request)).get("track"))
    if not t:
        return err("Bad track")
    hit = await db.q1("SELECT id FROM playlist_items WHERE playlist_id=? AND json_extract(track,'$.id')=?", (lid, t["id"]))
    if hit:
        await db.x("DELETE FROM playlist_items WHERE playlist_id=? AND json_extract(track,'$.id')=?", (lid, t["id"]))
        return JSONResponse({"liked": False})
    await db.x("INSERT INTO playlist_items(playlist_id,track,added) VALUES(?,?,?)", (lid, json.dumps(t), time.time()))
    return JSONResponse({"liked": True})


# ---------------- admin: logs, reports, bans, schedules ----------------
def _range(p) -> tuple[float, float]:
    f = _num(p.get("from"))
    t = _num(p.get("to")) or time.time() + 1
    return f, t


def _who(q: str) -> tuple[str, list]:
    q = q.strip().lstrip("@")
    if not q:
        return "", []
    if q.lstrip("-").isdigit():
        return " AND s.uid=?", [int(q)]
    return " AND (s.name LIKE ? OR s.username LIKE ?)", [f"%{q}%", f"%{q}%"]


@guarded(need_admin=True)
async def api_logs(request: Request, uid, chat_id, st):
    p = request.query_params
    f, t = _range(p)
    w, args = _who(p.get("q", ""))
    lim = max(1, min(int(_num(p.get("limit"), 50) or 50), 200))
    off = max(0, int(_num(p.get("offset"))))
    rows = await db.q(f"""SELECT s.id, s.uid, s.name, s.username, s.joined, COALESCE(s.left, s.last_seen) AS left,
        s.open, s.mic_secs FROM sessions s WHERE s.chat_id=? AND s.joined BETWEEN ? AND ? {w}
        ORDER BY s.joined DESC LIMIT ? OFFSET ?""", [chat_id, f, t, *args, lim, off])
    room = ROOMS.get(chat_id)
    for r in rows:
        if r["open"] and room and r["uid"] in room.parts:
            pp = room.parts[r["uid"]]
            r["left"] = None
            r["mic_secs"] = pp.mic_secs + ((time.time() - pp.mic_since) if pp.mic and pp.mic_since else 0)
    return JSONResponse({"sessions": rows})


@guarded(need_admin=True)
async def api_logs_users(request: Request, uid, chat_id, st):
    """Aggregated per user: joins, total time, mic on/off. Used for search + ranking."""
    p = request.query_params
    f, t = _range(p)
    w, args = _who(p.get("q", ""))
    order = {"time": "total DESC", "joins": "joins DESC", "mic": "mic DESC", "recent": "last DESC"}.get(p.get("sort", "time"), "total DESC")
    rows = await db.q(f"""SELECT s.uid, MAX(s.name) AS name, MAX(s.username) AS username, COUNT(*) AS joins,
        SUM((CASE WHEN s.open=1 THEN ? ELSE COALESCE(s.left, s.last_seen, s.joined) END) - s.joined) AS total,
        SUM(s.mic_secs) AS mic,
        MIN(s.joined) AS first, MAX(s.joined) AS last
        FROM sessions s WHERE s.chat_id=? AND s.joined BETWEEN ? AND ? {w}
        GROUP BY s.uid ORDER BY {order} LIMIT 100""", [time.time(), chat_id, f, t, *args])
    room = ROOMS.get(chat_id)
    for r in rows:
        r["online"] = bool(room and r["uid"] in room.parts)
        if r["online"]:
            pp = room.parts[r["uid"]]
            if pp.mic and pp.mic_since:
                r["mic"] = (r["mic"] or 0) + (time.time() - pp.mic_since)
        r["total"] = max(0, r["total"] or 0)
        r["mic"] = min(r["mic"] or 0, r["total"])
        r["mic_off"] = r["total"] - r["mic"]
    return JSONResponse({"users": rows})


@guarded(need_admin=True)
async def api_reports(request: Request, uid, chat_id, st):
    if request.method == "POST":
        body = await _json(request)
        await db.x("UPDATE reports SET resolved=1 WHERE id=? AND chat_id=?", (int(_num(body.get("id"))), chat_id))
        return JSONResponse({"ok": True})
    rows = await db.q("SELECT * FROM reports WHERE chat_id=? AND resolved=0 ORDER BY id DESC LIMIT 100", (chat_id,))
    bans = await db.q("SELECT * FROM bans WHERE chat_id=? ORDER BY ts DESC", (chat_id,))
    sch = await db.q("SELECT * FROM schedules WHERE chat_id=? AND sent=0 ORDER BY at", (chat_id,))
    return JSONResponse({"reports": rows, "bans": bans, "schedules": sch})


REC_MAX = 50 * 1024 * 1024  # Telegram bots can't upload files bigger than 50 MB


@guarded(need_admin=True)
async def api_recording(request: Request, uid, chat_id, st):
    if int(request.headers.get("content-length") or 0) > REC_MAX + 1024 * 1024:
        return err("The recording is too big for Telegram (max 50 MB). Record shorter parts.", 413)
    form = await request.form(max_part_size=REC_MAX + 1024 * 1024)
    f = form.get("file")
    if not f or isinstance(f, str):
        return err("No file")
    data = await f.read()
    if len(data) > REC_MAX:
        return err("The recording is too big for Telegram (max 50 MB). Record shorter parts.", 413)
    ext = str(form.get("ext") or "webm")
    if ext not in ("webm", "m4a", "ogg", "mp4"):
        ext = "webm"
    room = ROOMS.get(chat_id)
    clean = lambda v: re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(v or "")).strip()
    title = clean(room.settings["title"] if room else "") or "Voice chat"
    custom = clean(form.get("name"))[:60]
    name = f"{custom}.{ext}" if custom else f"{title[:40]} {time.strftime('%Y-%m-%d %H-%M')}.{ext}"
    if not await tg.send_recording(uid, data, name, f"🎙 {custom or 'Recording'} — {title}"):
        return err("Couldn't send the recording to your private chat. Open the bot, press Start, and try again.")
    return JSONResponse({"ok": True, "to": "dm"})


@guarded()
async def api_dm_check(request: Request, uid, chat_id, st):
    """Can the bot message this person privately? (Only if they pressed Start in the bot.)"""
    try:
        await tg.call("sendChatAction", chat_id=uid, action="typing")
        ok = True
    except Exception:
        ok = False
    return JSONResponse({"ok": ok, "bot": tg.BOT_USERNAME})


# ---------------- invites ----------------
_invite_last: dict[tuple, float] = {}  # (chat, invited uid) -> when
_invite_by: dict[tuple, list[float]] = {}  # (chat, inviter) -> recent invite times


@guarded()
async def api_invite(request: Request, uid, chat_id, st):
    """GET: one page of people to invite (?q= search, ?offset=, ?limit=). POST {uid}: invite that person.

    Who is listed: everyone in the group when the full member list is available (TG_API_ID/TG_API_HASH), else
    everyone the bot has seen (admins, people who chatted or joined the voice chat). If the group hides its
    members, normal members see only the admins; admins always see everyone.
    """
    room = await get_room(chat_id)
    in_room = set(room.parts)
    admins = await tg.admin_list(chat_id)
    anon = {a["uid"] for a in admins if a["anonymous"]}
    admin_ids = {a["uid"] for a in admins}
    hidden = await tg.hidden_members(chat_id)
    only_admins = hidden and not st["admin"]
    banned = {r["uid"] for r in await db.q("SELECT uid FROM bans WHERE chat_id=?", (chat_id,))}

    def ok(p: dict) -> bool:
        return (p["uid"] > 0 and p["uid"] != uid and p["uid"] not in in_room and p["uid"] not in banned
                and p["uid"] not in anon and not p.get("bot") and not p.get("deleted"))

    if request.method == "GET":
        q = (request.query_params.get("q") or "").strip().lstrip("@")[:64]
        offset = max(0, int(_num(request.query_params.get("offset"), 0)))
        limit = max(10, min(100, int(_num(request.query_params.get("limit"), 50))))
        source = "seen"
        if only_admins:
            pool = [{"uid": a["uid"], "name": a["name"], "username": a["username"], "admin": True} for a in admins]
            total = None
        else:
            full = await members.page(chat_id, q, offset, limit) if members.enabled() else None
            if full is not None:
                source = "full"
                pool, total = full["people"], full["total"]
            else:
                # everyone the bot has seen: admins first, then the most recently active
                people: dict[int, dict] = {}
                for a in admins:
                    people[a["uid"]] = {"uid": a["uid"], "name": a["name"], "username": a["username"], "admin": True}
                rows = await db.q("SELECT uid, name, username FROM members WHERE chat_id=? ORDER BY seen DESC LIMIT 5000", (chat_id,))
                rows += await db.q("""SELECT s.uid, MAX(s.name) AS name, MAX(s.username) AS username FROM sessions s
                                      WHERE s.chat_id=? AND s.uid > 0 GROUP BY s.uid ORDER BY MAX(s.joined) DESC LIMIT 2000""", (chat_id,))
                for r in rows:
                    people.setdefault(r["uid"], {"uid": r["uid"], "name": r["name"] or "User", "username": r["username"] or "",
                                                 "admin": r["uid"] in admin_ids})
                pool, total = list(people.values()), None
        if total is None:  # filter + page here (the full list comes paged and searched by Telegram)
            ql = q.lower()
            pool = [p for p in pool if ok(p) and (not ql or ql in p["name"].lower() or ql in p["username"].lower())]
            if only_admins:
                pool.sort(key=lambda p: p["name"].lower())
            total, pool = len(pool), pool[offset:offset + limit]
            more = offset + limit < total
        else:
            more = offset + limit < total
            pool = [p for p in pool if ok(p)]
        for p in pool:
            p["photo"] = f"/api/avatar/{p['uid']}"
            p["admin"] = p.get("admin") or p["uid"] in admin_ids
        return JSONResponse({"people": pool, "more": more, "next": offset + limit, "total": total, "hidden": hidden,
                             "only_admins": only_admins, "source": source,
                             # admins get told how to make the list complete
                             "hint": st["admin"] and source == "seen" and not only_admins,
                             "privacy": st["admin"] and tg.PRIVACY_ON})

    body = await _json(request)
    target = int(_num(body.get("uid")))
    if target <= 0 and not (config.DEV_MODE and target < 0):
        return err("You can't invite this person")
    if target == uid or target in in_room:
        return err("They're already here" if target in in_room else "That's you 🙂")
    if target in banned or target in anon:
        return err("You can't invite this person")
    if only_admins and target not in admin_ids:
        return err("This group hides its members: you can invite admins only")
    t = time.time()
    # Inviting several people one after another is normal: allow a burst of 10 a minute per person
    # (an old 4-second gap made every invite after the first one fail when tapped quickly).
    recent = [x for x in _invite_by.get((chat_id, uid), []) if t - x < 60]
    if len(recent) >= 10:
        return err("That's a lot of invites — try again in a minute 🙂", 429)
    info = await tg.member_status(chat_id, target, fresh=True)  # also gives the real name (not trusted from the app)
    name = info.get("name") or "Member"
    if not info["member"]:
        return err(f"{name if info.get('name') else 'This person'} isn't in the group any more")
    if info.get("bot"):
        return err("Bots can't join the voice chat")
    if t - _invite_last.get((chat_id, target), 0) < 300:
        return err(f"{name} was invited a moment ago", 429)
    _invite_by[(chat_id, uid)] = recent + [t]
    _invite_last[(chat_id, target)] = t
    if len(_invite_last) > 5000 or len(_invite_by) > 5000:
        for k in [k for k, v in _invite_last.items() if t - v > 300]:
            _invite_last.pop(k, None)
        for k in [k for k, v in _invite_by.items() if not v or t - v[-1] > 60]:
            _invite_by.pop(k, None)
    me = room.parts.get(uid)
    by = room.group_title if me and me.as_group else (me.name if me else "Someone")
    via = await tg.invite(chat_id, target, name, by, len(room.parts))
    if not via:
        _invite_last.pop((chat_id, target), None)
        return err("Couldn't send the invite — try again")
    return JSONResponse({"ok": True, "via": via, "name": name})


# ---------------- websocket ----------------
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        uid, chat_id = auth.read_session(ws.query_params.get("s", ""))
    except ValueError:
        await ws.close(4001)
        return
    st = await tg.member_status(chat_id, uid, fresh=True)
    if not st["member"] or await is_banned(chat_id, uid):
        await ws.close(4003)
        return
    room = await get_room(chat_id)
    if room.settings["locked"] and not st["admin"] and uid not in room.parts:
        await ws.send_text(json.dumps({"t": "locked"}))
        await ws.close(4004)
        return
    user = await db.q1("SELECT * FROM users WHERE uid=?", (uid,)) or {}
    conn = Conn(ws, uid)
    try:
        await room.join(conn, user, st["admin"], ws.query_params.get("asg") == "1")
        while True:
            raw = await ws.receive_text()
            t = time.time()
            conn.hits.append(t)
            if len(conn.hits) == conn.hits.maxlen and t - conn.hits[0] < 5:
                continue  # flood protection
            if len(raw) > 8000:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if isinstance(msg, dict):
                await room.handle(conn, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws")
    finally:
        conn.stop()
        await room.leave(conn)


async def api_bye(request: Request):
    """Sent by the app when it is closed, so the person leaves the voice chat right away."""
    try:
        uid, chat_id = auth.read_session(request.query_params.get("s", ""))
    except ValueError:
        return Response(status_code=204)
    room = ROOMS.get(chat_id)
    p = room.parts.get(uid) if room else None
    if p:
        for c in list(p.conns):
            await c.close(1000)
            await room.leave(c)
    return Response(status_code=204)


# ---------------- static ----------------
async def index(request: Request):
    return FileResponse(os.path.join(WEB, "index.html"), headers={
        "Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"})


async def health(request: Request):
    return JSONResponse({"ok": True})


async def robots(request: Request):
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(tg.run_polling()), asyncio.create_task(tg.schedule_loop()),
             asyncio.create_task(heartbeat_loop()), asyncio.create_task(pin.startup_reset()),
             asyncio.create_task(pin.countdown_loop())]
    log.info("LiveKit %s", "enabled" if lk.enabled() else "NOT configured (voice disabled)")
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in (music._http, tg._client, lk._client):
        if c is not None:
            await c.aclose()


routes = [
    Route("/", index),
    Route("/health", health),
    Route("/robots.txt", robots),
    Route("/api/auth", api_auth, methods=["POST"]),
    Route("/api/lk", api_lk_token),
    Route("/api/search", api_search),
    Route("/api/stream/{vid}/{kind}", api_stream),
    Route("/api/thumb/{vid}", api_thumb),
    Route("/api/avatar/{eid}", api_avatar),
    Route("/emoji/{name}", emoji),
    Route("/api/formats/{vid}", api_formats),
    Route("/api/lyrics/{vid}", api_lyrics),
    Route("/api/history", api_history),
    Route("/api/browse", api_browse),
    Route("/api/browse/{sec}", api_browse_section),
    Route("/api/playlists", api_playlists, methods=["GET", "POST"]),
    Route("/api/playlists/{pid:int}", api_playlist, methods=["GET", "POST", "DELETE"]),
    Route("/api/like", api_like, methods=["GET", "POST"]),
    Route("/api/logs", api_logs),
    Route("/api/logs/users", api_logs_users),
    Route("/api/reports", api_reports, methods=["GET", "POST"]),
    Route("/api/recording", api_recording, methods=["POST"]),
    Route("/api/dmcheck", api_dm_check),
    Route("/api/invite", api_invite, methods=["GET", "POST"]),
    Route("/api/bye", api_bye, methods=["POST", "GET"]),
    WebSocketRoute("/ws", ws_endpoint),
    Mount("/static", StaticFiles(directory=WEB), name="static"),
]
app = Starlette(routes=routes, lifespan=lifespan)


def run():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, proxy_headers=True, forwarded_allow_ips="*",
                ws_ping_interval=20, ws_ping_timeout=30)  # phones in the background answer late


if __name__ == "__main__":
    run()
