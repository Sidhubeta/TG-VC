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
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth, badges, config, db, lk, members, music, pin, radio, sudo, tg, voicemusic
from .room import Conn, ROOMS, bg_tick_loop, get_room, heartbeat_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


class _HideSession(logging.Filter):
    """Song, avatar and socket links carry the session (?s=…) — anyone reading the logs could have used it."""
    def filter(self, record):
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"([?&]s=)[^&\s\"]+", r"\1…", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str) and "s=" in record.msg:
            record.msg = re.sub(r"([?&]s=)[^&\s\"]+", r"\1…", record.msg)
        return True


for _name in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_name).addFilter(_HideSession())
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


sudo.load()


def is_sudo(uid: int, st: dict | None = None) -> bool:
    """The bot's owner (OWNER_ID) or a sudo user they added: the only ones who see the Health page. Until OWNER_ID
    is set, the group's own creator sees it (else nobody could, nor find out what's missing)."""
    return sudo.is_sudo(uid) or (not config.OWNER_ID and bool((st or {}).get("owner")))


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
        if version < g["link_version"]:
            return err("This link was reset by an admin. Ask for the new link (/vc in the group)", 403)
        if version > g["link_version"]:
            # A newer link than we know about: our database was lost (a redeploy without a volume) and started
            # over at 1, which made every pinned JOIN button fail with 403. The link is genuine (it's signed), so
            # carry on from its number.
            await db.x("UPDATE groups SET link_version=? WHERE chat_id=?", (version, chat_id))
            log.info("group %s: link version restored to %s", chat_id, version)
    st = await tg.member_status(chat_id, uid, fresh=True)
    if not st["member"]:
        return err("Only members of this group can join", 403)
    if await is_banned(chat_id, uid):
        return err("You are banned from this voice chat", 403)
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip() or "User"
    old = await db.q1("SELECT bio, updated FROM users WHERE uid=?", (uid,))
    bio = (old or {}).get("bio") or ""
    if uid > 0 and (not old or time.time() - (old.get("updated") or 0) > 1800):
        fresh = await tg.fetch_bio(uid)
        if fresh is not None:  # (None: Telegram didn't answer — keep the bio we have instead of wiping it)
            bio = fresh
    if config.DEV_MODE and uid < 0:
        bio = ["Music lover 🎧", "", "Living one song at a time", "", "Punjab ⇄ Toronto", ""][abs(uid) % 6]
    await db.x("INSERT OR REPLACE INTO users(uid,name,username,photo,bio,updated) VALUES(?,?,?,?,?,?)",
               (uid, name[:64], user.get("username") or "", user.get("photo_url") or "", bio, time.time()))
    room = await get_room(chat_id)
    if room.settings["locked"] and not st["admin"] and uid not in room.parts:
        return err("The voice chat is locked by admins", 403)
    can_pub = room.may_publish(uid, st["admin"])
    out = {"session": auth.make_session(uid, chat_id), "uid": uid, "chat": chat_id, "admin": st["admin"], "bot": tg.BOT_USERNAME,
           "group": "Dev Group" if config.DEV_MODE and uid < 0 else await tg.chat_title(chat_id),
           "livekit": lk.join_info(chat_id, uid, can_pub) if lk.enabled() else None}
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
    can = room.may_publish(uid, st["admin"])
    return JSONResponse({"livekit": lk.join_info(chat_id, int(room.lk_id(uid)), can)})


# ---------------- music ----------------
@guarded()
async def api_search(request: Request, uid, chat_id, st):
    q = (request.query_params.get("q") or "").strip()[:200]
    if not q:
        return JSONResponse({"tracks": []})
    sp, sc = music.parse_spotify(q), music.parse_soundcloud(q)
    if sp or sc:  # Spotify / SoundCloud: show what's in the link; adding finds each song on YouTube
        source = "Spotify" if sp else "SoundCloud"
        try:
            name, songs = await (music.spotify(*sp) if sp else music.soundcloud(sc))
        except Exception as e:
            log.info("%s preview failed: %s", source, e)
            return err(f"Couldn't read that {source} link — is it public?")
        kind = sp[0] if sp else ("song" if len(songs) == 1 else "playlist")
        return JSONResponse({"import": {"source": source, "url": q, "kind": kind, "title": name, "count": len(songs), "songs": songs[:50]}})
    vid, pl = music.parse_youtube(q)
    if pl:
        try:
            title, tracks = await music.playlist(pl)
        except Exception as e:
            log.info("search playlist %s: %s", pl, e)
            return err("Couldn't open that playlist — is it public? Try again in a moment")
        return JSONResponse({"playlist": {"id": pl, "title": title, "tracks": tracks}})
    if vid:
        try:
            info = await music.resolve(vid)
        except Exception as e:
            log.info("search link %s: %s", vid, e)
            return err("This video is private, removed or not available" if music.unplayable(e)
                       else "Couldn't look up that link right now — try again in a moment")
        return JSONResponse({"tracks": [info["track"]] if info.get("track") else []})
    return JSONResponse({"tracks": await music.search(q)})


