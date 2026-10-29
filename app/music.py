"""YouTube via yt-dlp: search, playlists, stream resolving, audio cache, range proxy, lyrics."""
import asyncio
import base64
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse

from . import config
from .http import httpx

log = logging.getLogger("music")

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

CACHE_DIR = os.path.join(config.DATA_DIR, "audio")
os.makedirs(CACHE_DIR, exist_ok=True)
COOKIE_FILE = os.path.join(config.DATA_DIR, "cookies.txt")
if config.YT_COOKIES_B64:
    open(COOKIE_FILE, "wb").write(base64.b64decode(config.YT_COOKIES_B64))
elif config.YT_COOKIES:
    open(COOKIE_FILE, "w").write(config.YT_COOKIES.replace("\\n", "\n"))

_info_cache: dict[str, tuple[float, dict]] = {}
_downloads: dict[str, asyncio.Task] = {}
_resolve_locks: dict[str, asyncio.Lock] = {}
_http: httpx.AsyncClient | None = None
CHUNK = 8 * 1024 * 1024  # googlevideo throttles big open ranges; serve in 8MB pieces


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
    return _http


class _QuietLog:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): log.info("yt-dlp: %s", str(msg)[:300])


def _opts(**extra) -> dict:
    o = {"logger": _QuietLog(), "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
         "socket_timeout": 15, "retries": 3, "extractor_retries": 2}
    if os.path.exists(COOKIE_FILE):
        o["cookiefile"] = COOKIE_FILE
    if config.POT_PROVIDER_URL:
        o["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [config.POT_PROVIDER_URL]}}
    o.update(extra)
    return o


def _thumb(vid: str) -> str:
    return f"/api/thumb/{vid}"


def _track(e: dict) -> dict | None:
    vid = e.get("id")
    if not vid or len(vid) != 11:
        return None
    if e.get("live_status") in ("is_live", "is_upcoming"):
        return None
    artist = e.get("artist") or e.get("channel") or e.get("uploader") or ""
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    return {"id": vid, "title": e.get("track") or e.get("title") or "Unknown", "artist": artist,
            "duration": float(e.get("duration") or 0), "thumb": _thumb(vid)}


def parse_youtube(text: str) -> tuple[str | None, str | None]:
    """Returns (video_id, playlist_id) from any YouTube / YT Music URL."""
    text = text.strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return text, None
    try:
        u = urlparse(text if "://" in text else "https://" + text)
    except Exception:
        return None, None
    host = (u.hostname or "").lower()
    if not any(h in host for h in ("youtube.com", "youtu.be")):
        return None, None
    qs = parse_qs(u.query)
    vid = None
    if "youtu.be" in host:
        vid = u.path.strip("/")[:11]
    elif "v" in qs:
        vid = qs["v"][0][:11]
    elif m := re.match(r"/(shorts|live|embed)/([\w-]{11})", u.path):
        vid = m.group(2)
    pl = qs.get("list", [None])[0]
    if pl and pl.startswith("RD"):  # auto mixes: treat as single video
        pl = None
    return vid, pl


def _extract(url: str, **extra) -> dict:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")
    with yt_dlp.YoutubeDL(_opts(**extra)) as y:
        return y.extract_info(url, download=False)


async def search(query: str, limit: int = 15) -> list[dict]:
    info = await asyncio.to_thread(_extract, f"ytsearch{limit}:{query}", extract_flat="in_playlist")
    return [t for e in info.get("entries") or [] if (t := _track(e))]


async def playlist(pid: str, limit: int = 200) -> tuple[str, list[dict]]:
    info = await asyncio.to_thread(_extract, f"https://www.youtube.com/playlist?list={pid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return info.get("title") or "Playlist", [t for e in info.get("entries") or [] if (t := _track(e))]


async def related(vid: str, limit: int = 25) -> list[dict]:
    info = await asyncio.to_thread(_extract, f"https://www.youtube.com/watch?v={vid}&list=RD{vid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return [t for e in info.get("entries") or [] if (t := _track(e)) and t["id"] != vid]


async def resolve(vid: str, force: bool = False) -> dict:
    hit = _info_cache.get(vid)
    if hit and not force and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    lock = _resolve_locks.setdefault(vid, asyncio.Lock())
    async with lock:
        hit = _info_cache.get(vid)
        if hit and not force and time.time() - hit[0] < 3 * 3600:
            return hit[1]
        info = await asyncio.to_thread(_extract, f"https://www.youtube.com/watch?v={vid}")
        fmts = []
        for f in info.get("formats") or []:
            if not f.get("url") or f.get("protocol") not in ("https", "http"):
                continue
            fmts.append({"id": f.get("format_id"), "url": f["url"], "ext": f.get("ext"),
                         "acodec": f.get("acodec") or "none", "vcodec": f.get("vcodec") or "none",
                         "height": f.get("height") or 0, "abr": f.get("abr") or 0, "tbr": f.get("tbr") or 0,
                         "headers": f.get("http_headers") or {}})
        out = {"track": _track(info), "formats": fmts}
        _info_cache[vid] = (time.time(), out)
        if len(_info_cache) > 500:
            for k, _ in sorted(_info_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _info_cache.pop(k, None)
        return out


def pick_audio(info: dict, codec: str = "aac", quality: str = "medium") -> dict | None:
    auds = [f for f in info["formats"] if f["vcodec"] == "none" and f["acodec"] != "none"]
    if codec == "aac":
        pref = [f for f in auds if f["ext"] == "m4a" or f["acodec"].startswith("mp4a")]
    else:
        pref = [f for f in auds if f["ext"] == "webm" or "opus" in f["acodec"]]
    auds = sorted(pref or auds, key=lambda f: f["abr"] or f["tbr"])
    if not auds:
        return None
    if quality == "low":
        return auds[0]
    if quality == "high":
        return auds[-1]
    mid = [f for f in auds if (f["abr"] or f["tbr"]) <= 135]
    return mid[-1] if mid else auds[0]


def pick_video(info: dict, height: int = 480) -> dict | None:
    vids = [f for f in info["formats"] if f["vcodec"] != "none" and f["acodec"] == "none" and f["height"]]
    avc = [f for f in vids if f["vcodec"].startswith("avc1") and f["ext"] == "mp4"]
    vids = avc or vids
    if not vids:  # muxed fallback (itag 18)
        vids = [f for f in info["formats"] if f["vcodec"] != "none" and f["height"]]
    under = sorted([f for f in vids if f["height"] <= height], key=lambda f: (f["height"], f["tbr"]))
    if under:
        return under[-1]
    return min(vids, key=lambda f: f["height"]) if vids else None


def video_heights(info: dict) -> list[int]:
    hs = {f["height"] for f in info["formats"] if f["vcodec"] != "none" and f["height"]}
    return sorted(h for h in hs if h in (144, 240, 360, 480, 720, 1080))


# ---- audio cache ---------------------------------------------------------------------------
def _cache_path(vid: str, fid: str) -> str:
    return os.path.join(CACHE_DIR, f"{vid}_{re.sub(r'[^\w-]', '', fid)}.bin")


async def _download(vid: str, fmt: dict):
    path = _cache_path(vid, fmt["id"])
    tmp = path + ".part"
    try:
        pos = 0
        with open(tmp, "wb") as fh:
            while True:
                r = await http().get(fmt["url"], headers={**fmt["headers"], "Range": f"bytes={pos}-{pos + CHUNK - 1}"})
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"status {r.status_code}")
                fh.write(r.content)
                total = int(r.headers.get("content-range", "/0").split("/")[-1] or 0)
                pos += len(r.content)
                if r.status_code == 200 or not r.content or (total and pos >= total):
                    break
        os.replace(tmp, path)
        _trim_cache()
    except Exception as e:
        log.info("cache download failed %s: %s", vid, e)
        if os.path.exists(tmp):
            os.remove(tmp)


def _trim_cache():
    files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".bin")]
    files.sort(key=os.path.getatime)
    total = sum(os.path.getsize(f) for f in files)
    while files and total > config.AUDIO_CACHE_MB * 1024 * 1024:
        f = files.pop(0)
        total -= os.path.getsize(f)
        os.remove(f)


async def prefetch(vid: str):
    """Resolve + cache the default audio formats for a track (both codecs) in the background."""
    try:
        info = await resolve(vid)
    except Exception as e:
        log.info("prefetch resolve failed %s: %s", vid, e)
        return
    for codec in ("aac", "opus"):
        f = pick_audio(info, codec, "medium")
        if not f:
            continue
        key = f"{vid}_{f['id']}"
        if os.path.exists(_cache_path(vid, f["id"])) or key in _downloads:
            continue
        t = asyncio.create_task(_download(vid, f))
        _downloads[key] = t
        t.add_done_callback(lambda _t, k=key: _downloads.pop(k, None))


def _mime(fmt: dict) -> str:
    if fmt["vcodec"] == "none":
        return "audio/mp4" if fmt["ext"] == "m4a" else "audio/webm"
    return "video/mp4" if fmt["ext"] == "mp4" else "video/webm"


async def stream(request: Request, vid: str, kind: str, codec: str, quality: str) -> Response:
    try:
        info = await resolve(vid)
    except Exception as e:
        log.warning("resolve failed %s: %s", vid, e)
        return Response("unavailable", status_code=502)
    if kind == "audio":
        fmt = pick_audio(info, codec, quality)
    else:
        fmt = pick_video(info, int(quality) if quality.isdigit() else 480)
    if not fmt:
        return Response("no format", status_code=404)
    path = _cache_path(vid, fmt["id"])
    if os.path.exists(path):
        return FileResponse(path, media_type=_mime(fmt), headers={"Cache-Control": "private, max-age=3600"})
    return await _proxy(request, vid, fmt, retry=True)


async def _proxy(request: Request, vid: str, fmt: dict, retry: bool) -> Response:
    rng = request.headers.get("range", "bytes=0-")
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    start = int(m.group(1) or 0) if m else 0
    end = int(m.group(2)) if m and m.group(2) else start + CHUNK - 1
    end = min(end, start + CHUNK - 1)
    req = http().build_request("GET", fmt["url"], headers={**fmt["headers"], "Range": f"bytes={start}-{end}"})
    r = await http().send(req, stream=True)
    if r.status_code in (403, 410) and retry:
        await r.aclose()
        info = await resolve(vid, force=True)
        same = [f for f in info["formats"] if f["id"] == fmt["id"]]
        if same:
            return await _proxy(request, vid, same[0], retry=False)
        return Response("expired", status_code=502)
    if r.status_code not in (200, 206):
        await r.aclose()
        return Response("upstream error", status_code=502)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    for h in ("content-length", "content-range"):
        if h in r.headers:
            headers[h] = r.headers[h]

    async def body():
        try:
            async for chunk in r.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(body(), status_code=r.status_code, media_type=_mime(fmt), headers=headers)


async def thumb(vid: str) -> Response:
    for q in ("maxresdefault", "hqdefault", "mqdefault"):
        try:
            r = await http().get(f"https://i.ytimg.com/vi/{vid}/{q}.jpg")
            if r.status_code == 200 and len(r.content) > 2000:
                return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
        except Exception:
            pass
    return Response(status_code=404)


# ---- lyrics (LRCLIB, free) -----------------------------------------------------------------
_JUNK = re.compile(r"[\(\[][^\)\]]*(official|video|audio|lyric|lyrics|visualizer|hd|4k|remaster|full song|mv)[^\)\]]*[\)\]]", re.I)
_lyr_cache: dict[str, dict] = {}


def _clean(title: str, artist: str) -> tuple[str, str]:
    t = _JUNK.sub("", title)
    t = re.sub(r"\s*\|.*$", "", t)
    t = re.sub(r"\s*(ft\.?|feat\.?)\s.*$", "", t, flags=re.I).strip(" -")
    if " - " in t:
        a, b = t.split(" - ", 1)
        return b.strip(), a.strip()
    return t.strip(), artist


async def lyrics(vid: str, title: str, artist: str, duration: float) -> dict:
    if vid in _lyr_cache:
        return _lyr_cache[vid]
    name, art = _clean(title, artist)
    res = {"synced": None, "plain": None}
    try:
        r = await http().get("https://lrclib.net/api/search", params={"track_name": name, "artist_name": art},
                             headers={"User-Agent": "VCMusicMiniApp/1.0"})
        items = r.json() if r.status_code == 200 else []
        if not items:
            r = await http().get("https://lrclib.net/api/search", params={"q": f"{art} {name}"},
                                 headers={"User-Agent": "VCMusicMiniApp/1.0"})
            items = r.json() if r.status_code == 200 else []
        if items:
            items.sort(key=lambda i: (not i.get("syncedLyrics"), abs((i.get("duration") or 0) - duration)))
            best = items[0]
            if not duration or abs((best.get("duration") or 0) - duration) < 15:
                res = {"synced": best.get("syncedLyrics"), "plain": best.get("plainLyrics")}
    except Exception as e:
        log.info("lyrics failed: %s", e)
    _lyr_cache[vid] = res
    return res


# ---- browse: genres, trending, recommendations (cached so YouTube isn't hit every open) ----------
YEAR = time.strftime("%Y")
GENRES = [
    {"id": "punjabi", "title": "Punjabi", "c": ["#ff7a18", "#af002d"], "q": [f"latest punjabi songs {YEAR}", "punjabi hit songs"]},
    {"id": "bollywood", "title": "Bollywood", "c": ["#f7971e", "#d4145a"], "q": [f"latest bollywood songs {YEAR}", "bollywood hits"]},
    {"id": "hiphop", "title": "Hip-Hop & Rap", "c": ["#232526", "#5b5b5b"], "q": ["desi hip hop songs", "hip hop hits"]},
    {"id": "romantic", "title": "Romantic", "c": ["#ee0979", "#ff6a88"], "q": ["romantic hindi songs", "romantic punjabi songs"]},
    {"id": "party", "title": "Party", "c": ["#8e2de2", "#4a00e0"], "q": ["party songs hindi punjabi", "dance party hits"]},
    {"id": "sad", "title": "Sad", "c": ["#2c3e50", "#4ca1af"], "q": ["sad hindi songs", "sad punjabi songs"]},
    {"id": "lofi", "title": "Lo-fi & Chill", "c": ["#614385", "#516395"], "q": ["hindi lofi songs", "lofi chill songs"]},
    {"id": "pop", "title": "Pop", "c": ["#fc466b", "#3f5efb"], "q": [f"pop hits {YEAR}", "english pop songs"]},
    {"id": "haryanvi", "title": "Haryanvi", "c": ["#11998e", "#38ef7d"], "q": [f"latest haryanvi songs {YEAR}", "haryanvi hit songs"]},
    {"id": "retro", "title": "90s & Retro", "c": ["#c79081", "#dfa579"], "q": ["90s hindi songs", "old hindi songs evergreen"]},
    {"id": "workout", "title": "Workout", "c": ["#f12711", "#f5af19"], "q": ["gym workout songs", "workout motivation songs"]},
    {"id": "edm", "title": "Dance & EDM", "c": ["#00c6ff", "#0072ff"], "q": ["edm hits", "bollywood remix songs"]},
    {"id": "indie", "title": "Indie", "c": ["#606c88", "#3f4c6b"], "q": ["hindi indie songs", "indie pop songs india"]},
    {"id": "devotional", "title": "Devotional", "c": ["#f6d365", "#fda085"], "q": ["shabad gurbani kirtan", "bhakti songs"]},
    {"id": "english", "title": "English Hits", "c": ["#1d2b64", "#f8cdda"], "q": [f"top english songs {YEAR}", "billboard hits"]},
    {"id": "sufi", "title": "Sufi & Ghazal", "c": ["#3a1c71", "#d76d77"], "q": ["sufi songs", "best ghazals"]},
]
SPECIAL = {
    "trending": [f"trending songs india {YEAR}", f"top hits this week {YEAR}"],
    "new": [f"new songs this week {YEAR}", f"new punjabi songs {YEAR}"],
}
_bcache: dict[str, tuple[float, list]] = {}
_blocks: dict[str, asyncio.Lock] = {}


async def _cached(key: str, ttl: float, fn):
    hit = _bcache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    lock = _blocks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _bcache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await fn()
        except Exception as e:
            log.info("browse %s failed: %s", key, e)
            if hit:
                return hit[1]
            raise
        _bcache[key] = (time.time(), val)
        return val


def _interleave(lists: list[list[dict]], limit: int) -> list[dict]:
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["id"] not in seen and (not lst[i]["duration"] or 60 < lst[i]["duration"] < 900):
                seen.add(lst[i]["id"])
                out.append(lst[i])
    return out[:limit]


async def _multi(queries: list[str], limit: int) -> list[dict]:
    res = await asyncio.gather(*(search(q, 15) for q in queries), return_exceptions=True)
    return _interleave([r for r in res if isinstance(r, list)], limit)


def genre_list() -> list[dict]:
    return [{"id": g["id"], "title": g["title"], "c": g["c"]} for g in GENRES]


async def collection(cid: str) -> dict:
    g = next((x for x in GENRES if x["id"] == cid), None)
    if g:
        tracks = await _cached("g:" + cid, 6 * 3600, lambda: _multi(g["q"], 30))
        return {"title": g["title"], "c": g["c"], "tracks": tracks}
    if cid in SPECIAL:
        title = {"trending": "Trending Now", "new": "New Releases"}[cid]
        tracks = await _cached("s:" + cid, 3 * 3600, lambda: _multi(SPECIAL[cid], 30))
        return {"title": title, "c": ["#fa2d48", "#a8183a"] if cid == "trending" else ["#5e5ce6", "#bf5af2"], "tracks": tracks}
    raise KeyError(cid)


async def recommend(seed_ids: list[str], exclude: set[str]) -> list[dict]:
    seeds = seed_ids[:3]
    if not seeds:
        return []
    key = "r:" + ",".join(seeds)

    async def build():
        res = await asyncio.gather(*(related(s, 20) for s in seeds), return_exceptions=True)
        return _interleave([r for r in res if isinstance(r, list)], 40)

    tracks = await _cached(key, 1800, build)
    return [t for t in tracks if t["id"] not in exclude][:25]