async def api_stream(request: Request):
    try:
        await session(request)
    except Denied as d:
        return Response(d.msg, status_code=d.code)
    vid = request.path_params["vid"]
    kind = request.path_params["kind"]
    if kind not in ("audio", "video", "piece") or not music.valid_id(vid):
        return Response(status_code=404)
    if kind == "piece":
        try:
            k = int(request.query_params.get("k", ""))
        except ValueError:
            return Response("bad piece", status_code=400)
        return await music.piece(vid, k, request.query_params.get("q", "medium"))
    return await music.stream(request, vid, kind, request.query_params.get("c", "aac"),
                              request.query_params.get("q", "medium"))


async def api_radio(request: Request):
    """The room's music as one endless stream, for a minimized app (see radio.py)."""
    try:
        uid, chat_id, _ = await session(request)
    except Denied as d:
        return Response(d.msg, status_code=d.code)
    room = ROOMS.get(chat_id)
    if not room or radio.busy():
        return Response("not available", status_code=503)
    p = room.parts.get(uid)
    request.state.who = f"{p.name if p else uid} ({uid})"

    async def still_allowed() -> bool:  # banned or gone from the group: the stream stops too
        st = await tg.member_status(chat_id, uid)
        return (st["member"] or st.get("unsure", False)) and not await is_banned(chat_id, uid)
    return StreamingResponse(radio.stream(room, request, still_allowed), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


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
_emoji_new: list[float] = []


async def emoji(request: Request):
    """Apple-style emoji images, cached on our server (same on Android and iPhone)."""
    name = request.path_params["name"]
    if not re.fullmatch(r"[0-9a-f]{2,6}(-[0-9a-f]{2,6}){0,9}\.png", name):
        return Response(status_code=404)
    path = os.path.join(EMOJI_DIR, name)
    if not os.path.exists(path):
        # The page loads emoji before anyone is logged in, so this can't ask for a session. Instead the server
        # fetches at most 600 new ones in 10 minutes (anyone could make it download the whole set).
        now_ = time.time()
        while _emoji_new and now_ - _emoji_new[0] > 600:
            _emoji_new.pop(0)
        if len(_emoji_new) >= 600:
            return Response(status_code=503, headers={"Retry-After": "60"})
        _emoji_new.append(now_)
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
    """Play history of this voice chat over the last 24 hours: what played, who added it, when. ?offset= for more,
    ?mine=1 for my songs, ?q= to search the song, artist or who added it."""
    offset = max(0, int(_num(request.query_params.get("offset"), 0)))
    limit = max(10, min(100, int(_num(request.query_params.get("limit"), 50))))
    mine = request.query_params.get("mine") == "1"
    q = (request.query_params.get("q") or "").strip()[:60]
    where, args = "chat_id=? AND ts>?", [chat_id, time.time() - 86400]
    if mine:
        where += " AND by_uid=?"
        args.append(uid)
    if q:
        like = "%" + q.replace("\\", "").replace("%", "").replace("_", " ") + "%"
        # (someone who played as the group is found by the group's name, not their own)
        where += " AND (title LIKE ? OR artist LIKE ? OR by_name LIKE ?)"
        args += [like, like, like]
    rows = await db.q("SELECT vid as id,title,artist,duration,by_uid,by_name,ts,asg FROM plays WHERE " + where
                      + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit + 1, offset))
    more = len(rows) > limit
    rows = rows[:limit]
    from .room import pid_of
    for r in rows:
        r["thumb"] = f"/api/thumb/{r['id']}"
        if r.pop("asg", 0) and not st["admin"] and r["by_uid"] != uid:
            r["by_uid"] = pid_of(chat_id, r["by_uid"])  # played as the group: members don't learn who it was
    return JSONResponse({"tracks": rows, "more": more, "next": offset + limit})


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
    # a song already in the playlist isn't added again (Liked Songs got the same song twice)
    have = {r["vid"] for r in await db.q("SELECT json_extract(track,'$.id') AS vid FROM playlist_items WHERE playlist_id=?", (pid,))}
    rows = []
    for t in tracks[:300]:
        c = _clean_track(t)
        if c and c["id"] not in have:
            have.add(c["id"])
            rows.append((pid, json.dumps(c), now))
    await db.xmany("INSERT INTO playlist_items(playlist_id,track,added) VALUES(?,?,?)", rows)


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
        if r["online"] and t >= time.time() - 60:  # (the live mic time belongs to now, not to an older range)
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
    if _num(request.headers.get("content-length"), 0) > REC_MAX + 1024 * 1024:
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
    chapters = str(form.get("chapters") or "")[:3800].strip()
    if chapters:  # who spoke when + the songs, so the recording is easy to jump around in
        try:
            await tg.call("sendMessage", chat_id=uid, text=f"📑 {custom or 'Recording'} — {chapters}")
        except Exception as e:
            log.info("chapters not sent: %s", e)
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
_invite_chat: dict[int, list[float]] = {}  # chat -> recent invite times (everyone's)


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
                if not pool:  # Telegram gave no more people (its count can be higher than what it lists)
                    total = offset
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
    if len(recent) >= 5:
        return err("That's a lot of invites — try again in a minute 🙂", 429)
    # the whole group too: invites are mentions in the group now, and many at once are spam
    group = [x for x in _invite_chat.get(chat_id, []) if t - x < 600]
    if len(group) >= 20:
        return err("Lots of people were invited just now — try again in a few minutes 🙂", 429)
    info = await tg.member_status(chat_id, target, fresh=True)  # also gives the real name (not trusted from the app)
    name = info.get("name") or "Member"
    if not info["member"]:
        return err(f"{name if info.get('name') else 'This person'} isn't in the group any more")
    if info.get("bot"):
        return err("Bots can't join the voice chat")
    if t - _invite_last.get((chat_id, target), 0) < 300:
        return err(f"{name} was invited a moment ago", 429)
    _invite_by[(chat_id, uid)] = recent + [t]
    _invite_chat[chat_id] = group + [t]  # (counted only once it's going out: failed tries used to count too)
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
        for lst in (_invite_by.get((chat_id, uid)), _invite_chat.get(chat_id)):  # (didn't go out: doesn't count)
            if lst and t in lst:
                lst.remove(t)
        return err("Couldn't send the invite — try again")
    return JSONResponse({"ok": True, "via": via, "name": name})


# ---------------- badges ----------------
@guarded()
async def api_badges(request: Request, uid, chat_id, st):
    got = await badges.for_group(chat_id)
    return JSONResponse({"badges": {str(u): badges.describe(ids) for u, ids in got.items()},
                         "all": badges.describe(badges.ORDER)})


# ---------------- sudo users (the owner manages them) ----------------
@guarded()
async def api_sudo(request: Request, uid, chat_id, st):
    """GET: the list. POST {"who": "<id or @username>"} adds, {"remove": uid} removes. The owner only."""
    if not sudo.is_owner(uid):
        return err("Only the bot's owner can change sudo users", 403)
    if request.method == "POST":
        body = await _json(request)
        if "remove" in body:
            await sudo.remove(int(_num(body.get("remove"))))
        else:
            who = await sudo.find(str(body.get("who", ""))[:64])
            if not who:
                return err("I don't know that username yet. Ask them to open the voice chat once (or write in a "
                           "group with the bot), or use their numeric Telegram id.")
            if sudo.is_owner(who["uid"]):
                return err("That's you — the owner always has access")
            await sudo.add(who["uid"], who.get("name") or "", who.get("username") or "", uid)
    return JSONResponse({"users": await sudo.listing()})


# ---------------- health (the bot's owner and sudo users) ----------------
_started = time.time()


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


@guarded()
async def api_health(request: Request, uid, chat_id, st):
    """Is everything working? One look for the owner: bot, YouTube, LiveKit, member lists, storage, phones' problems."""
    import shutil
    from .room import DIAG
    if not is_sudo(uid, st):
        return err("Only the bot's owner and sudo users can see this", 403)
    if request.method == "POST":  # "Test YouTube now"
        t = time.time()
        try:
            await music.resolve("jNQXAC9IVRw", force=True)  # a tiny public video
            return JSONResponse({"ok": True, "detail": f"YouTube works ({time.time() - t:.1f} s)"})
        except Exception as e:
            return JSONResponse({"ok": False, "detail": f"YouTube failed: {str(e)[:200]}"})
    items = []
    if config.DEV_MODE:  # (anyone can log in as anyone: only for testing on a computer)
        items.append({"name": "DEV_MODE", "ok": False, "detail": "DEV_MODE=1 is on: anyone can open the app as anyone. Remove it on Railway now."})
    tgs = tg.STATS
    if not config.BOT_TOKEN:
        items.append({"name": "Telegram bot", "ok": False, "detail": "BOT_TOKEN is not set"})
    else:
        ok = tgs["poll_ok_at"] and time.time() - tgs["poll_ok_at"] < 180
        items.append({"name": "Telegram bot", "ok": bool(ok), "detail": f"@{tg.BOT_USERNAME} · last update check {_ago(tgs['poll_ok_at'])}"
                      + (f" · last error {_ago(tgs['poll_err_at'])}: {tgs['poll_err']}" if tgs["poll_err_at"] > tgs["poll_ok_at"] else "")})
        if tgs.get("conflicts", 0) >= 10:
            items.append({"name": "Second bot copy", "ok": False, "detail": f"{tgs['conflicts']} times another program used "
                          "this BOT_TOKEN at the same time (another server, Railway service or PC). Stop it, or the bot misses messages."})
        items.append({"name": "Bot privacy mode", "ok": not tg.PRIVACY_ON,
                      "detail": "On: the bot doesn't see group messages (@BotFather → /setprivacy → Disable)" if tg.PRIVACY_ON else "Off: the bot sees who chats"})
    ms = music.STATS
    yt_ok = ms["yt_ok_at"] >= ms["yt_err_at"]
    items.append({"name": "YouTube", "ok": None if not (ms["yt_ok_at"] or ms["yt_err_at"]) else yt_ok, "test": "youtube",
                  "detail": (f"Last song found {_ago(ms['yt_ok_at'])}" if yt_ok and ms["yt_ok_at"] else
                             f"Last failure {_ago(ms['yt_err_at'])}: {ms['yt_err'][:160]}" if ms["yt_err_at"] else "No songs played yet")
                  + (" · PO token: " + ("plugin ready" if music.POT_PLUGIN else "plugin missing")
                     + (", provider set" if config.POT_PROVIDER_URL else ", POT_PROVIDER_URL not set"))
                  + ("" if config.POT_PROVIDER_URL or os.path.exists(music.COOKIE_FILE) else " · tip: set POT_PROVIDER_URL against YouTube blocks")})
    items.append({"name": "PO token provider", **(await music.pot_check())})
    items.append({"name": "Music in the voice chat", **voicemusic.status()})
    n403 = music.refused_last_hour()
    items.append({"name": "YouTube song links", "ok": n403 < 10,
                  "detail": "No refused links in the last hour" if not n403 else
                  f"YouTube refused {n403} song link{'s' if n403 != 1 else ''} in the last hour"
                  + (" — songs may start late or stay silent. Check the PO token provider above; YouTube cookies "
                     "(YT_COOKIES_B64) help too" if n403 >= 10 else " (retried by themselves)")})
    lkc = await lk.check()
    items.append({"name": "Voice (LiveKit)", **lkc})
    items.append({"name": "Full member list", **members.status()})
    items.append({"name": "Spotify links", "ok": True, "detail": "Official API (whole playlists)" if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET
                  else "Public page (up to ~100 songs) · set SPOTIFY_CLIENT_ID / SECRET for more"})
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        sizes = {".bin": 0, ".vbin": 0}
        for f in os.listdir(music.CACHE_DIR):
            ext = os.path.splitext(f)[1]
            if ext in sizes:
                sizes[ext] += os.path.getsize(os.path.join(music.CACHE_DIR, f))
        items.append({"name": "Storage", "ok": du.free > 300 * 1024 * 1024,
                      "detail": f"{du.free // 2**20} MB free · song cache {sizes['.bin'] // 2**20} of {config.AUDIO_CACHE_MB} MB"
                      f" · video cache {sizes['.vbin'] // 2**20} of {config.VIDEO_CACHE_MB} MB"
                      + ("" if os.path.ismount(config.DATA_DIR) or config.DEV_MODE else " · no volume at /data: data is lost on redeploy")})
    except Exception as e:
        items.append({"name": "Storage", "ok": False, "detail": str(e)[:150]})
    online = sum(len(r.parts) for r in ROOMS.values())
    items.append({"name": "Server", "ok": True, "detail": f"Up {_ago(_started).replace(' ago', '')} · {len(ROOMS)} voice chat{'s' if len(ROOMS) != 1 else ''} · {online} people online"})
    day = time.time() - 86400
    probs = [d for d in DIAG if d["chat"] == chat_id and d["ts"] > day]
    kinds: dict[str, int] = {}
    for d in probs:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    return JSONResponse({"items": items, "problems": {"counts": kinds, "latest": [
        {k: d[k] for k in ("ts", "name", "device", "kind", "detail")} for d in list(probs)[-25:][::-1]]}})


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
    conn.cid = ws.query_params.get("c", "")[:40]
    conn.sudo = is_sudo(uid, st)
    conn.owner = sudo.is_owner(uid)
    try:
        await room.join(conn, user, st["admin"], ws.query_params.get("asg") == "1")
        while True:
            raw = await ws.receive_text()
            t = time.time()
            conn.hits.append(t)
            if len(conn.hits) == conn.hits.maxlen and t - conn.hits[0] < 5:
                # flood protection: dropped (said once in a while, so a tap that does nothing isn't a mystery)
                if t - conn.flood_told > 10:
                    conn.flood_told = t
                    await conn.send({"t": "toast", "text": "Slow down a little 🙂"})
                continue
            if len(raw) > 64_000:  # ("Add all": 200 songs are ~40 KB; 8000 cut it off at ~45 songs)
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
    cid = request.query_params.get("c", "")
    if p:
        # only the app that closed: the same person on another phone stays (old apps send no id: all of them)
        for c in [c for c in p.conns if not cid or getattr(c, "cid", "") == cid]:
            await c.close(1000)
            await room.leave(c, at_once=True)
        if not p.conns and room.parts.get(uid) is p:
            # the socket had already dropped (it's lingering): closing the app means gone now, not in 15 s
            await room._gone(p)
    return Response(status_code=204)


# ---------------- static ----------------
async def index(request: Request):
    return FileResponse(os.path.join(WEB, "index.html"), headers={
        "Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"})


async def health(request: Request):
    return JSONResponse({"ok": True})


async def favicon(request: Request):
    # browsers ask for it on every open (it was a 404 in the logs): a small music note
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2b5278"/>'
           '<path d="M40 14v24.5a7 7 0 1 1-4-6.3V22l-12 3v17.5a7 7 0 1 1-4-6.3V20z" fill="#fff"/></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


async def robots(request: Request):
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(tg.run_polling()), asyncio.create_task(tg.schedule_loop()),
             asyncio.create_task(heartbeat_loop()), asyncio.create_task(pin.startup_reset()),
             asyncio.create_task(pin.countdown_loop()), asyncio.create_task(bg_tick_loop())]
    log.info("LiveKit %s", "enabled" if lk.enabled() else "NOT configured (voice disabled)")
    if config.DEV_MODE and config.BOT_TOKEN:
        log.error("DEV_MODE=1 is on with a real bot: anyone can open the app as anyone. Remove DEV_MODE.")
    try:
        await asyncio.to_thread(music._trim_cache, False, True)  # (half-finished downloads from before a restart)
    except Exception as e:
        log.info("cache trim at start: %s", e)
    yield
    for r in list(ROOMS.values()):  # a deploy or restart: keep the queue and the song for when it's back
        await r.save_now()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in (music._http, music._sthttp, tg._client, lk._client):
        if c is not None:
            await c.aclose()


routes = [
    Route("/", index),
    Route("/health", health),
    Route("/robots.txt", robots),
    Route("/favicon.ico", favicon),
    Route("/api/auth", api_auth, methods=["POST"]),
    Route("/api/lk", api_lk_token),
    Route("/api/search", api_search),
    Route("/api/stream/{vid}/{kind}", api_stream),
    Route("/api/radio", api_radio),
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
    Route("/api/health", api_health, methods=["GET", "POST"]),
    Route("/api/sudo", api_sudo, methods=["GET", "POST"]),
    Route("/api/badges", api_badges),
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
